"""The consumer against real RabbitMQ: crafted duplicates, out-of-order and malformed messages."""

import asyncio
import json

import pytest
from psycopg import AsyncConnection

from app.config import load_settings
from app.consumer import consume
from tests.fakes import FakeEmbedder
from tests.integration.amqp import TEST_QUEUE, delete_queue, publish


def event(number: object, updated_at: object, description: str | None, **extra: object) -> str:
    data = {"companyName": f"Company {number}", "natureOfBusiness": description, "state": "Johor", "industry": None}
    return json.dumps(
        {"type": "company-upserted", "registrationNumber": number, "updatedAt": updated_at, "data": data, **extra}
    )


# Each problem seen in the real feed, in miniature.
CRAFTED = [
    event("SYN-000001", "2026-08-14T09:00:00+08:00", "Coffee shops"),
    event("SYN-000001", "2026-08-14T09:00:00+08:00", "Coffee shops"),  # duplicate delivery
    event("SYN-000002", "2026-08-14T12:00:00+08:00", "Cold chain logistics"),  # newer...
    event("SYN-000002", "2026-08-14T10:00:00+08:00", "Trucking"),  # ...then older arrives late
    event(" syn-000003 ", "2026-08-14T01:00:00Z", "Bakery", source="ssm-batch"),  # messy number, UTC
    event("SYN-000003", 1786672800000, "Bakery and cafe"),  # epoch ms for 02:00Z: an hour newer
    event("SYN-000004", "2026-08-14T09:00:00", None),  # no description, naive timestamp
    json.dumps({"type": "company-struck-off", "registrationNumber": "SYN-000004", "updatedAt": "2026-08-14T18:00:00Z"}),
    "null",
    '{"type": "company-upserted", "registrationNumber": "SYN-000005", "data": {"companyName": "Bes',
    json.dumps({"type": "company-upserted", "registrationNumber": "SYN-000006", "data": {"companyName": "Unknown"}}),
    event(119, "2026-08-14T09:00:00+08:00", "Trading"),  # integer registration number
]

EXPECTED_COMPANIES = [
    ("SYN-000001", "Coffee shops", "active"),
    ("SYN-000002", "Cold chain logistics", "active"),
    ("SYN-000003", "Bakery and cafe", "active"),
    ("SYN-000004", None, "struck_off"),
]


@pytest.fixture(autouse=True)
async def empty_queue() -> None:
    await delete_queue()


async def run_until_idle(database_url: str) -> None:
    await consume(database_url, load_settings().rabbitmq_url, FakeEmbedder(), queue_name=TEST_QUEUE, idle_timeout=1.5)


async def companies(db: AsyncConnection) -> list[tuple]:
    cursor = await db.execute("SELECT registration_number, description, status FROM companies ORDER BY 1")
    return await cursor.fetchall()


async def rejected_count(db: AsyncConnection) -> int:
    cursor = await db.execute("SELECT count(*) FROM rejected_events")
    row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def test_crafted_feed_ends_in_the_expected_state(db: AsyncConnection, database_url: str) -> None:
    await publish(CRAFTED)
    await run_until_idle(database_url)
    assert await companies(db) == EXPECTED_COMPANIES
    assert await rejected_count(db) == 4


async def test_publishing_twice_changes_nothing(db: AsyncConnection, database_url: str) -> None:
    await publish(CRAFTED)
    await run_until_idle(database_url)
    await publish(CRAFTED)
    await run_until_idle(database_url)
    assert await companies(db) == EXPECTED_COMPANIES
    assert await rejected_count(db) == 4


async def test_consumer_killed_mid_feed_recovers_on_restart(db: AsyncConnection, database_url: str) -> None:
    await publish(CRAFTED * 20)  # enough messages that the consumer is still busy when killed
    task = asyncio.create_task(run_until_idle(database_url))
    while not await companies(db):
        await asyncio.sleep(0.01)
    task.cancel()  # like Ctrl+C or a crash: unacked deliveries go back to the queue
    with pytest.raises(asyncio.CancelledError):
        await task
    await run_until_idle(database_url)
    assert await companies(db) == EXPECTED_COMPANIES
    assert await rejected_count(db) == 4
