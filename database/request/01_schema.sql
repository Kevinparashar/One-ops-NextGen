-- request/01_schema.sql
--
-- The request table — owned by this service slice.
-- Requires: _foundation (sys_user, cmdb_ci) AND catalog_fulfillment/ first
-- (FK: catalog_item_id -> itsm.catalog_item). See database/README.md run order.
-- Idempotent. Split verbatim from the original core schema + request_columns.

CREATE TABLE IF NOT EXISTS itsm.request (
    tenant_id         text        NOT NULL,
    request_id        text        NOT NULL,
    title             text        NOT NULL,
    description       text,
    status            text,
    stage             text,
    priority          text,
    category          text,
    catalog_item_id   text,
    requested_for     text,
    requested_by      text,
    approved_by       text[]      NOT NULL DEFAULT '{}',
    assigned_to       text,
    assignment_group  text,
    ci_id             text,
    sla_due           timestamptz,
    sla_breached      boolean     NOT NULL DEFAULT false,
    comments          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    created_at        timestamptz,
    updated_at        timestamptz,
    fulfilled_at      timestamptz,
    PRIMARY KEY (tenant_id, request_id),
    FOREIGN KEY (tenant_id, requested_for)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, requested_by)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, assigned_to)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, catalog_item_id)
        REFERENCES itsm.catalog_item (tenant_id, catalog_item_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, ci_id)
        REFERENCES itsm.cmdb_ci (tenant_id, ci_id) ON DELETE SET NULL
);

ALTER TABLE itsm.request
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B')
        ) STORED;

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

CREATE INDEX IF NOT EXISTS idx_request_status ON itsm.request (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_request_tsv    ON itsm.request USING gin (search_tsv);
