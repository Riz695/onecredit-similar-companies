"""Postgres connections with pgvector support, and the schema."""

from pathlib import Path

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.repository import CANDIDATE_POOL

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text()


async def ensure_schema(database_url: str) -> None:
    """Create the extension, tables and index if they do not exist yet."""
    async with await AsyncConnection.connect(database_url, autocommit=True) as connection:
        await connection.execute(SCHEMA_SQL)


async def prepare(connection: AsyncConnection) -> None:
    """Teach psycopg the `vector` type, and tune HNSW searches for the similarity query.

    `ef_search` is how many candidates one HNSW scan keeps (default 40); it must cover the candidate pool.
    `iterative_scan` lets the index keep scanning when the WHERE filter (active, has an embedding) drops rows,
    so a filtered query still returns enough neighbours.
    """
    await connection.set_autocommit(True)
    await register_vector_async(connection)
    await connection.execute(f"SET hnsw.ef_search = {CANDIDATE_POOL}")
    await connection.execute("SET hnsw.iterative_scan = relaxed_order")


async def connect(database_url: str) -> AsyncConnection:
    connection = await AsyncConnection.connect(database_url)
    await prepare(connection)
    return connection


def create_pool(database_url: str) -> AsyncConnectionPool:
    """A connection pool for the API. Open it with `await pool.open()`."""
    return AsyncConnectionPool(database_url, min_size=1, max_size=10, configure=prepare, open=False)
