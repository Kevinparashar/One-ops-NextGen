-- incident/01_schema.sql
--
-- The incident table — owned entirely by this service slice.
--   * base table (composite PK + composite FKs to foundation reference tables)
--   * search_tsv (UC keyword search) + content_hash_symptom / _diagnosis
--     (the two change-detection doorbells that drive the refresh trigger)
--   * its indexes
--
-- Requires: _foundation (itsm schema, sys_user/cmdb_ci/problem/change, pgcrypto).
-- Idempotent. Split verbatim from the original core schema + incident_columns.

CREATE TABLE IF NOT EXISTS itsm.incident (
    tenant_id         text        NOT NULL,
    incident_id       text        NOT NULL,
    title             text        NOT NULL,
    description       text,
    status            text,
    priority          text,
    severity          text,
    impact            text,
    urgency           text,
    category          text,
    subcategory       text,
    service_name      text,
    reported_by       text,
    assigned_to       text,
    assignment_group  text,
    ci_id             text,
    linked_ci_ids     text[]      NOT NULL DEFAULT '{}',
    related_problem   text,
    related_change    text,
    attachments       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    work_notes        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    comments          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    sla_due           timestamptz,
    sla_breached      boolean     NOT NULL DEFAULT false,
    created_at        timestamptz,
    updated_at        timestamptz,
    resolved_at       timestamptz,
    PRIMARY KEY (tenant_id, incident_id),
    FOREIGN KEY (tenant_id, reported_by)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, assigned_to)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, ci_id)
        REFERENCES itsm.cmdb_ci (tenant_id, ci_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, related_problem)
        REFERENCES itsm.problem (tenant_id, problem_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, related_change)
        REFERENCES itsm.change (tenant_id, change_id) ON DELETE SET NULL
);

-- Keyword search vector (title weighted above description).
ALTER TABLE itsm.incident
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
        GENERATED ALWAYS AS (
            setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(description, '')), 'B')
        ) STORED;

-- Change-detection doorbells (drive the refresh trigger in 02_embeddings.sql).
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

-- Indexes.
CREATE INDEX IF NOT EXISTS idx_incident_status   ON itsm.incident (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_incident_category ON itsm.incident (tenant_id, category);
CREATE INDEX IF NOT EXISTS idx_incident_assignee ON itsm.incident (tenant_id, assigned_to);
CREATE INDEX IF NOT EXISTS idx_incident_ci       ON itsm.incident (tenant_id, ci_id);
CREATE INDEX IF NOT EXISTS idx_incident_linkedci ON itsm.incident USING gin (linked_ci_ids);
CREATE INDEX IF NOT EXISTS idx_incident_tsv      ON itsm.incident USING gin (search_tsv);
