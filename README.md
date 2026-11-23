# Similar companies

The service behind the OneCredit portal's **"Similar companies"** panel. This is my solution to the OneCredit MLE take-home
(`mle-v2026.11`). The original brief is in [`docs/assessment-brief.md`](docs/assessment-brief.md), and the reasoning is in
[`DESIGN.md`](DESIGN.md).

**▶ Video demo (5 min):** https://youtu.be/Sz0npbm7Udc

- A **consumer** reads the registry's `company-events` queue, validates each message, embeds the business description,
  and keeps one row per company in Postgres with pgvector. Each row always holds the company's latest state by `updatedAt`.
- A **service** answers `GET /companies/{registrationNumber}/similar` with the most similar companies first. The search runs
  in Postgres on an HNSW index.

```
registry feed ──▶ RabbitMQ ──▶ consumer ──▶ Postgres + pgvector ◀── service ◀── portal
                 company-events   │  (validate, embed,    companies              GET /companies/{n}/similar
                                  │   latest-wins upsert)  rejected_events
                                  ▼                         processed_messages
                         BAAI/bge-small-en-v1.5 (CPU)
```

## Run it

You need Docker, [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just). Run every command from the
repository root.

```bash
just setup       # install dependencies (Python 3.14 via uv)
just up          # start Postgres (pgvector) and RabbitMQ
just publish     # publish the registry feed to the queue
just consumer    # terminal 1: consume the queue into Postgres (Ctrl+C to stop)
just serve       # terminal 2: serve the API on http://127.0.0.1:8000
```

- On its first run the consumer downloads the embedding model (about 65 MB) into the fastembed cache. Later runs use the
  cache.
- The whole feed takes about 75 seconds to consume on a laptop. The consumer logs progress every 500 messages and keeps
  waiting for new events. To stop it automatically once the queue is empty, run `just consumer --exit-when-idle 5`.
- The consumer and the service can start in any order, before or after `just publish`. Each one creates the tables if they
  are missing.
- Running `just publish` again, or stopping and restarting the consumer at any point, gives the same final state.
- To start again from empty, run `just down` and then `just up`.

Try it:

```bash
curl -s http://127.0.0.1:8000/companies/SYN-001550/similar | python3 -m json.tool
```

The interactive API docs are at http://127.0.0.1:8000/docs.

A terminal recording of these exact steps, run from a fresh clone, is in
[`docs/demo/readme-run.cast`](docs/demo/readme-run.cast). It covers the consumer, the API and its errors, a replay of the
feed, lint and the full test suite. Play it with `asciinema play docs/demo/readme-run.cast`.

If a port is taken, export `POSTGRES_PORT`, `RABBITMQ_PORT` or `API_PORT` before running the commands above. The consumer,
the service and the tests read the same variables.

## API

### `GET /companies/{registrationNumber}/similar?limit=4`

The registration number is case-insensitive (`syn-001550` works). `limit` must be between 1 and 20; the default is 4,
like the mock.

```json
{
  "company": {
    "registrationNumber": "SYN-001550",
    "companyName": "Sinar Teguh Bakery Sdn. Bhd.",
    "description": "Operating cafes and coffee shops in the Klang Valley",
    "industry": "Food & Beverage",
    "state": "Selangor",
    "status": "active"
  },
  "similar": [
    {
      "registrationNumber": "SYN-000539",
      "companyName": "Seri Sejahtera Food Sdn. Bhd.",
      "industry": "Food & Beverage",
      "state": "Johor",
      "similarity": 1.0
    }
  ],
  "reason": null
}
```

- `similar` is ordered most similar first. `similarity` is the cosine similarity: 1.0 means the same meaning, and
  unrelated texts score lower.
- Struck-off companies still have their own page (`status: "struck_off"`), but they are never recommended.
- A company without a business description returns `200` with `"similar": []` and `"reason": "no_description"`.

Every error has the same body:

| Status | `error.code` | When |
|---|---|---|
| 404 | `company_not_found` | No company has that registration number. |
| 422 | `invalid_parameter` | `limit` is out of range or not a number, or the registration number is not `SYN-` followed by six digits. |
| 404 | `not_found` | Unknown URL. |
| 500 | `internal_error` | Anything unexpected. The details are logged. |

```json
{"error": {"code": "company_not_found", "message": "No company with registration number SYN-999999."}}
```

### `GET /health`

Returns `{"status": "ok"}` when the service can reach Postgres.

## Tests and tools

```bash
just test        # all tests; the integration ones need `just up`
just test-unit   # only the tests that need no Docker
just lint        # ruff check and format check
just bench       # p50/p95/p99 latency (needs `just serve` and a consumed feed)
just evaluate    # compare embedding models on recommendation quality (needs a consumed feed)
```

The integration tests use their own database (`companies_test`) and queue (`company-events-test`), so they never touch the
data behind `just serve`. They use a deterministic fake embedder, so they need no model download.

## Configuration

| Variable | Default |
|---|---|
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:${POSTGRES_PORT:-5432}/companies` |
| `RABBITMQ_URL` | `amqp://guest:guest@localhost:${RABBITMQ_PORT:-5672}/` |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` (any 384-dimension fastembed model works) |
| `API_PORT` | `8000` |

These defaults are the local Docker Compose credentials from the assessment. They are not secrets.

## Layout

```
app/
  events.py      parse, validate and normalise one raw message
  repository.py  all SQL: latest-wins upsert, rejects, similarity query
  consumer.py    RabbitMQ loop: ack after commit, retries, rejects
  api.py         FastAPI service and error responses
  embedding.py   Embedder protocol and the fastembed model
  db.py          connections, pgvector registration, HNSW settings
  schema.sql     tables and the HNSW index (idempotent)
  config.py      settings from the environment
scripts/         latency benchmark and quality evaluation
tests/unit/      parsing rules (no Docker)
tests/integration/  Postgres and RabbitMQ: upserts, consumer, API, full feed end to end
docs/            decision log and the original assessment brief
```
