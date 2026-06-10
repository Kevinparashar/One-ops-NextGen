-- request/02_embeddings.sql
--
-- The request vector store + its OWN refresh queue + trigger.
-- Drained by database/request/worker.py. Requires: 01_schema.sql + _foundation.
-- Idempotent.

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
  -- tenant_id is in the PK: entity ids are unique only per-tenant (§2.4).
  PRIMARY KEY (tenant_id, entity_id, chunk_type, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_request_tenant
  ON ai.embeddings_request (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_request_hnsw_symptom
  ON ai.embeddings_request USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'symptom_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_request_hnsw_diagnosis
  ON ai.embeddings_request USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'diagnosis_trail';

-- Self-heal: widen a legacy narrow PK (without tenant_id). Idempotent.
DO $$
DECLARE pk_cols text; pk_name text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(i.indkey, a.attnum))
    INTO pk_cols
  FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
  WHERE i.indrelid='ai.embeddings_request'::regclass AND i.indisprimary;
  IF pk_cols IS NOT NULL AND position('tenant_id' in pk_cols) = 0 THEN
    SELECT conname INTO pk_name FROM pg_constraint
      WHERE conrelid='ai.embeddings_request'::regclass AND contype='p';
    EXECUTE 'ALTER TABLE ai.embeddings_request DROP CONSTRAINT '||quote_ident(pk_name);
    ALTER TABLE ai.embeddings_request
      ADD PRIMARY KEY (tenant_id, entity_id, chunk_type, embedding_version);
  END IF;
END $$;

DO $$
BEGIN
  PERFORM pgmq.create('embedding_refresh_request');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;

CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_request()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_symptom IS DISTINCT FROM NEW.content_hash_symptom THEN
    PERFORM pgmq.send('embedding_refresh_request', jsonb_build_object(
      'target_table',  'ai.embeddings_request',
      'entity_id',     NEW.request_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'symptom_anchor',
      'enqueued_hash', encode(NEW.content_hash_symptom, 'hex')
    ));
  END IF;
  IF OLD.content_hash_diagnosis IS DISTINCT FROM NEW.content_hash_diagnosis THEN
    PERFORM pgmq.send('embedding_refresh_request', jsonb_build_object(
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
  AFTER INSERT OR UPDATE ON itsm.request
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_request();

-- DELETE cleanup: when a parent row is removed, drop its embeddings (no orphans).
CREATE OR REPLACE FUNCTION ai.cleanup_embeddings_request()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM ai.embeddings_request
   WHERE entity_id = OLD.request_id AND tenant_id = OLD.tenant_id;
  RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_request_embedding_cleanup ON itsm.request;
CREATE TRIGGER trg_request_embedding_cleanup
  AFTER DELETE ON itsm.request
  FOR EACH ROW
  EXECUTE FUNCTION ai.cleanup_embeddings_request();
