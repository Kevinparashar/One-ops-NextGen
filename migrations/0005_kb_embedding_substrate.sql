-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0005 — KB embedding substrate (extends 0004 pattern to kb_knowledge)
-- Date: 2026-05-30
-- Purpose: per-chunk_type KB embeddings with adaptive body chunking. Same
--          trigger-fed pgmq pattern as incident/request, plus a chunk_index
--          column because long KB articles produce multiple body chunks.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. ai.embeddings_kb_knowledge
CREATE TABLE IF NOT EXISTS ai.embeddings_kb_knowledge (
  entity_id         text         NOT NULL,                -- kb_id
  chunk_type        text         NOT NULL
    CHECK (chunk_type IN ('kb_anchor','kb_body')),
  chunk_index       int          NOT NULL DEFAULT 0,      -- 0 for anchor; 0..N-1 for body
  tenant_id         text         NOT NULL,
  embedding         vector(1536) NOT NULL,
  content_hash      bytea        NOT NULL,
  content_text      text         NOT NULL,
  embedding_model   text         NOT NULL,
  embedding_version text         NOT NULL DEFAULT 'v1',
  embedded_at       timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, chunk_type, chunk_index, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_kb_tenant
  ON ai.embeddings_kb_knowledge (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_kb_hnsw_anchor
  ON ai.embeddings_kb_knowledge USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'kb_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_kb_hnsw_body
  ON ai.embeddings_kb_knowledge USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'kb_body';

-- 2. Generated hash column on itsm.kb_knowledge.
-- Single hash over the whole semantic surface — when ANY part changes the
-- worker regenerates all chunks (anchor + body). Simpler than per-chunk
-- hashes; KB updates are usually wholesale (author republishes article).
-- tags is text[]; cast via array_to_string for deterministic hash input.
ALTER TABLE itsm.kb_knowledge
  ADD COLUMN IF NOT EXISTS content_hash_kb bytea
    GENERATED ALWAYS AS (
      digest(
        'Title: '    || coalesce(title, '')    || E'\n' ||
        'Summary: '  || coalesce(summary, '')  || E'\n' ||
        'Category: ' || coalesce(category, '') || E'\n' ||
        'Content: '  || coalesce(content, ''),
        'sha256'
      )
    ) STORED;
-- Note: `tags` (text[]) intentionally not in the hash — text[]::text isn't
-- IMMUTABLE in Postgres and generated columns require immutable expressions.
-- Tag-only edits are rare and don't materially change semantic similarity;
-- if needed in future, mirror tags into a separate normalised text column.

-- 3. Trigger function for kb_knowledge — same shape as incident/request.
-- Single message per UPDATE; worker emits anchor + N body chunks.
CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_kb()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_kb IS DISTINCT FROM NEW.content_hash_kb THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_kb_knowledge',
      'entity_id',     NEW.kb_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'kb_all',     -- sentinel: worker emits anchor+body
      'enqueued_hash', encode(NEW.content_hash_kb, 'hex')
    ));
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_kb_embedding_refresh ON itsm.kb_knowledge;
CREATE TRIGGER trg_kb_embedding_refresh
  AFTER UPDATE ON itsm.kb_knowledge
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_kb();

-- ─────────────────────────────────────────────────────────────────────────────
-- ROLLBACK:
--   DROP TRIGGER IF EXISTS trg_kb_embedding_refresh ON itsm.kb_knowledge;
--   DROP FUNCTION IF EXISTS ai.enqueue_embedding_refresh_kb;
--   ALTER TABLE itsm.kb_knowledge DROP COLUMN IF EXISTS content_hash_kb;
--   DROP TABLE IF EXISTS ai.embeddings_kb_knowledge;
-- ─────────────────────────────────────────────────────────────────────────────
