-- tool/01_schema.sql
--
-- itsm.tool — the tool registry record, synced from registries/v2/tools/**/*.json.
-- Global / tenant-agnostic. NO embeddings: tool selection is deterministic
-- (explicit tool_id + parameter-shape), so nothing queries tools by vector.
--   * agent_id = the owning agent (the tools/<agent_id>/ folder). The agent->tools
--     direction is itsm.agent.body.tool_refs. Soft reference (not a DB FK): the
--     registry is versioned + card-driven, validated at boot by check_integrity.
-- Requires: _foundation (itsm schema, pgcrypto). Idempotent.

CREATE TABLE IF NOT EXISTS itsm.tool (
  tool_id       text        NOT NULL,
  version       int         NOT NULL,
  agent_id      text        NOT NULL,
  status        text        NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','draft','deprecated','retired')),
  body          jsonb       NOT NULL,
  content_hash  bytea       GENERATED ALWAYS AS (digest(body::text, 'sha256')) STORED,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tool_id, version)
);

CREATE INDEX IF NOT EXISTS idx_tool_agent  ON itsm.tool (agent_id);
CREATE INDEX IF NOT EXISTS idx_tool_status ON itsm.tool (status);
