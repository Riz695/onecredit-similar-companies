# Design

How the "Similar companies" service works, why it works that way, and what I would do next. The full decision log,
including the raw numbers, is in [`docs/decisions.md`](docs/decisions.md).

## 1. What the feed actually contains

Before designing anything, I analysed `data/feed.jsonl`. "Unvalidated and at-least-once" turned out to mean all of
this:

| Problem | Count (of 2,518 lines) | How it is handled |
|---|---|---|
| Not valid JSON (truncated) | 5 | rejected: `invalid_json` |
| The literal `null` | 6 (one identical line) | rejected: `not_an_object` |
| Exact duplicate lines (same `message_id`, since `message_id = sha256(line)`) | 68 extra copies | skipped through `processed_messages` |
| Older event arriving after a newer one for the same company | 66 | ignored by the latest-wins upsert |
| `registrationNumber` with spaces or in lowercase | 49 | normalised (`" syn-000251 "` → `SYN-000251`) |
| `registrationNumber` missing or an integer | 10 | rejected: `invalid_registration_number` |
| `updatedAt` as `Z` / epoch milliseconds / no timezone | 28 / 30 / 14 | parsed as UTC / ms / Malaysia time (+08:00) |
| `updatedAt` missing | 7 | rejected: `invalid_updated_at` |
| `data` as a string, missing, or only `companyName` | 9 that are not already rejected for another reason | rejected: `invalid_data` |
| `natureOfBusiness` null or empty | 44 (39 companies never have one) | company stored without an embedding |
| `industry` / `state` null | 120 / 60 | stored as null |
| `state` in inconsistent case (`KUALA LUMPUR`) | ~500 | mapped onto the 16 canonical names |
| One phrase repeated up to 3,359 characters | 7 | collapsed to one copy |
| `company-struck-off` events | 8 | company kept, status `struck_off` |
| Extra fields `source` / `ingestedBy` | 75 | ignored |

**Result after one `just publish`:** all 2,000 companies are stored (1,992 active and 8 struck off), 32 distinct
messages are in `rejected_events`, and no company is lost. Every rejected message is either junk ("X", "Foo", "No Reg
Sdn. Bhd.") or a stub for a company that also has a valid event. The end-to-end test checks these numbers.

## 2. Decisions

### Storage

```sql
companies(registration_number PK, company_name, description, industry, state,
          status 'active'|'struck_off', embedding vector(384) NULL,
          source_updated_at timestamptz, last_message_id, stored_at)
processed_messages(message_id PK)          -- every committed message
rejected_events(message_id PK, reason, detail, body, rejected_at)
```

There is one row per company with its latest state. I keep no event history, because the portal only needs the latest
state and it keeps the similarity query simple. `source_updated_at` is the source time (`updatedAt`), not the arrival
time.

### Idempotency and ordering, enforced by the database

Each message is handled in **one transaction**:

1. `INSERT INTO processed_messages … ON CONFLICT DO NOTHING RETURNING`. If nothing is returned, this `message_id` has
   already been processed, so the consumer stops here and acks.
2. Conditional upsert:
   ```sql
   INSERT INTO companies … ON CONFLICT (registration_number) DO UPDATE SET …
   WHERE stored.source_updated_at < EXCLUDED.source_updated_at
   ```
   An older event changes nothing. Postgres makes the decision while holding the row lock, so there is no read-then-write
   race, even with several consumers.

The two mechanisms overlap on purpose. The timestamp guard alone would already make a replay harmless. The
`processed_messages` table also lets the consumer tell "already seen" apart from "stale", and it protects events whose
replay would otherwise be ambiguous, such as a struck-off notice.

- When two events have the **same timestamp**, the first arrival wins. The feed has no conflicting ties.
- A **struck-off** notice changes only the status, so the company page keeps its last known details. A later upsert with a
  newer `updatedAt` makes the company active again. An older one cannot.

### Delivery: ack after commit, poison messages, retries

- The consumer **acks only after the transaction has committed**. If it crashes anywhere before that, the message stays
  in the queue and is redelivered. A test checks from a second database connection that the row is visible at the moment
  `ack()` is called. Another test kills the consumer mid-feed and restarts it.
- A **malformed message** goes into `rejected_events` with a reason code, the error detail and the raw body, and is then
  acked. It never blocks the queue. The registry owns the queue and declares it without arguments, so a dead-letter
  exchange is not an option. This table plays that role, and it can be queried and replayed.
- **Database unreachable:** the consumer nacks the message with requeue and reconnects with a delay. It never rejects a
  good message because of an outage.
- **Any other error** (a bug, a model crash) gets one retry. If the redelivered copy fails too, it is stored as
  `processing_error` and acked, which stops a redelivery loop.
- **Queue declaration:** the consumer declares the queue the same way the registry's producer does (`durable=True`, no
  arguments). RabbitMQ accepts an identical declaration, and the consumer can then start before the first publish.
- **Prefetch** is 32, and messages are processed one by one. Order within the consumer does not matter, because the
  database decides by `updatedAt`.

### Validation: required and optional fields

| Field | Rule |
|---|---|
| `type` | Must be `company-upserted` or `company-struck-off`; anything else is rejected. |
| `registrationNumber` | Required string. It is trimmed and uppercased and must then match `SYN-\d{6}`. |
| `updatedAt` | Required. ISO 8601 with an offset, `Z`, or no timezone (read as +08:00), or an integer in epoch milliseconds. |
| `data` (upserts) | Required object with all four keys. A partial object is rejected rather than merged, because a stub like `{"companyName": "Unknown"}` must never become a company's newest state. |
| `companyName` | Required, non-empty string. |
| `natureOfBusiness`, `industry`, `state` | String or null. |

Rejecting the whole event is safe here because every company in the feed also has at least one complete event. Whether
naive timestamps are really Malaysia time was checked against the data: under that reading, each such event falls
within minutes of its `+08:00` neighbours. The same check confirmed the `Z` and epoch-millisecond readings.

### Embeddings

- The model is `BAAI/bge-small-en-v1.5` through fastembed (ONNX Runtime, CPU, 384 dimensions, ~65 MB). It is the model
  the brief suggests, and it works on Python 3.14.
- The model is hidden behind a two-line `Embedder` protocol, so every test uses a deterministic fake.
- Only the cleaned description is embedded. Industry and state are not mixed in, because the brief says matching on the
  nature of business is enough.
- The ONNX Runtime thread count is capped at 4. The default (one thread per logical core) measured 4× slower per text on
  a 12-thread laptop.
- **Companies without a description** are stored and served with `reason: "no_description"`, but they have no embedding,
  so they do not appear in recommendations. I chose this over inventing a description; see "What's left".

### Similarity search in Postgres

```sql
SELECT …, 1 - distance AS similarity FROM (
    SELECT …, embedding <=> (SELECT embedding FROM companies WHERE registration_number = $1) AS distance
    FROM companies
    WHERE status = 'active' AND embedding IS NOT NULL AND registration_number <> $1
    ORDER BY embedding <=> (SELECT embedding FROM companies WHERE registration_number = $1)
    LIMIT 100) AS candidates
ORDER BY distance, registration_number
LIMIT $2
```

- There is an **HNSW** index on `embedding vector_cosine_ops`. HNSW needs no training step, unlike IVFFlat, so it works
  on an empty table that fills up, and it has better recall at this size.
- The index can only serve a plain `ORDER BY distance LIMIT n`. So the inner query lets the index fetch 100 candidates,
  and the outer query re-sorts them exactly, **breaking ties by registration number**. Ties are common: 1,953 companies
  share only 180 distinct descriptions, so without a tie-break the panel would change between requests.
- Each connection sets `hnsw.ef_search = 100` and `hnsw.iterative_scan = relaxed_order` (pgvector 0.8). The filters on
  status and embedding can then never starve the result.
- `EXPLAIN ANALYZE` shows an `Index Scan using companies_embedding_hnsw` that takes about 1.5 ms. For every one of the
  1,953 searchable companies, the index returns **exactly** the same top 4 as an exact scan.

### API

- FastAPI. The route is `GET /companies/{registrationNumber}/similar?limit=4`, and the response includes the company
  itself so the page header can render from the same call.
- JSON field names are camelCase, like the feed.
- Every error, including FastAPI's own validation errors and unknown routes, is returned as
  `{"error": {"code", "message"}}`. Codes: 404 `company_not_found`, 422 `invalid_parameter`, 500 `internal_error`. The
  README has the full table.

### Latency

Measured with `just bench` on a laptop with Docker Desktop. It requests all 2,000 companies in random order through HTTP:

| Load | p50 | p95 | p99 | Throughput |
|---|---|---|---|---|
| 1 request in flight | 5.5 ms | 9.4 ms | 15.6 ms | 161 req/s |
| 10 requests in flight | 18.6 ms | 38.5 ms | 53.6 ms | 459 req/s |

That is comfortably fast enough to render while a page loads. Most of the time is spent in HTTP and Python; the SQL
takes about 1.5 ms.

## 3. Assumptions where the scenario was unclear

1. Naive timestamps are Malaysia time, `Z` means UTC, and integers are epoch milliseconds. All three readings were checked
   against neighbouring events.
2. Lowercase or space-padded registration numbers refer to the same company. Integer ones (`119`) cannot be mapped safely
   and are junk anyway.
3. An event whose `data` is partial or malformed is rejected entirely, never merged field by field.
4. "Struck off" means the company is no longer recommended, but its page still works. It is a tombstone, not a delete.
5. "No lost companies" means every valid company is stored and gets a page. A company with no description at all cannot be
   placed by meaning, so it is shown on its own page but does not appear in other companies' recommendations.
6. The portal wants deterministic panels, so equal similarity is broken by registration number.
7. `source` and `ingestedBy` carry no meaning for recommendations.

## 4. Optional deep-dive: recommendation quality

There are no relevance labels, so I used the company's **industry** as a proxy label. The model never sees the industry;
it only sees the description. `just evaluate` ranks every company's neighbours the same way the service does and reports:

- **Industry P@4:** the share of the 4 cards that are in the same industry.
- **Different-text P@4:** the same measure, but ignoring neighbours whose description is identical. Every model puts
  identical texts first, so the plain P@4 is inflated. This measure isolates whether the model understands meaning.
- **Cross-language rate:** the share of those different-text neighbours written in the other language. 612 of 1,953
  descriptions are Malay, and most of them are translations of English templates.

| Model (all 384 dims) | Industry P@4 | Different-text P@4 | Cross-language rate |
|---|---|---|---|
| Hashed bag-of-words (lexical baseline) | 0.944 | 0.299 | 0.049 |
| `BAAI/bge-small-en-v1.5` (**current default**) | 0.934 | 0.489 | 0.130 |
| `paraphrase-multilingual-MiniLM-L12-v2` | 0.941 | **0.804** | **0.691** |

What this shows:

- **The headline number hides the real behaviour.** Every model scores about 0.94 on P@4, because exact duplicate
  descriptions fill most panels. That includes the panel in the mock: SYN-001550's first card has similarity 1.0.
- **bge-small-en does not understand Malay.** For "Restoran masakan Melayu dan Cina" (*Malay and Chinese restaurant*), its
  nearest different descriptions are a parcel-delivery company, a café and a travel agency, all in Malay. It matches the
  language, not the business. The multilingual model's first match is "Restaurant serving Malaysian and Chinese cuisine"
  (0.94).
- About 30% of the feed is Malay, so this is the biggest quality issue in the service. Malay-described companies get worse
  recommendations, and an English and a Malay company that do the same thing rarely meet.

**Recommendation:** switch to `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. It has the same dimension, so
the change is just `EMBEDDING_MODEL=…` and re-consuming the feed, with no schema change. I left the default as the brief's
model because that was the agreed plan before this measurement, and the mock was produced with it. The proxy also has
limits: industry is coarse, and 120 events have no industry. A small hand-labelled set of "would a user click this?"
pairs would be the next step.

## 5. What changes at 10x volume

At 20,000 companies and about 25,000 messages per replay, the current design still works unchanged. pgvector with HNSW
handles millions of 384-dimension vectors on one machine. What I would change:

1. **Consumer throughput.** Each message costs about 7 ms of commit plus 5 ms of embedding, so a 25k-message replay takes
   about 5 minutes. I would batch: take up to N deliveries, embed them in one model call (batched embedding measured about
   2× faster per text), apply them in one transaction, then ack them all. Ack-after-commit still holds. A further step is
   to run several consumers; the conditional upsert is already safe under concurrency.
2. **Cache embeddings by text.** There are only 180 distinct descriptions for 2,000 companies. A
   `description_hash → embedding` lookup skips the model for repeated texts and for stale events.
3. **Bound `processed_messages`.** It grows forever. I would add a retention window (for example, delete IDs older than
   the registry's redelivery horizon), or partition it by day. The timestamp guard keeps correctness after expiry.
4. **The API.** Run several uvicorn workers behind the portal and tune the connection pool. The response for a company only
   changes when some company changes, so it can be cached briefly, or the top-k can be precomputed per company and stored.
5. **Index maintenance.** HNSW builds take longer and need memory (`maintenance_work_mem`). I would build the index
   `CONCURRENTLY` after a bulk backfill and watch `ef_search` against measured recall.
6. **Operations.** Alert on the size of `rejected_events`, on consumer lag (queue depth), and on p95 latency.

## 6. What's left

- **Companies without a description (39)** are not recommendable. A reasonable fallback is to embed
  `companyName + industry`, which is a weak signal. A better one is to ask the registry for the data.
- **Struck-off notice before any upsert** creates a row with only a status. If an *older* upsert then arrives, it is stale
  by the rule, so that company would have no name. This does not happen in this feed. A fix is to track the status time
  and the details time separately.
- **Only the latest state is kept**, so there is no audit trail of a company's history. An `events` table would add one.
- **Retry counting** uses RabbitMQ's `redelivered` flag. A message redelivered after a consumer crash, rather than after a
  failure, gets no second retry. A proper attempt counter would need our own table, because headers cannot be changed on
  requeue.
- **Model choice:** see the deep-dive. Switching to the multilingual model is a measured improvement I did not make.
- **No metrics endpoint.** There are logs, but no Prometheus counters for applied, stale, duplicate and rejected messages.
- **No explanation** of why a company was recommended, beyond the similarity score.

## 7. AI tools used

I used **Claude Code** (Anthropic, model Claude Opus 5.5) throughout, as a pair programmer that I directed and reviewed:

- **Data analysis:** it wrote the throwaway scripts that profiled `feed.jsonl` (field variants, timestamp formats,
  out-of-order events), and it checked the timestamp interpretations against neighbouring events.
- **Design:** it proposed the design and listed every ambiguity with a recommendation. I reviewed the list and approved
  it. The decisions are recorded in `docs/decisions.md`.
- **Code and tests:** it wrote the tests first, then the implementation, as separate commits, and ran them against the real
  Postgres and RabbitMQ. It also checked library APIs against the installed package source.
- **Measurements:** it wrote and ran the latency benchmark, the HNSW-versus-exact check and the quality evaluation.
- **Documentation:** it drafted this document and the README for my review.

I can walk through and justify every line, and that is the bar I held the generated code to. No paid APIs are used by
the solution itself; the embedding model runs locally on the CPU.
