"""Publishing to a private test queue, the same way the registry's producer does."""

import hashlib
from collections.abc import Sequence

import aio_pika

from app.config import load_settings

TEST_QUEUE = "company-events-test"


async def publish(lines: Sequence[str], queue: str = TEST_QUEUE) -> None:
    """Like producer/publish.py: one persistent message per line, message_id = sha256(body)."""
    async with await aio_pika.connect_robust(load_settings().rabbitmq_url) as connection:
        channel = await connection.channel(publisher_confirms=True)
        await channel.declare_queue(queue, durable=True)
        for line in lines:
            message = aio_pika.Message(
                line.encode(),
                message_id=hashlib.sha256(line.encode()).hexdigest(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await channel.default_exchange.publish(message, routing_key=queue)


async def delete_queue(queue: str = TEST_QUEUE) -> None:
    async with await aio_pika.connect_robust(load_settings().rabbitmq_url) as connection:
        channel = await connection.channel()
        await channel.queue_delete(queue)
