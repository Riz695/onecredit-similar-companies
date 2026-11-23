"""Settings from the environment, with defaults that match docker-compose.yml."""

import os
from dataclasses import dataclass

QUEUE = "company-events"


@dataclass(frozen=True)
class Settings:
    database_url: str
    rabbitmq_url: str
    embedding_model: str


def load_settings() -> Settings:
    postgres_port = os.environ.get("POSTGRES_PORT", "5432")
    rabbitmq_port = os.environ.get("RABBITMQ_PORT", "5672")
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", f"postgresql://postgres:postgres@localhost:{postgres_port}/companies"
        ),
        rabbitmq_url=os.environ.get("RABBITMQ_URL", f"amqp://guest:guest@localhost:{rabbitmq_port}/"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"),
    )
