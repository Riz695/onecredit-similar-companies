"""Fixtures for tests against the real Postgres and RabbitMQ started by `just up`.

Tests use their own database (`companies_test`), so they never touch the data behind `just serve`.
"""

import os
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg import AsyncConnection, sql

from app.db import connect, ensure_schema

PORT = os.environ.get("POSTGRES_PORT", "5432")
ADMIN_URL = f"postgresql://postgres:postgres@localhost:{PORT}/postgres"
TEST_DATABASE = "companies_test"
TEST_DATABASE_URL = f"postgresql://postgres:postgres@localhost:{PORT}/{TEST_DATABASE}"


@pytest.fixture(scope="session")
async def database_url() -> str:
    """Recreate the test database once per test run."""
    async with await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True) as admin:
        name = sql.Identifier(TEST_DATABASE)
        await admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(name))
    await ensure_schema(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
async def db(database_url: str) -> AsyncIterator[AsyncConnection]:
    """A connection to an empty test database."""
    connection = await connect(database_url)
    await connection.execute("TRUNCATE companies, processed_messages, rejected_events")
    yield connection
    await connection.close()
