-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0004 — Embedding refresh substrate (P0)
-- Date: 2026-05-30
-- Purpose: per-service embedding storage with hash-gated trigger-based refresh.
--          Additive only. No reader uses the new tables yet (P3 swaps UC-5).
-- Rollback: see end of file.
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Extensions
CREATE EXTENSION IF NOT EXISTS pgmq;

-- 2. Schema
CREATE SCHEMA IF NOT EXISTS ai;

-- 3. Queue
SELECT pgmq.create('embedding_refresh');

-- 4. ai.embeddings_incident
CREATE TABLE IF NOT EXISTS ai.embeddings_incident (
  entity_id         text         NOT NULL,
  chunk_type        text         NOT NULL
    CHECK (chunk_type IN ('symptom_anchor','diagnosis_trail','resolution')),
  tenant_id         text         NOT NULL,
  embedding         vector(1536) NOT NULL,
  content_hash      bytea        NOT NULL,
  content_text      text         NOT NULL,
  embedding_model   text         NOT NULL,
  embedding_version text         NOT NULL DEFAULT 'v1',
  embedded_at       timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, chunk_type, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_incident_tenant
  ON ai.embeddings_incident (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_incident_hnsw_symptom
  ON ai.embeddings_incident USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'symptom_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_incident_hnsw_diagnosis
  ON ai.embeddings_incident USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'diagnosis_trail';

-- 5. ai.embeddings_request
CREATE TABLE IF NOT EXISTS ai.embeddings_request (
  entity_id         text         NOT NULL,
  chunk_type        text         NOT NULL
    CHECK (chunk_type IN ('symptom_anchor','diagnosis_trail','resolution')),
  tenant_id         text         NOT NULL,
  embedding         vector(1536) NOT NULL,
  content_hash      bytea        NOT NULL,
  content_text      text         NOT NULL,
  embedding_model   text         NOT NULL,
  embedding_version text         NOT NULL DEFAULT 'v1',
  embedded_at       timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, chunk_type, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_request_tenant
  ON ai.embeddings_request (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_request_hnsw_symptom
  ON ai.embeddings_request USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'symptom_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_request_hnsw_diagnosis
  ON ai.embeddings_request USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'diagnosis_trail';

-- 6. Generated hash columns on itsm.incident
-- Note: Postgres disallows generated-column-referencing-generated-column.
-- The hash expression inlines the concat directly. The worker rebuilds the
-- same text from the same source columns when it embeds.
ALTER TABLE itsm.incident
  ADD COLUMN IF NOT EXISTS content_hash_symptom bytea
    GENERATED ALWAYS AS (
      digest(
        'Title: '       || coalesce(title, '')        || E'\n' ||
        'Description: ' || coalesce(description, '')  || E'\n' ||
        'Category: '    || coalesce(category, '')     || E'\n' ||
        'Subcategory: ' || coalesce(subcategory, '')  || E'\n' ||
        'Service: '     || coalesce(service_name, '') || E'\n' ||
        'CI: '          || coalesce(ci_id, ''),
        'sha256'
      )
    ) STORED;

ALTER TABLE itsm.incident
  ADD COLUMN IF NOT EXISTS content_hash_diagnosis bytea
    GENERATED ALWAYS AS (
      digest(coalesce(work_notes::text, '[]'), 'sha256')
    ) STORED;

-- 7. Generated hash columns on itsm.request
ALTER TABLE itsm.request
  ADD COLUMN IF NOT EXISTS content_hash_symptom bytea
    GENERATED ALWAYS AS (
      digest(
        'Title: '       || coalesce(title, '')           || E'\n' ||
        'Description: ' || coalesce(description, '')     || E'\n' ||
        'Category: '    || coalesce(category, '')        || E'\n' ||
        'Catalog: '     || coalesce(catalog_item_id, '') || E'\n' ||
        'CI: '          || coalesce(ci_id, ''),
        'sha256'
      )
    ) STORED;

ALTER TABLE itsm.request
  ADD COLUMN IF NOT EXISTS content_hash_diagnosis bytea
    GENERATED ALWAYS AS (
      digest(coalesce(comments::text, '[]'), 'sha256')
    ) STORED;

-- 8. Trigger function for incident
CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_incident()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_symptom IS DISTINCT FROM NEW.content_hash_symptom THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_incident',
      'entity_id',     NEW.incident_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'symptom_anchor',
      'enqueued_hash', encode(NEW.content_hash_symptom, 'hex')
    ));
  END IF;

  IF OLD.content_hash_diagnosis IS DISTINCT FROM NEW.content_hash_diagnosis THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_incident',
      'entity_id',     NEW.incident_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'diagnosis_trail',
      'enqueued_hash', encode(NEW.content_hash_diagnosis, 'hex')
    ));
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_incident_embedding_refresh ON itsm.incident;
CREATE TRIGGER trg_incident_embedding_refresh
  AFTER UPDATE ON itsm.incident
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_incident();

-- 9. Trigger function for request
CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_request()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_symptom IS DISTINCT FROM NEW.content_hash_symptom THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_request',
      'entity_id',     NEW.request_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'symptom_anchor',
      'enqueued_hash', encode(NEW.content_hash_symptom, 'hex')
    ));
  END IF;

  IF OLD.content_hash_diagnosis IS DISTINCT FROM NEW.content_hash_diagnosis THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_request',
      'entity_id',     NEW.request_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'diagnosis_trail',
      'enqueued_hash', encode(NEW.content_hash_diagnosis, 'hex')
    ));
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_request_embedding_refresh ON itsm.request;
CREATE TRIGGER trg_request_embedding_refresh
  AFTER UPDATE ON itsm.request
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_request();

-- ─────────────────────────────────────────────────────────────────────────────
-- ROLLBACK (run manually if needed):
--   DROP TRIGGER IF EXISTS trg_incident_embedding_refresh ON itsm.incident;
--   DROP TRIGGER IF EXISTS trg_request_embedding_refresh ON itsm.request;
--   DROP FUNCTION IF EXISTS ai.enqueue_embedding_refresh_incident;
--   DROP FUNCTION IF EXISTS ai.enqueue_embedding_refresh_request;
--   ALTER TABLE itsm.incident
--     DROP COLUMN IF EXISTS content_hash_symptom,
--     DROP COLUMN IF EXISTS content_hash_diagnosis;
--   ALTER TABLE itsm.request
--     DROP COLUMN IF EXISTS content_hash_symptom,
--     DROP COLUMN IF EXISTS content_hash_diagnosis;
--   DROP SCHEMA IF EXISTS ai CASCADE;
--   -- (pgmq queue contents drop with the schema)
-- ─────────────────────────────────────────────────────────────────────────────
