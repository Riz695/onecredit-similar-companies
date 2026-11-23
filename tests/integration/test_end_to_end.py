"""The whole registry feed through RabbitMQ, the consumer and the API."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from psycopg import AsyncConnection

from app.api import create_app
from app.config import load_settings
from app.consumer import consume
from app.db import connect
from tests.fakes import FakeEmbedder
from tests.integration.amqp import TEST_QUEUE, delete_queue, publish

FEED = Path(__file__).parents[2] / "data" / "feed.jsonl"


async def rows(db: AsyncConnection, query: str) -> list[tuple]:
    cursor = await db.execute(query)
    return await cursor.fetchall()


@pytest.fixture(scope="module")
async def db(database_url: str) -> AsyncIterator[AsyncConnection]:
    """The feed published twice (a replay) and consumed until the queue is empty, once for this module."""
    connection = await connect(database_url)
    await connection.execute("TRUNCATE companies, processed_messages, rejected_events")
    await delete_queue()
    lines = FEED.read_text(encoding="utf-8").splitlines()
    await publish(lines)
    await publish(lines)
    rabbitmq_url = load_settings().rabbitmq_url
    await consume(database_url, rabbitmq_url, FakeEmbedder(), queue_name=TEST_QUEUE, idle_timeout=2)
    yield connection
    await connection.close()


async def test_every_company_in_the_feed_is_stored_once(db: AsyncConnection) -> None:
    # Numbers from the feed analysis in docs/decisions.md.
    assert await rows(db, "SELECT status, count(*) FROM companies GROUP BY 1 ORDER BY 1") == [
        ("active", 1992),
        ("struck_off", 8),
    ]
    assert await rows(db, "SELECT count(*) FROM companies WHERE embedding IS NULL") == [(39,)]
    # 37 rejected lines, but the 6 `null` lines are identical and so share one message_id.
    assert await rows(db, "SELECT count(*) FROM rejected_events") == [(32,)]


async def test_late_older_events_did_not_win(db: AsyncConnection) -> None:
    # SYN-000675 receives 20:38 and then 14:49 (out of order); the 20:38 description must stay.
    assert await rows(db, "SELECT description FROM companies WHERE registration_number = 'SYN-000675'") == [
        ("Cold chain transport and storage of frozen goods",)
    ]


async def test_api_serves_every_stored_company(db: AsyncConnection, database_url: str) -> None:
    numbers = [number for (number,) in await rows(db, "SELECT registration_number FROM companies")]
    app = create_app(database_url)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for number in numbers:
            response = await client.get(f"/companies/{number}/similar")
            assert response.status_code == 200, number
            body = response.json()
            assert body["reason"] is not None or len(body["similar"]) == 4, number
