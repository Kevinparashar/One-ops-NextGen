-- ITSM application schema for NextGen-ai (POC-5-MW).
-- 10 entity tables in a dedicated `itsm` schema — kept apart from Supabase's
-- own auth/storage/realtime schemas. Multi-tenant: every table is keyed by
-- (tenant_id, <entity_id>), so a row physically belongs to exactly one tenant
-- and a point lookup is the primary-key index itself.
--
-- Design notes:
--   * Composite PK (tenant_id, id) — tenant scoping is structural, not advisory.
--   * Composite FKs (tenant_id, ref) — a reference must resolve AND stay in the
--     same tenant; cross-tenant references are rejected by the database.
--   * Object-arrays (work_notes, tasks, relationships, ...) -> jsonb.
--     Id-arrays (linked_ci_ids, related_incidents, tags, ...) -> text[] so they
--     are GIN-indexable with array containment.
--   * kb_knowledge carries a generated tsvector for UC-3 keyword search. The
--     pgvector embedding column is deliberately deferred to a later migration.
--   * Tenant isolation is enforced by the application's WHERE clause and by
--     these composite keys; Postgres RLS is intentionally NOT used (the app
--     connects with a single service role). RLS can be layered on later.
--
-- This file is reviewed before it is ever run. No data is inserted here.

CREATE SCHEMA IF NOT EXISTS itsm;

-- ── sys_user ─────────────────────────────────────────────────────────────
CREATE TABLE itsm.sys_user (
    tenant_id    text        NOT NULL,
    user_id      text        NOT NULL,
    name         text        NOT NULL,
    email        text        NOT NULL,
    role         text        NOT NULL,
    department   text,
    location     text,
    manager_id   text,
    vip          boolean     NOT NULL DEFAULT false,
    locale       text        NOT NULL DEFAULT 'en',
    is_active    boolean     NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, user_id),
    -- Self-referential hierarchy: DEFERRABLE so a bulk load (or a transaction
    -- inserting a manager and report together) is checked at COMMIT, not
    -- row-by-row — file/insert order then does not matter.
    FOREIGN KEY (tenant_id, manager_id)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED
);

-- ── catalog_item ─────────────────────────────────────────────────────────
CREATE TABLE itsm.catalog_item (
    tenant_id               text    NOT NULL,
    catalog_item_id         text    NOT NULL,
    name                    text    NOT NULL,
    description             text,
    category                text,
    owner_group             text,
    estimated_total_minutes integer,
    tasks                   jsonb   NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (tenant_id, catalog_item_id)
);

-- ── onboarding_template ──────────────────────────────────────────────────
CREATE TABLE itsm.onboarding_template (
    tenant_id               text    NOT NULL,
    template_id             text    NOT NULL,
    name                    text    NOT NULL,
    description             text,
    department              text,
    default_catalog_item_id text,
    required_inputs         text[]  NOT NULL DEFAULT '{}',
    tasks                   jsonb   NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (tenant_id, template_id),
    FOREIGN KEY (tenant_id, default_catalog_item_id)
        REFERENCES itsm.catalog_item (tenant_id, catalog_item_id) ON DELETE SET NULL
);

-- ── cmdb_ci ──────────────────────────────────────────────────────────────
CREATE TABLE itsm.cmdb_ci (
    tenant_id     text    NOT NULL,
    ci_id         text    NOT NULL,
    ci_name       text    NOT NULL,
    ci_type       text,
    environment   text,
    status        text,
    owner         text,
    location      text,
    criticality   text,
    relationships jsonb   NOT NULL DEFAULT '[]'::jsonb,  -- [{type, target_ci_id}]
    attributes    jsonb   NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, ci_id),
    FOREIGN KEY (tenant_id, owner)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL
);

-- ── asset ────────────────────────────────────────────────────────────────
CREATE TABLE itsm.asset (
    tenant_id        text   NOT NULL,
    asset_id         text   NOT NULL,
    asset_name       text   NOT NULL,
    asset_class      text,
    subtype          text,
    model            text,
    vendor           text,
    serial_number    text,
    assigned_to      text,
    linked_ci        text,
    location         text,
    status           text,
    purchase_date    date,
    warranty_expiry  date,
    PRIMARY KEY (tenant_id, asset_id),
    FOREIGN KEY (tenant_id, assigned_to)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, linked_ci)
        REFERENCES itsm.cmdb_ci (tenant_id, ci_id) ON DELETE SET NULL
);

-- ── problem ──────────────────────────────────────────────────────────────
CREATE TABLE itsm.problem (
    tenant_id          text        NOT NULL,
    problem_id         text        NOT NULL,
    title              text        NOT NULL,
    description        text,
    status             text,
    priority           text,
    category           text,
    root_cause         text,
    workaround         text,
    known_error        boolean     NOT NULL DEFAULT false,
    related_incidents  text[]      NOT NULL DEFAULT '{}',
    related_changes    text[]      NOT NULL DEFAULT '{}',
    owner              text,
    created_at         timestamptz,
    updated_at         timestamptz,
    PRIMARY KEY (tenant_id, problem_id),
    FOREIGN KEY (tenant_id, owner)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL
);

-- ── change ───────────────────────────────────────────────────────────────
CREATE TABLE itsm.change (
    tenant_id         text        NOT NULL,
    change_id         text        NOT NULL,
    title             text        NOT NULL,
    description       text,
    state             text,
    change_type       text,
    risk_level        text,
    impact            text,
    approval_status   text,
    approved_by       text[]      NOT NULL DEFAULT '{}',
    requested_by      text,
    assigned_to       text,
    assignment_group  text,
    affected_ci       text[]      NOT NULL DEFAULT '{}',
    related_problem   text,
    planned_start     timestamptz,
    planned_end       timestamptz,
    actual_start      timestamptz,
    actual_end        timestamptz,
    created_at        timestamptz,
    updated_at        timestamptz,
    PRIMARY KEY (tenant_id, change_id),
    FOREIGN KEY (tenant_id, requested_by)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, assigned_to)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL,
    FOREIGN KEY (tenant_id, related_problem)
        REFERENCES itsm.problem (tenant_id, problem_id) ON DELETE SET NULL
);

-- ── incident ─────────────────────────────────────────────────────────────
CREATE TABLE itsm.incident (
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

-- ── request ──────────────────────────────────────────────────────────────
CREATE TABLE itsm.request (
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

-- ── kb_knowledge ─────────────────────────────────────────────────────────
-- UC-3 lookup target. The pgvector `embedding` column for semantic search is
-- deferred — it will be added in a later migration once the embedding model
-- is chosen. For now UC-3 search runs on the keyword `content_tsv` index.
CREATE TABLE itsm.kb_knowledge (
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

-- ── indexes — tuned for UC-1 (record lookup/filter) and UC-3 (search) ────
-- The composite PK already serves point lookups and tenant-scoped scans.

CREATE INDEX idx_incident_status   ON itsm.incident (tenant_id, status);
CREATE INDEX idx_incident_category ON itsm.incident (tenant_id, category);
CREATE INDEX idx_incident_assignee ON itsm.incident (tenant_id, assigned_to);
CREATE INDEX idx_incident_ci       ON itsm.incident (tenant_id, ci_id);
CREATE INDEX idx_incident_linkedci ON itsm.incident USING gin (linked_ci_ids);

CREATE INDEX idx_request_status    ON itsm.request (tenant_id, status);
CREATE INDEX idx_change_state      ON itsm.change (tenant_id, state);
CREATE INDEX idx_problem_status    ON itsm.problem (tenant_id, status);
CREATE INDEX idx_problem_relinc    ON itsm.problem USING gin (related_incidents);

CREATE INDEX idx_kb_state          ON itsm.kb_knowledge (tenant_id, state);
CREATE INDEX idx_kb_category       ON itsm.kb_knowledge (tenant_id, category);
CREATE INDEX idx_kb_tags           ON itsm.kb_knowledge USING gin (tags);
CREATE INDEX idx_kb_relinc         ON itsm.kb_knowledge USING gin (related_incidents);
-- Keyword search for UC-3 (semantic/vector search added in a later migration).
CREATE INDEX idx_kb_tsv            ON itsm.kb_knowledge USING gin (content_tsv);
