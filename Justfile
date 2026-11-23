set shell := ["bash", "-euo", "pipefail", "-c"]

default: setup

# Install the producer's dependencies.
setup:
    uv sync --locked

# Start Postgres (with pgvector) and RabbitMQ, and wait until both are healthy.
up:
    docker compose up -d --wait

# Stop everything and delete the volumes.
down:
    docker compose down -v

# Replay the registry feed onto the company-events queue.
publish:
    uv run python -m producer.publish

# --- The similar-companies service ---

# Consume company-events into Postgres (Ctrl+C to stop). Downloads the embedding model (~65 MB) on first run.
consumer *args:
    uv run python -m app.consumer {{args}}

# Serve the API on http://127.0.0.1:${API_PORT:-8000} (docs at /docs).
serve:
    uv run uvicorn app.api:app --factory --host 127.0.0.1 --port "${API_PORT:-8000}"

# Run every test. Integration tests need `just up` first; they use their own database and queue.
test:
    uv run pytest

# Run only the tests that need no Docker.
test-unit:
    uv run pytest -m "not integration"

# Lint and check formatting.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Measure p50/p95/p99 latency of the endpoint (needs `just serve` running and the feed consumed).
bench:
    uv run python -m scripts.bench_latency

# Compare embedding models on recommendation quality (needs the feed consumed).
evaluate:
    uv run python -m scripts.evaluate_quality
