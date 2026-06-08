-- kb/01_schema.sql
--
-- The kb_knowledge table — owned by this service slice.
-- Requires: _foundation (itsm schema, sys_user, pgcrypto). Idempotent.
-- Split verbatim from the original core schema + kb_columns.

CREATE TABLE IF NOT EXISTS itsm.kb_knowledge (
    tenant_id          text        NOT NULL,
    kb_id              text        NOT NULL,
    title              text        NOT NULL,
    summary            text,
    content            text,
    category           text,
    tags               text[]      NOT NULL DEFAULT '{}',
    state              text,                       -- draft | published | retired
    audience           text,                       -- all | end_user | technician
    created_by         text,
    created_at         timestamptz,
    updated_at         timestamptz,
    views              integer     NOT NULL DEFAULT 0,
    helpful_votes      integer     NOT NULL DEFAULT 0,
    related_ci_ids     text[]      NOT NULL DEFAULT '{}',
    related_incidents  text[]      NOT NULL DEFAULT '{}',
    content_tsv        tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title, '') || ' ' ||
            coalesce(summary, '') || ' ' ||
            coalesce(content, ''))) STORED,
    PRIMARY KEY (tenant_id, kb_id),
    FOREIGN KEY (tenant_id, created_by)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL
);

-- Single change-detection doorbell over the whole semantic surface — any change
-- regenerates all chunks (anchor + body). `tags` excluded (text[]::text not
-- IMMUTABLE; generated columns require immutable expressions).
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

CREATE INDEX IF NOT EXISTS idx_kb_state    ON itsm.kb_knowledge (tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_kb_category ON itsm.kb_knowledge (tenant_id, category);
CREATE INDEX IF NOT EXISTS idx_kb_tags     ON itsm.kb_knowledge USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_kb_relinc   ON itsm.kb_knowledge USING gin (related_incidents);
CREATE INDEX IF NOT EXISTS idx_kb_tsv      ON itsm.kb_knowledge USING gin (content_tsv);
