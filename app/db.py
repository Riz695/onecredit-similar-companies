"""Postgres connections with pgvector support, and the schema."""

from pathlib import Path

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


async def ensure_schema(database_url: str) -> None:
    """Create the extension, tables and index if they do not exist yet."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(SCHEMA_SQL)


async def connect(database_url: str) -> AsyncConnection:
    """One connection that sends and receives `vector` values as Python lists / numpy arrays."""
    connection = await AsyncConnection.connect(database_url, autocommit=True)
    await register_vector_async(connection)
    return connection


def create_pool(database_url: str) -> AsyncConnectionPool:
    """A connection pool for the API. Open it with `await pool.open()`."""

    async def configure(connection: AsyncConnection) -> None:
        await register_vector_async(connection)
        await connection.set_autocommit(True)

    return AsyncConnectionPool(database_url, min_size=1, max_size=10, configure=configure, open=False)
