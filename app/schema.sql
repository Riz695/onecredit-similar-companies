-- Idempotent: safe to run on every start of the consumer and the service.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per company: always its latest known state (by the source time, `updatedAt`).
CREATE TABLE IF NOT EXISTS companies (
    registration_number text PRIMARY KEY,
    -- Null only when a struck-off notice arrives for a company we have never seen.
    company_name        text,
    description         text,
    industry            text,
    state               text,
    status              text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'struck_off')),
    -- Null when the company has no description to embed.
    embedding           vector(384),
    source_updated_at   timestamptz NOT NULL,
    last_message_id     text NOT NULL,
    stored_at           timestamptz NOT NULL DEFAULT now()
);

-- Approximate nearest-neighbour search on cosine distance (the `<=>` operator).
CREATE INDEX IF NOT EXISTS companies_embedding_hnsw ON companies USING hnsw (embedding vector_cosine_ops);

-- Every message we have committed, so a redelivery is recognised and skipped.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- Messages we could not apply. The registry's queue cannot have a dead-letter exchange, so they live here.
CREATE TABLE IF NOT EXISTS rejected_events (
    message_id  text PRIMARY KEY,
    reason      text NOT NULL,
    detail      text NOT NULL,
    body        text NOT NULL,
    rejected_at timestamptz NOT NULL DEFAULT now()
);
