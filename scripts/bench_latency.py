"""Latency of the similar-companies endpoint, measured from a client like the portal.

Start the service first (`just serve`), then run `just bench`. Every company in the database is requested
in random order, one request at a time and then with several in flight at once.
"""

import argparse
import asyncio
import random
import statistics
import time

import httpx
import psycopg

from app.config import load_settings


async def timed_get(client: httpx.AsyncClient, number: str) -> float:
    start = time.perf_counter()
    response = await client.get(f"/companies/{number}/similar")
    elapsed = time.perf_counter() - start
    response.raise_for_status()
    return elapsed * 1000


async def run(base_url: str, numbers: list[str], concurrency: int) -> list[float]:
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        await timed_get(client, numbers[0])  # warm up the connection and the pool

        async def one(number: str) -> float:
            async with semaphore:
                return await timed_get(client, number)

        return await asyncio.gather(*(one(number) for number in numbers))


def report(label: str, latencies: list[float], seconds: float) -> None:
    cuts = statistics.quantiles(latencies, n=100)  # cuts[49] is p50, cuts[94] p95, cuts[98] p99
    throughput = len(latencies) / seconds
    print(f"| {label} | {len(latencies)} | {cuts[49]:.1f} | {cuts[94]:.1f} | {cuts[98]:.1f} | {throughput:.0f} |")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with psycopg.connect(load_settings().database_url) as connection:
        numbers = [row[0] for row in connection.execute("SELECT registration_number FROM companies")]
    random.Random(0).shuffle(numbers)

    print("| Load | Requests | p50 ms | p95 ms | p99 ms | Requests/s |")
    print("|---|---|---|---|---|---|")
    for concurrency in (1, 10):
        start = time.perf_counter()
        latencies = asyncio.run(run(args.url, numbers, concurrency))
        report(f"{concurrency} in flight", latencies, time.perf_counter() - start)


if __name__ == "__main__":
    main()
