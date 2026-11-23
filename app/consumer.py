"""Reads `company-events` and keeps the `companies` table at each company's latest state.

Run with `just consumer`. Delivery rules:

- A message is acked only after its database transaction has committed, so a crash at any point
  leaves it in the queue to be redelivered.
- A message we cannot parse is stored in `rejected_events` and acked, so it never blocks the queue.
  (The registry owns the queue, so we cannot give it a dead-letter exchange.)
- If the database is unreachable, the message is requeued and the consumer reconnects.
- Any other error gets one retry; if the redelivery fails too, it is stored as rejected and acked.
"""

import argparse
import asyncio
import hashlib
import logging
from collections import Counter
from typing import Protocol

import aio_pika
import psycopg
from psycopg import AsyncConnection

from app.config import QUEUE, load_settings
from app.db import connect, ensure_schema
from app.embedding import Embedder, FastEmbedder
from app.events import CompanyUpserted, InvalidEvent, parse_event
from app.repository import apply_event, record_rejection

log = logging.getLogger("consumer")

PREFETCH = 32  # unacked messages RabbitMQ may hand us at once
RECONNECT_DELAY_SECONDS = 2.0
LOG_EVERY = 500


class IncomingMessage(Protocol):
    """The part of an AMQP delivery (`aio_pika.abc.AbstractIncomingMessage`) the consumer uses."""

    body: bytes
    message_id: str | None
    redelivered: bool | None

    async def ack(self) -> None: ...

    async def nack(self, requeue: bool = True) -> None: ...


async def handle_message(message: IncomingMessage, connection: AsyncConnection, embedder: Embedder) -> str:
    """Process one delivery and settle it. Returns the outcome: applied, stale, duplicate, rejected, requeued."""
    # The registry always sets message_id; the hash is a fallback with the same meaning (sha256 of the body).
    message_id = message.message_id or hashlib.sha256(message.body).hexdigest()
    try:
        outcome = await _process(message, message_id, connection, embedder)
    except psycopg.OperationalError:
        await message.nack(requeue=True)  # database down: keep the message and let the caller reconnect
        raise
    if outcome == "requeued":
        await message.nack(requeue=True)
    else:
        await message.ack()  # only reached after the database work above has committed
    return outcome


async def _process(message: IncomingMessage, message_id: str, connection: AsyncConnection, embedder: Embedder) -> str:
    """Store the message, giving an unexpected error (a bug, a model crash) one retry before rejecting it."""
    try:
        return await _store(message.body, message_id, connection, embedder)
    except psycopg.OperationalError:
        raise
    except Exception as error:
        if not message.redelivered:
            log.warning("message %s failed, requeueing once: %r", message_id, error)
            return "requeued"
        log.error("message %s failed again, storing as rejected: %r", message_id, error)
        await record_rejection(connection, message_id, message.body, InvalidEvent("processing_error", repr(error)))
        return "rejected"


async def _store(body: bytes, message_id: str, connection: AsyncConnection, embedder: Embedder) -> str:
    try:
        event = parse_event(body)
    except InvalidEvent as error:
        log.info("rejected message %s: %s", message_id, error)
        await record_rejection(connection, message_id, body, error)
        return "rejected"

    embedding = None
    if isinstance(event, CompanyUpserted) and event.description:
        # The model is CPU-bound; run it in a thread so the AMQP connection keeps its heartbeats.
        [embedding] = await asyncio.to_thread(embedder.embed, [event.description])
    return await apply_event(connection, message_id, event, embedding)


async def consume(
    database_url: str,
    rabbitmq_url: str,
    embedder: Embedder,
    queue_name: str = QUEUE,
    idle_timeout: float | None = None,
) -> Counter[str]:
    """Consume until cancelled, or until no message has arrived for `idle_timeout` seconds."""
    outcomes: Counter[str] = Counter()
    connection = await connect(database_url)
    try:
        async with await aio_pika.connect_robust(rabbitmq_url) as rabbit:
            channel = await rabbit.channel()
            await channel.set_qos(prefetch_count=PREFETCH)
            # Same declaration as the registry's (durable, no arguments), so RabbitMQ accepts it; declaring
            # also means the consumer can start before the first publish.
            queue = await channel.declare_queue(queue_name, durable=True)
            try:
                async with queue.iterator(timeout=idle_timeout) as messages:
                    async for message in messages:
                        try:
                            outcomes[await handle_message(message, connection, embedder)] += 1
                        except psycopg.OperationalError:
                            log.exception("database unavailable; message requeued, reconnecting")
                            connection = await _reconnect(connection, database_url)
                        if outcomes.total() % LOG_EVERY == 0:
                            log.info("progress: %s", dict(outcomes))
            except TimeoutError:
                log.info("no message for %s seconds, stopping", idle_timeout)
    finally:
        await connection.close()
    log.info("done: %s", dict(outcomes))
    return outcomes


async def _reconnect(old: AsyncConnection, database_url: str) -> AsyncConnection:
    await old.close()
    while True:
        await asyncio.sleep(RECONNECT_DELAY_SECONDS)
        try:
            return await connect(database_url)
        except psycopg.OperationalError:
            log.warning("database still unavailable, retrying in %s seconds", RECONNECT_DELAY_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Consume company-events into Postgres.")
    parser.add_argument(
        "--exit-when-idle",
        type=float,
        metavar="SECONDS",
        help="stop once the queue has been empty for this long (default: run until Ctrl+C)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    asyncio.run(ensure_schema(settings.database_url))
    log.info("loading embedding model %s", settings.embedding_model)
    embedder = FastEmbedder(settings.embedding_model)
    try:
        asyncio.run(consume(settings.database_url, settings.rabbitmq_url, embedder, idle_timeout=args.exit_when_idle))
    except KeyboardInterrupt:
        log.info("stopped; unacked messages stay in the queue")


if __name__ == "__main__":
    main()
