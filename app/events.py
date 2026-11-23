"""Turn a raw message from the registry feed into a validated, normalized event.

The feed is unvalidated, so every rule here comes from what the real feed contains (see docs/decisions.md).
Anything we cannot trust raises `InvalidEvent` with a short machine-readable reason; the consumer stores
those in `rejected_events` instead of crashing.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

# The registry sends naive timestamps in its local time (verified against neighbouring events).
REGISTRY_TIMEZONE = timezone(timedelta(hours=8))

REGISTRATION_NUMBER = re.compile(r"SYN-\d{6}")

CANONICAL_STATES = (
    "Johor", "Kedah", "Kelantan", "Melaka", "Negeri Sembilan", "Pahang", "Perak", "Perlis",
    "Pulau Pinang", "Sabah", "Sarawak", "Selangor", "Terengganu",
    "Kuala Lumpur", "Labuan", "Putrajaya",
)  # fmt: skip
_STATES_BY_LOWERCASE = {state.lower(): state for state in CANONICAL_STATES}

# A description made of one phrase repeated at least this many times is collapsed to one copy.
MIN_REPEATS_TO_COLLAPSE = 3

DATA_FIELDS = ("companyName", "natureOfBusiness", "state", "industry")


class InvalidEvent(Exception):
    """A message we cannot apply. `reason` is a short code stored with the rejected message."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class CompanyUpserted:
    registration_number: str
    updated_at: datetime
    company_name: str
    description: str | None
    industry: str | None
    state: str | None


@dataclass(frozen=True)
class CompanyStruckOff:
    registration_number: str
    updated_at: datetime


type CompanyEvent = CompanyUpserted | CompanyStruckOff


def parse_event(body: bytes) -> CompanyEvent:
    """Validate one message body. Raises `InvalidEvent` if it cannot be applied safely."""
    try:
        message = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidEvent("invalid_json", str(error)) from error
    if not isinstance(message, dict):
        raise InvalidEvent("not_an_object", type(message).__name__)

    event_type = message.get("type")
    if event_type not in ("company-upserted", "company-struck-off"):
        raise InvalidEvent("unknown_type", repr(event_type))

    registration_number = parse_registration_number(message.get("registrationNumber"))
    updated_at = parse_timestamp(message.get("updatedAt"))

    if event_type == "company-struck-off":
        return CompanyStruckOff(registration_number, updated_at)

    data = message.get("data")
    # Partial data is rejected rather than merged: a stub like {"companyName": "Unknown"} must never
    # become the latest version of a company.
    if not isinstance(data, dict) or any(field not in data for field in DATA_FIELDS):
        raise InvalidEvent("invalid_data", "data must be an object with " + ", ".join(DATA_FIELDS))
    company_name = data["companyName"]
    if not isinstance(company_name, str) or not company_name.strip():
        raise InvalidEvent("invalid_data", "companyName must be a non-empty string")
    for field in ("natureOfBusiness", "state", "industry"):
        if data[field] is not None and not isinstance(data[field], str):
            raise InvalidEvent("invalid_data", f"{field} must be a string or null")

    return CompanyUpserted(
        registration_number=registration_number,
        updated_at=updated_at,
        company_name=company_name.strip(),
        description=clean_description(data["natureOfBusiness"]),
        industry=(data["industry"] or "").strip() or None,
        state=normalize_state(data["state"]),
    )


def parse_registration_number(value: object) -> str:
    """Accept `SYN-` numbers with stray whitespace or lowercase; reject everything else."""
    if not isinstance(value, str):
        raise InvalidEvent("invalid_registration_number", repr(value))
    normalized = value.strip().upper()
    if not REGISTRATION_NUMBER.fullmatch(normalized):
        raise InvalidEvent("invalid_registration_number", repr(value))
    return normalized


def parse_timestamp(value: object) -> datetime:
    """Parse `updatedAt` into a timezone-aware datetime.

    Seen in the feed: ISO 8601 with an offset, with `Z`, without any timezone (registry local time), and
    integers holding epoch milliseconds.
    """
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, ValueError, OSError) as error:
            raise InvalidEvent("invalid_updated_at", repr(value)) from error
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as error:
            raise InvalidEvent("invalid_updated_at", repr(value)) from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=REGISTRY_TIMEZONE)
    raise InvalidEvent("invalid_updated_at", repr(value))


def normalize_state(value: str | None) -> str | None:
    """Map any casing onto the canonical state name. Unknown names are kept as given, never guessed."""
    if value is None or not value.strip():
        return None
    stripped = value.strip()
    return _STATES_BY_LOWERCASE.get(stripped.lower(), stripped)


def clean_description(value: str | None) -> str | None:
    """Trim the description, treat empty as missing, and collapse one phrase repeated many times."""
    if value is None or not value.strip():
        return None
    words = value.split()
    for phrase_length in range(1, len(words) // MIN_REPEATS_TO_COLLAPSE + 1):
        repeats, remainder = divmod(len(words), phrase_length)
        if remainder == 0 and words == words[:phrase_length] * repeats:
            return " ".join(words[:phrase_length])
    return " ".join(words)
