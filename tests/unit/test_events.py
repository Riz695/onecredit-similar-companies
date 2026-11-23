"""Parsing and validation of raw feed messages."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.events import (
    CompanyStruckOff,
    CompanyUpserted,
    InvalidEvent,
    clean_description,
    normalize_state,
    parse_event,
    parse_registration_number,
    parse_timestamp,
)

MYT = timezone(timedelta(hours=8))


def body(**fields: object) -> bytes:
    """Build a raw message body the way the registry sends it."""
    return json.dumps(fields).encode()


def upsert_body(**overrides: object) -> bytes:
    fields: dict[str, object] = {
        "type": "company-upserted",
        "registrationNumber": "SYN-001550",
        "updatedAt": "2026-08-14T09:00:00+08:00",
        "data": {
            "companyName": "Sinar Teguh Bakery Sdn. Bhd.",
            "natureOfBusiness": "Operating cafes and coffee shops in the Klang Valley",
            "state": "Selangor",
            "industry": "Food & Beverage",
        },
    }
    fields.update(overrides)
    return body(**fields)


class TestParseEvent:
    def test_valid_upsert(self) -> None:
        event = parse_event(upsert_body())
        assert event == CompanyUpserted(
            registration_number="SYN-001550",
            updated_at=datetime(2026, 8, 14, 9, 0, tzinfo=MYT),
            company_name="Sinar Teguh Bakery Sdn. Bhd.",
            description="Operating cafes and coffee shops in the Klang Valley",
            industry="Food & Beverage",
            state="Selangor",
        )

    def test_struck_off_needs_no_data(self) -> None:
        event = parse_event(
            body(type="company-struck-off", registrationNumber="SYN-001748", updatedAt="2026-08-14T16:00:33+08:00")
        )
        assert event == CompanyStruckOff(
            registration_number="SYN-001748", updated_at=datetime(2026, 8, 14, 16, 0, 33, tzinfo=MYT)
        )

    def test_unknown_top_level_fields_are_ignored(self) -> None:
        assert isinstance(parse_event(upsert_body(source="ssm-batch", ingestedBy="partner-api")), CompanyUpserted)

    def test_null_optional_fields_are_kept_as_none(self) -> None:
        data = {"companyName": "Maju Kirana Resort Trading", "natureOfBusiness": None, "state": None, "industry": None}
        event = parse_event(upsert_body(data=data))
        assert isinstance(event, CompanyUpserted)
        assert (event.description, event.industry, event.state) == (None, None, None)

    @pytest.mark.parametrize(
        ("raw", "reason"),
        [
            (
                b'{"type": "company-upserted", "registrationNumber": "SYN-000509", "data": {"companyName": "Bes',
                "invalid_json",
            ),
            (b"null", "not_an_object"),
            (b"\xff\xfe", "invalid_json"),
            (body(registrationNumber="SYN-000001", updatedAt="2026-08-14T09:00:00+08:00"), "unknown_type"),
            (upsert_body(type="company-renamed"), "unknown_type"),
            (upsert_body(registrationNumber=119), "invalid_registration_number"),
            (
                body(type="company-upserted", updatedAt="2026-08-14T09:00:00+08:00", data={"companyName": "No Reg"}),
                "invalid_registration_number",
            ),
            (upsert_body(updatedAt=None), "invalid_updated_at"),
            (upsert_body(updatedAt="yesterday"), "invalid_updated_at"),
            (upsert_body(data="companyName=Foo; state=Perak"), "invalid_data"),
            (upsert_body(data=None), "invalid_data"),
            (upsert_body(data={"companyName": "Unknown"}), "invalid_data"),
            (
                upsert_body(data={"companyName": "", "natureOfBusiness": "x", "state": None, "industry": None}),
                "invalid_data",
            ),
            (
                upsert_body(data={"companyName": "A", "natureOfBusiness": 5, "state": None, "industry": None}),
                "invalid_data",
            ),
        ],
    )
    def test_rejections_carry_a_reason(self, raw: bytes, reason: str) -> None:
        with pytest.raises(InvalidEvent) as error:
            parse_event(raw)
        assert error.value.reason == reason

    def test_upsert_without_data_is_rejected(self) -> None:
        raw = body(type="company-upserted", registrationNumber="SYN-000175", updatedAt="2026-08-14T09:17:46+08:00")
        with pytest.raises(InvalidEvent) as error:
            parse_event(raw)
        assert error.value.reason == "invalid_data"


class TestRegistrationNumber:
    @pytest.mark.parametrize("raw", ["SYN-000251", " SYN-000251 ", "syn-000251"])
    def test_is_normalized(self, raw: str) -> None:
        assert parse_registration_number(raw) == "SYN-000251"

    @pytest.mark.parametrize("raw", [None, 119, "", "SYN-12", "ABC-000251", "SYN-0002510", True])
    def test_rejects_anything_else(self, raw: object) -> None:
        with pytest.raises(InvalidEvent):
            parse_registration_number(raw)


class TestTimestamp:
    @pytest.mark.parametrize(
        "raw",
        [
            "2026-08-14T09:03:35+08:00",
            "2026-08-14T01:03:35Z",
            "2026-08-14T09:03:35",  # no timezone: the registry's local time (MYT)
            1786669415000,  # epoch milliseconds
        ],
    )
    def test_every_format_means_the_same_instant(self, raw: object) -> None:
        assert parse_timestamp(raw) == datetime(2026, 8, 14, 1, 3, 35, tzinfo=UTC)

    def test_result_is_always_timezone_aware(self) -> None:
        assert parse_timestamp("2026-08-14T09:03:35").tzinfo is not None

    @pytest.mark.parametrize("raw", [None, "", "not a date", "2026-13-40T00:00:00Z", 1.5, True, [], -1])
    def test_rejects_anything_else(self, raw: object) -> None:
        with pytest.raises(InvalidEvent):
            parse_timestamp(raw)


class TestState:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("KUALA LUMPUR", "Kuala Lumpur"),
            ("pulau pinang", "Pulau Pinang"),
            (" Selangor ", "Selangor"),
            ("Negeri Sembilan", "Negeri Sembilan"),
            ("Atlantis", "Atlantis"),  # unknown values are kept, not invented
            (None, None),
            ("  ", None),
        ],
    )
    def test_normalize(self, raw: str | None, expected: str | None) -> None:
        assert normalize_state(raw) == expected


class TestDescription:
    def test_trims_whitespace(self) -> None:
        assert clean_description("  Hotel butik di Seremban ") == "Hotel butik di Seremban"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_empty_means_none(self, raw: str | None) -> None:
        assert clean_description(raw) is None

    def test_collapses_a_phrase_repeated_many_times(self) -> None:
        phrase = "Hotel butik di Seremban"
        assert clean_description(" ".join([phrase] * 60)) == phrase

    def test_keeps_text_that_only_partly_repeats(self) -> None:
        text = "Trading and trading services"
        assert clean_description(text) == text

    def test_needs_at_least_three_copies_to_collapse(self) -> None:
        assert clean_description("Bakery Bakery") == "Bakery Bakery"
