-- incident/02_embeddings.sql
--
-- The incident vector store + its OWN refresh queue + trigger.
--   * ai.embeddings_incident (one row per chunk_type per ticket)
--   * pgmq queue `embedding_refresh_incident` — this service's private lane
--     (so a bulk incident reindex never blocks any other service's worker)
--   * trigger: a change to a content_hash doorbell enqueues the changed chunk
--
-- Drained by database/incident/worker.py. Requires: 01_schema.sql + _foundation
-- (vector/pgmq extensions, ai schema). Idempotent.

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
  -- tenant_id is in the PK: entity ids are unique only per-tenant (§2.4).
  PRIMARY KEY (tenant_id, entity_id, chunk_type, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_incident_tenant
  ON ai.embeddings_incident (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_incident_hnsw_symptom
  ON ai.embeddings_incident USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'symptom_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_incident_hnsw_diagnosis
  ON ai.embeddings_incident USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'diagnosis_trail';

-- Self-heal: widen a legacy narrow PK (without tenant_id) on already-deployed
-- tables. Idempotent — no-op once tenant_id is in the PK.
DO $$
DECLARE pk_cols text; pk_name text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(i.indkey, a.attnum))
    INTO pk_cols
  FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
  WHERE i.indrelid='ai.embeddings_incident'::regclass AND i.indisprimary;
  IF pk_cols IS NOT NULL AND position('tenant_id' in pk_cols) = 0 THEN
    SELECT conname INTO pk_name FROM pg_constraint
      WHERE conrelid='ai.embeddings_incident'::regclass AND contype='p';
    EXECUTE 'ALTER TABLE ai.embeddings_incident DROP CONSTRAINT '||quote_ident(pk_name);
    ALTER TABLE ai.embeddings_incident
      ADD PRIMARY KEY (tenant_id, entity_id, chunk_type, embedding_version);
  END IF;
END $$;

-- This service's private refresh queue.
DO $$
BEGIN
  PERFORM pgmq.create('embedding_refresh_incident');
EXCEPTION WHEN OTHERS THEN
  NULL;  -- already exists
END $$;

CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_incident()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_symptom IS DISTINCT FROM NEW.content_hash_symptom THEN
    PERFORM pgmq.send('embedding_refresh_incident', jsonb_build_object(
      'target_table',  'ai.embeddings_incident',
      'entity_id',     NEW.incident_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'symptom_anchor',
      'enqueued_hash', encode(NEW.content_hash_symptom, 'hex')
    ));
  END IF;
  IF OLD.content_hash_diagnosis IS DISTINCT FROM NEW.content_hash_diagnosis THEN
    PERFORM pgmq.send('embedding_refresh_incident', jsonb_build_object(
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
  AFTER INSERT OR UPDATE ON itsm.incident
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_incident();

-- DELETE cleanup: when a parent row is removed, drop its embeddings (no orphans).
CREATE OR REPLACE FUNCTION ai.cleanup_embeddings_incident()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM ai.embeddings_incident
   WHERE entity_id = OLD.incident_id AND tenant_id = OLD.tenant_id;
  RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_incident_embedding_cleanup ON itsm.incident;
CREATE TRIGGER trg_incident_embedding_cleanup
  AFTER DELETE ON itsm.incident
  FOR EACH ROW
  EXECUTE FUNCTION ai.cleanup_embeddings_incident();
