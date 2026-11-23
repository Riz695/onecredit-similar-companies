"""How the consumer treats one message: what it stores, and when it acks."""

import hashlib
import json
from dataclasses import dataclass, field

import psycopg
import pytest
from psycopg import AsyncConnection

from app.consumer import handle_message
from app.db import connect
from tests.fakes import FakeEmbedder

VALID = json.dumps(
    {
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
).encode()


@dataclass
class FakeMessage:
    """Records what the consumer did with it, like an AMQP delivery."""

    body: bytes
    message_id: str | None = "m1"
    redelivered: bool = False
    actions: list[str] = field(default_factory=list)
    on_ack: object = None

    async def ack(self) -> None:
        if callable(self.on_ack):
            await self.on_ack()
        self.actions.append("ack")

    async def nack(self, requeue: bool = True) -> None:
        self.actions.append(f"nack(requeue={requeue})")


class ExplodingEmbedder:
    dimensions = 384

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model crashed")


async def scalar(db: AsyncConnection, query: str) -> object:
    cursor = await db.execute(query)
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def test_valid_message_is_stored_then_acked(db: AsyncConnection, database_url: str) -> None:
    observer = await connect(database_url)  # a separate connection only sees committed data

    async def assert_committed() -> None:
        assert await scalar(observer, "SELECT count(*) FROM companies") == 1

    message = FakeMessage(VALID, on_ack=assert_committed)
    assert await handle_message(message, db, FakeEmbedder()) == "applied"
    assert message.actions == ["ack"]
    await observer.close()


async def test_malformed_message_is_recorded_and_acked(db: AsyncConnection) -> None:
    message = FakeMessage(b"null")
    assert await handle_message(message, db, FakeEmbedder()) == "rejected"
    assert message.actions == ["ack"]
    assert await scalar(db, "SELECT reason FROM rejected_events") == "not_an_object"
    assert await scalar(db, "SELECT count(*) FROM companies") == 0


async def test_duplicate_delivery_is_acked_without_changes(db: AsyncConnection) -> None:
    embedder = FakeEmbedder()
    await handle_message(FakeMessage(VALID), db, embedder)
    again = FakeMessage(VALID, redelivered=True)
    assert await handle_message(again, db, embedder) == "duplicate"
    assert again.actions == ["ack"]
    assert await scalar(db, "SELECT count(*) FROM companies") == 1


async def test_company_without_description_is_stored_without_calling_the_model(db: AsyncConnection) -> None:
    event = json.loads(VALID)
    event["data"]["natureOfBusiness"] = None
    embedder = FakeEmbedder()
    assert await handle_message(FakeMessage(json.dumps(event).encode()), db, embedder) == "applied"
    assert embedder.calls == []
    assert await scalar(db, "SELECT embedding IS NULL FROM companies") is True


async def test_missing_message_id_falls_back_to_a_hash_of_the_body(db: AsyncConnection) -> None:
    await handle_message(FakeMessage(VALID, message_id=None), db, FakeEmbedder())
    expected = hashlib.sha256(VALID).hexdigest()
    assert await scalar(db, "SELECT last_message_id FROM companies") == expected


async def test_database_outage_requeues_and_never_acks(database_url: str) -> None:
    broken = await connect(database_url)
    await broken.close()
    message = FakeMessage(VALID)
    with pytest.raises(psycopg.OperationalError):
        await handle_message(message, broken, FakeEmbedder())
    assert message.actions == ["nack(requeue=True)"]


async def test_unexpected_error_is_retried_once(db: AsyncConnection) -> None:
    message = FakeMessage(VALID)
    assert await handle_message(message, db, ExplodingEmbedder()) == "requeued"
    assert message.actions == ["nack(requeue=True)"]
    assert await scalar(db, "SELECT count(*) FROM rejected_events") == 0


async def test_unexpected_error_on_redelivery_is_recorded_and_acked(db: AsyncConnection) -> None:
    message = FakeMessage(VALID, redelivered=True)
    assert await handle_message(message, db, ExplodingEmbedder()) == "rejected"
    assert message.actions == ["ack"]
    assert await scalar(db, "SELECT reason FROM rejected_events") == "processing_error"
