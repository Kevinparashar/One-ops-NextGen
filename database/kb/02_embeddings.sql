-- kb/02_embeddings.sql
--
-- The KB vector store (per-chunk: anchor + body) + its OWN queue + trigger.
-- One trigger fires per article ('kb_all' sentinel); the worker emits 1 anchor
-- + 1..N body chunks (adaptive chunking + overlap). Requires: 01_schema.sql +
-- _foundation. Idempotent.

CREATE TABLE IF NOT EXISTS ai.embeddings_kb_knowledge (
  entity_id         text         NOT NULL,                -- kb_id
  chunk_type        text         NOT NULL
    CHECK (chunk_type IN ('kb_anchor','kb_body')),
  chunk_index       int          NOT NULL DEFAULT 0,      -- 0 anchor; 0..N-1 body
  tenant_id         text         NOT NULL,
  embedding         vector(1536) NOT NULL,
  content_hash      bytea        NOT NULL,
  content_text      text         NOT NULL,
  embedding_model   text         NOT NULL,
  embedding_version text         NOT NULL DEFAULT 'v1',
  embedded_at       timestamptz  NOT NULL DEFAULT now(),
  -- tenant_id is in the PK: entity ids are unique only per-tenant (§2.4).
  PRIMARY KEY (tenant_id, entity_id, chunk_type, chunk_index, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_kb_tenant
  ON ai.embeddings_kb_knowledge (tenant_id, chunk_type);
CREATE INDEX IF NOT EXISTS idx_emb_kb_hnsw_anchor
  ON ai.embeddings_kb_knowledge USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'kb_anchor';
CREATE INDEX IF NOT EXISTS idx_emb_kb_hnsw_body
  ON ai.embeddings_kb_knowledge USING hnsw (embedding vector_cosine_ops)
  WHERE chunk_type = 'kb_body';

-- Self-heal: widen a legacy narrow PK (without tenant_id). Idempotent.
DO $$
DECLARE pk_cols text; pk_name text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(i.indkey, a.attnum))
    INTO pk_cols
  FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
  WHERE i.indrelid='ai.embeddings_kb_knowledge'::regclass AND i.indisprimary;
  IF pk_cols IS NOT NULL AND position('tenant_id' in pk_cols) = 0 THEN
    SELECT conname INTO pk_name FROM pg_constraint
      WHERE conrelid='ai.embeddings_kb_knowledge'::regclass AND contype='p';
    EXECUTE 'ALTER TABLE ai.embeddings_kb_knowledge DROP CONSTRAINT '||quote_ident(pk_name);
    ALTER TABLE ai.embeddings_kb_knowledge
      ADD PRIMARY KEY (tenant_id, entity_id, chunk_type, chunk_index, embedding_version);
  END IF;
END $$;

DO $$
BEGIN
  PERFORM pgmq.create('embedding_refresh_kb_knowledge');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;

CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_kb()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.content_hash_kb IS DISTINCT FROM NEW.content_hash_kb THEN
    PERFORM pgmq.send('embedding_refresh_kb_knowledge', jsonb_build_object(
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
  AFTER INSERT OR UPDATE ON itsm.kb_knowledge
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_kb();

-- DELETE cleanup: when an article is removed, drop its chunks (no orphans).
CREATE OR REPLACE FUNCTION ai.cleanup_embeddings_kb()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM ai.embeddings_kb_knowledge
   WHERE entity_id = OLD.kb_id AND tenant_id = OLD.tenant_id;
  RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_kb_embedding_cleanup ON itsm.kb_knowledge;
CREATE TRIGGER trg_kb_embedding_cleanup
  AFTER DELETE ON itsm.kb_knowledge
  FOR EACH ROW
  EXECUTE FUNCTION ai.cleanup_embeddings_kb();
