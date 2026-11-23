"""All SQL lives here: storing events, recording rejects, and the similarity search."""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from psycopg import AsyncConnection

from app.events import CompanyEvent, CompanyUpserted, InvalidEvent

# Nearest neighbours fetched through the HNSW index before the exact re-sort (see `find_similar`).
CANDIDATE_POOL = 100


class ApplyResult(StrEnum):
    APPLIED = "applied"  # the event is now the company's latest state
    STALE = "stale"  # we already hold a state at least as new; nothing changed
    DUPLICATE = "duplicate"  # this message_id was processed before; nothing changed


@dataclass(frozen=True)
class Company:
    registration_number: str
    company_name: str | None
    description: str | None
    industry: str | None
    state: str | None
    status: str
    has_embedding: bool


@dataclass(frozen=True)
class SimilarCompany:
    registration_number: str
    company_name: str | None
    industry: str | None
    state: str | None
    similarity: float


# Latest-wins is decided by Postgres inside the upsert: the row is only updated when the stored
# `source_updated_at` is strictly older than the incoming one. Two consumers racing on the same company
# are serialised by the row lock that ON CONFLICT takes, so no read-then-write race is possible.
UPSERT_COMPANY = """
INSERT INTO companies AS stored (
    registration_number, company_name, description, industry, state, status,
    embedding, source_updated_at, last_message_id
)
VALUES (%(registration_number)s, %(company_name)s, %(description)s, %(industry)s, %(state)s, 'active',
        %(embedding)s, %(updated_at)s, %(message_id)s)
ON CONFLICT (registration_number) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    description = EXCLUDED.description,
    industry = EXCLUDED.industry,
    state = EXCLUDED.state,
    status = 'active',
    embedding = EXCLUDED.embedding,
    source_updated_at = EXCLUDED.source_updated_at,
    last_message_id = EXCLUDED.last_message_id,
    stored_at = now()
WHERE stored.source_updated_at < EXCLUDED.source_updated_at
RETURNING registration_number
"""

# A struck-off notice only changes the status, so the company's last known details stay on its page.
STRIKE_OFF_COMPANY = """
INSERT INTO companies AS stored (registration_number, status, source_updated_at, last_message_id)
VALUES (%(registration_number)s, 'struck_off', %(updated_at)s, %(message_id)s)
ON CONFLICT (registration_number) DO UPDATE SET
    status = 'struck_off',
    source_updated_at = EXCLUDED.source_updated_at,
    last_message_id = EXCLUDED.last_message_id,
    stored_at = now()
WHERE stored.source_updated_at < EXCLUDED.source_updated_at
RETURNING registration_number
"""


async def apply_event(
    connection: AsyncConnection,
    message_id: str,
    event: CompanyEvent,
    embedding: Sequence[float] | None,
) -> ApplyResult:
    """Store an event in one transaction: mark the message processed, then apply it if it is newer."""
    async with connection.transaction():
        cursor = await connection.execute(
            "INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING message_id",
            (message_id,),
        )
        if await cursor.fetchone() is None:
            return ApplyResult.DUPLICATE

        parameters: dict[str, object] = {
            "registration_number": event.registration_number,
            "updated_at": event.updated_at,
            "message_id": message_id,
        }
        if isinstance(event, CompanyUpserted):
            parameters |= {
                "company_name": event.company_name,
                "description": event.description,
                "industry": event.industry,
                "state": event.state,
                "embedding": list(embedding) if embedding is not None else None,
            }
            cursor = await connection.execute(UPSERT_COMPANY, parameters)
        else:
            cursor = await connection.execute(STRIKE_OFF_COMPANY, parameters)
        applied = await cursor.fetchone() is not None
    return ApplyResult.APPLIED if applied else ApplyResult.STALE


async def record_rejection(connection: AsyncConnection, message_id: str, body: bytes, error: InvalidEvent) -> None:
    """Keep a message we could not apply, with why. A redelivery of the same message is stored once."""
    await connection.execute(
        "INSERT INTO rejected_events (message_id, reason, detail, body) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (message_id, error.reason, str(error), body.decode("utf-8", errors="replace")),
    )


async def get_company(connection: AsyncConnection, registration_number: str) -> Company | None:
    cursor = await connection.execute(
        "SELECT registration_number, company_name, description, industry, state, status,"
        " embedding IS NOT NULL FROM companies WHERE registration_number = %s",
        (registration_number,),
    )
    row = await cursor.fetchone()
    return Company(*row) if row else None


# The inner query lets the HNSW index find the nearest candidates (it can only serve a plain
# `ORDER BY embedding <=> constant LIMIT n`). The outer query re-sorts that small set exactly and breaks
# ties by registration number, since many companies share an identical description.
# `similarity` is 1 - cosine distance: 1 means the same direction, 0 means unrelated.
FIND_SIMILAR = """
SELECT registration_number, company_name, industry, state, 1 - distance AS similarity
FROM (
    SELECT registration_number, company_name, industry, state,
           embedding <=> (SELECT embedding FROM companies WHERE registration_number = %(target)s) AS distance
    FROM companies
    WHERE status = 'active'
      AND embedding IS NOT NULL
      AND registration_number <> %(target)s
    ORDER BY embedding <=> (SELECT embedding FROM companies WHERE registration_number = %(target)s)
    LIMIT %(pool)s
) AS candidates
WHERE distance IS NOT NULL
ORDER BY distance, registration_number
LIMIT %(limit)s
"""


async def find_similar(connection: AsyncConnection, registration_number: str, limit: int) -> list[SimilarCompany]:
    """The most similar active companies, most similar first. Empty if the company has no embedding."""
    cursor = await connection.execute(
        FIND_SIMILAR,
        {"target": registration_number, "pool": max(CANDIDATE_POOL, limit), "limit": limit},
    )
    return [SimilarCompany(*row) for row in await cursor.fetchall()]
