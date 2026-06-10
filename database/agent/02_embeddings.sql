-- agent/02_embeddings.sql
--
-- ai.embeddings_agent — multi-chunk vectors over the agent skill body, for
-- retrieve-then-decide routing. GLOBAL (no tenant_id; agent_id-keyed). One row
-- per facet: chunk_type in description / use_when / example. not_when is NOT
-- embedded (LLM-disambiguator only — embedding a "don't pick me" phrase would
-- attract the wrong queries).
--
-- OWN queue `embedding_refresh_agent` + trigger. Drained by
-- database/agent/worker.py. Requires: 01_schema.sql + _foundation. Idempotent.

CREATE TABLE IF NOT EXISTS ai.embeddings_agent (
  agent_id          text         NOT NULL,
  chunk_type        text         NOT NULL
    CHECK (chunk_type IN ('description','use_when','example')),
  chunk_index       int          NOT NULL DEFAULT 0,
  domain            text         NOT NULL DEFAULT 'itsm',  -- itsm | itom[.subdomain] — for domain-scoped retrieval at scale
  content_text      text         NOT NULL,
  embedding         vector(1536) NOT NULL,
  content_hash      bytea        NOT NULL,
  embedding_model   text         NOT NULL,
  embedding_version text         NOT NULL DEFAULT 'v1',
  embedded_at       timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, chunk_type, chunk_index, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_agent_hnsw
  ON ai.embeddings_agent USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_emb_agent_type
  ON ai.embeddings_agent (chunk_type);

-- Self-heal: add the domain column to an already-deployed table. Idempotent.
ALTER TABLE ai.embeddings_agent
  ADD COLUMN IF NOT EXISTS domain text NOT NULL DEFAULT 'itsm';
CREATE INDEX IF NOT EXISTS idx_emb_agent_domain
  ON ai.embeddings_agent (domain);

DO $$
BEGIN
  PERFORM pgmq.create('embedding_refresh_agent');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;

-- Whole-body doorbell: any change to itsm.agent.content_hash enqueues the agent.
-- 'agent_all' sentinel — the worker rebuilds description + use_when + example.
CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_agent()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (TG_OP = 'INSERT')
     OR (NEW.content_hash IS DISTINCT FROM OLD.content_hash)
  THEN
    PERFORM pgmq.send('embedding_refresh_agent', jsonb_build_object(
      'target_table',  'ai.embeddings_agent',
      'entity_id',     NEW.agent_id,
      'chunk_type',    'agent_all',
      'enqueued_hash', encode(NEW.content_hash, 'hex')
    ));
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_agent_embedding_refresh ON itsm.agent;
CREATE TRIGGER trg_agent_embedding_refresh
  AFTER INSERT OR UPDATE ON itsm.agent
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_agent();

-- DELETE cleanup: agent is keyed (agent_id, version); only drop the agent's
-- embeddings when its LAST version row is removed (no orphans, no premature wipe).
CREATE OR REPLACE FUNCTION ai.cleanup_embeddings_agent()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM itsm.agent WHERE agent_id = OLD.agent_id) THEN
    DELETE FROM ai.embeddings_agent WHERE agent_id = OLD.agent_id;
  END IF;
  RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_agent_embedding_cleanup ON itsm.agent;
CREATE TRIGGER trg_agent_embedding_cleanup
  AFTER DELETE ON itsm.agent
  FOR EACH ROW
  EXECUTE FUNCTION ai.cleanup_embeddings_agent();
