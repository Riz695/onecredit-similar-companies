# Decision log

Decisions agreed in planning (Stage 1), plus decisions made during execution. This is the raw log that
`DESIGN.md` is built from.

## What the feed actually contains

Analysis of `data/feed.jsonl` (2,518 lines) before any design:

| Finding | Count |
|---|---|
| Lines that are not valid JSON (truncated mid-`companyName`) | 5 |
| Lines that are the JSON literal `null` (the same line 6 times) | 6 |
| Distinct `message_id`s (it is `sha256(line)`, so duplicates = identical lines) | 2,450 (68 redundant copies) |
| Event types | `company-upserted` 2,499, `company-struck-off` 8 |
| Distinct valid companies | 2,000 (1,594 with one event, 322 with 2, 77 with 3, 7 with 4) |
| Events arriving with an older `updatedAt` than one already seen for the company | 66 |
| `registrationNumber` padded with spaces / lowercase / missing / integer | 27 / 22 / 7 / 3 |
| `updatedAt` as `+08:00` / `Z` / epoch milliseconds (int) / no timezone / missing | 2,428 / 28 / 30 / 14 / 7 |
| `data` as a string / missing on an upsert / only `companyName` | 4 / 13 / 14 |
| `natureOfBusiness` null / empty | 29 / 15 (39 companies never have one) |
| `industry` null, `state` null | 120, 60 |
| `state` spelled with inconsistent case (`KUALA LUMPUR`, `pulau pinang`) | ~500 |
| Descriptions that are one phrase repeated (up to 3,359 chars) | 7 |
| Descriptions in Malay | ~550 |
| Extra top-level fields `source` / `ingestedBy` | 38 / 37 |
| Distinct descriptions across all companies | 180 |

Every one of the 2,000 companies has at least one usable upsert, so losing none is possible.

## Agreed decisions (planning)

| # | Topic | Decision | Why |
|---|---|---|---|
| 1 | Repo name | `onecredit-similar-companies` | Descriptive. |
| 2 | Docker | User started Docker Desktop. | — |
| 3 | `just` | Installed with `uv tool install rust-just` (user-level). | No sudo needed. |
| 4 | Messy `registrationNumber` | Trim and uppercase, then require `^SYN-\d{6}$`. | They are clearly the same companies. |
| 5 | Integer or missing `registrationNumber` | Reject. | `119` cannot be mapped safely to `SYN-000119`, and those rows are junk ("X"/"Trading"). |
| 6 | Timestamps | ISO with offset; `Z` = UTC; int = epoch **milliseconds**; no timezone = MYT (+08:00). Missing or unparseable: reject. | Each variant lines up with its neighbouring `+08:00` events under these readings. |
| 7 | Incomplete `data` | Reject the whole event (string `data`, missing `data` on an upsert, missing any of the four keys). Never apply a partial update. | A stub such as `{"companyName": "Unknown"}` that is the newest event would wipe real data. |
| 8 | Null or empty `natureOfBusiness` | Store the company with no embedding. `GET` works and the API says `no_description`; it does not appear in others' recommendations. | Keeps the 39 companies on the portal without inventing a description. Embedding name + industry is listed as a next step. |
| 9 | Null `industry` / `state` | Optional: stored and returned as null. | — |
| 10 | `state` casing | Map case-insensitively onto the 16 canonical Malaysian state/territory names; keep unknown values as given (trimmed). | Clean cards. |
| 11 | `company-struck-off` | Set `status = 'struck_off'` with the same latest-wins rule. Keep the row (its page works); exclude it from other companies' recommendations. An older upsert cannot revive it. | Tombstone, not delete. |
| 12 | Unknown `type` | Reject. | Safe default. |
| 13 | `source` / `ingestedBy` | Ignored. The raw body of rejects is stored. | Not needed for recommendations. |
| 14 | Repeated-phrase descriptions | Collapse an exact repetition of one phrase to a single copy (stored and embedded). | The repetition is noise and the model truncates at 512 tokens. |
| 15 | Malay descriptions | Keep `BAAI/bge-small-en-v1.5`; measure the effect in the deep-dive; mention `intfloat/multilingual-e5-small`. | Swapping the model up front costs time. |
| 16 | Similarity | Description embedding only, no industry/state filter. Ties broken by `registration_number`. | README: "nature of business is enough". Deterministic results. |
| 17 | Default `limit` | 4 (1–20 allowed). | Matches the mock. |
| 18 | API stack | FastAPI + uvicorn, psycopg 3 async pool + `pgvector` adapter. | Standard; native 422 handling; OpenAPI docs. |
| 19 | Deep-dive | Recommendation quality: same-industry precision@k and English-vs-Malay behaviour. | ML-engineer scope; uses the data findings. p50/p95 latency is measured regardless. |
| 20 | Duplicates | `processed_messages(message_id)` table **and** the `updatedAt` guard. | A timestamp alone cannot tell a redelivered struck-off event apart. |

Also agreed (design proposal):

- Ack only after the database commit. Malformed message → row in `rejected_events` with the reason, then ack.
- Latest-wins is enforced in SQL: `INSERT … ON CONFLICT DO UPDATE … WHERE companies.source_updated_at < EXCLUDED.source_updated_at`.
- HNSW index with `vector_cosine_ops`; cosine distance `<=>`; vectors L2-normalised.
- Error body everywhere: `{"error": {"code": "...", "message": "..."}}`. 404 `company_not_found`, 422 `invalid_parameter`.
- Unit tests use a deterministic fake embedder behind an `Embedder` protocol.

## Execution decisions

<!-- Appended during Stage 2. -->
