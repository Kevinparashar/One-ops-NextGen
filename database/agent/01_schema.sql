-- agent/01_schema.sql
--
-- itsm.agent — the agent registry record, synced from registries/v2/agents/*.json
-- (files stay the source of truth; this table is the runtime/DB mirror).
-- Global / tenant-agnostic. One row per (agent_id, version).
--   * body         = the full agent card (jsonb), verbatim from the file
--   * content_hash = doorbell over the EMBEDDED content ONLY (body->'skills').
--                    Metadata changes (domain, owner, abac_tags) must NOT
--                    trigger a re-embed — only edits to the embedded skill text
--                    (description / use_when / examples) do.
--   * domain       = generated from body->>'domain' (default 'itsm') — the
--                    routing scope, mirrored into ai.embeddings_agent.
-- The agent->tools link is body.tool_refs (no junction); see database/tool/.
-- Requires: _foundation (itsm schema, pgcrypto). Idempotent.

CREATE TABLE IF NOT EXISTS itsm.agent (
  agent_id      text        NOT NULL,
  version       int         NOT NULL,
  status        text        NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','draft','deprecated','retired')),
  body          jsonb       NOT NULL,
  content_hash  bytea       GENERATED ALWAYS AS (digest((body->'skills')::text, 'sha256')) STORED,
  domain        text        GENERATED ALWAYS AS (coalesce(body->>'domain', 'itsm')) STORED,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (agent_id, version)
);

-- Self-heal for already-deployed tables (generated columns can't be ALTERed in
-- place — drop + re-add). Idempotent: only acts when the definition differs.
-- Runs BEFORE the domain index (which needs the column to exist).
DO $$
DECLARE expr text;
BEGIN
  SELECT pg_get_expr(d.adbin, d.adrelid) INTO expr
  FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum
  WHERE a.attrelid='itsm.agent'::regclass AND a.attname='content_hash';
  IF expr IS NULL OR position('skills' in expr) = 0 THEN
    ALTER TABLE itsm.agent DROP COLUMN content_hash;
    ALTER TABLE itsm.agent ADD COLUMN content_hash bytea
      GENERATED ALWAYS AS (digest((body->'skills')::text, 'sha256')) STORED;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_schema='itsm' AND table_name='agent'
                   AND column_name='domain') THEN
    ALTER TABLE itsm.agent ADD COLUMN domain text
      GENERATED ALWAYS AS (coalesce(body->>'domain', 'itsm')) STORED;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agent_status ON itsm.agent (status);
CREATE INDEX IF NOT EXISTS idx_agent_domain ON itsm.agent (domain);
