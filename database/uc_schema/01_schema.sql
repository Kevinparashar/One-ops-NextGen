-- uc_schema/01_schema.sql
--
-- itsm.uc_schema — the request/envelope schema registry record, synced from
-- registries/v2/schemas/*.json. Global / tenant-agnostic. No embeddings
-- (structural). Same record shape as itsm.agent. (Named uc_schema, not "schema",
-- to avoid the SQL keyword.) Requires: _foundation (itsm schema, pgcrypto).
-- Idempotent.

CREATE TABLE IF NOT EXISTS itsm.uc_schema (
  schema_id     text        NOT NULL,
  version       int         NOT NULL,
  status        text        NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','draft','deprecated','retired')),
  body          jsonb       NOT NULL,
  content_hash  bytea       GENERATED ALWAYS AS (digest(body::text, 'sha256')) STORED,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (schema_id, version)
);

CREATE INDEX IF NOT EXISTS idx_uc_schema_status ON itsm.uc_schema (status);
