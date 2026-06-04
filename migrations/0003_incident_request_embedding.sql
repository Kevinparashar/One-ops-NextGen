-- 0003_incident_request_embedding.sql
--
-- WHY: UC-5 Triage's check_duplicate_candidates tool uses hybrid retrieval
-- (FTS + vector + RRF fusion + relevance gate ≥ 0.50) — the same pattern UC-3
-- KB lookup uses today. Hybrid requires two columns per searchable table:
--   * search_tsv     — generated tsvector for keyword/lexical ranking (ts_rank_cd)
--   * embedding      — 1536-d pgvector for semantic similarity (text-embedding-3-large)
--
-- Pattern is identical to the kb_knowledge.content_tsv treatment in 0002:
-- generated tsvector is automatic (zero application code; Postgres maintains
-- it on every write), and the HNSW index gives sub-millisecond cosine search.
--
-- Idempotent throughout — every ALTER and CREATE INDEX guards with
-- IF NOT EXISTS, so a re-run is a no-op. Safe to apply in any environment.
--
-- Operator instructions:
--   psql "$DATABASE_URL" -f migrations/0003_incident_request_embedding.sql
--
-- Verification after apply:
--   psql -c "SELECT column_name FROM information_schema.columns
--            WHERE table_schema='itsm' AND table_name='incident'
--            AND column_name IN ('search_tsv','embedding','embedding_model',
--                                'embedding_version','embedded_at');"
--   psql -c "SELECT indexname FROM pg_indexes WHERE tablename='incident'
--            AND indexname IN ('idx_incident_tsv','idx_incident_embedding');"
--
-- Rollback (if ever needed — pgvector indexes are large but harmless to drop):
--   ALTER TABLE itsm.incident DROP COLUMN IF EXISTS embedding, ...;
--   DROP INDEX IF EXISTS itsm.idx_incident_embedding, itsm.idx_incident_tsv;
--   (same for itsm.request)

-- ─── prerequisite extension ─────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── itsm.incident ──────────────────────────────────────────────────────────

-- Generated tsvector for keyword search. title weighted higher than description
-- so a title-keyword match outranks a body-keyword match (mirrors UC-3 kb_knowledge.content_tsv).
ALTER TABLE itsm.incident
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B')
        ) STORED;

-- Embedding columns. 1536 dims = text-embedding-3-large; embedding_model and
-- embedding_version make it possible to re-embed with a newer model later
-- without losing the audit trail of what was used for a given row.
ALTER TABLE itsm.incident
    ADD COLUMN IF NOT EXISTS embedding         vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_model   text,
    ADD COLUMN IF NOT EXISTS embedding_version text,
    ADD COLUMN IF NOT EXISTS embedded_at       timestamptz;

-- Indexes. GIN on tsvector (standard), HNSW on vector with cosine ops.
-- HNSW parameters left at defaults (m=16, ef_construction=64); production
-- tuning happens after we measure recall on real queries.
CREATE INDEX IF NOT EXISTS idx_incident_tsv
    ON itsm.incident
    USING gin (search_tsv);

CREATE INDEX IF NOT EXISTS idx_incident_embedding
    ON itsm.incident
    USING hnsw (embedding vector_cosine_ops);

-- ─── itsm.request ───────────────────────────────────────────────────────────

ALTER TABLE itsm.request
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B')
        ) STORED;

ALTER TABLE itsm.request
    ADD COLUMN IF NOT EXISTS embedding         vector(1536),
    ADD COLUMN IF NOT EXISTS embedding_model   text,
    ADD COLUMN IF NOT EXISTS embedding_version text,
    ADD COLUMN IF NOT EXISTS embedded_at       timestamptz;

CREATE INDEX IF NOT EXISTS idx_request_tsv
    ON itsm.request
    USING gin (search_tsv);

CREATE INDEX IF NOT EXISTS idx_request_embedding
    ON itsm.request
    USING hnsw (embedding vector_cosine_ops);

-- ─── done ───────────────────────────────────────────────────────────────────
-- After this runs:
--   * itsm.incident has search_tsv + embedding columns and both indexes
--   * itsm.request  has search_tsv + embedding columns and both indexes
--   * Generated tsvectors are populated for all existing rows immediately
--   * embedding columns are NULL until tools/seed_incident_embeddings.py runs
