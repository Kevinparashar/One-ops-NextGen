-- _foundation/01_extensions_schemas.sql
--
-- Shared infrastructure that every service depends on. Run FIRST.
--   * Extensions: pgvector (embeddings), pgmq (refresh queues), pgcrypto
--     (digest() — used by the content_hash generated columns on every service).
--   * Schemas: itsm (application records) + ai (vector store).
--
-- Idempotent. No queues are created here — each service owns its own queue in
-- its 02_embeddings.sql (per-service workers, no shared lane).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgmq;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS itsm;
CREATE SCHEMA IF NOT EXISTS ai;
