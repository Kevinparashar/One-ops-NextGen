-- _foundation/02_reference_tables.sql
--
-- The shared reference entities — FK-referenced by the service tables
-- (incident, request, ...) but owned by no single service. They have no
-- embeddings. Run after 01; before any service slice.
--
-- Multi-tenant: composite PK (tenant_id, <id>); composite FKs keep a reference
-- in the same tenant (cross-tenant references are rejected by the database).
-- Split verbatim from the original itsm_core_schema.sql (object-parity).
--
-- Load order (FK deps): sys_user -> cmdb_ci -> asset -> problem -> change.

-- ── sys_user ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS itsm.sys_user (
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
    -- Self-referential hierarchy: DEFERRABLE so a bulk load (manager + report
    -- in one transaction) is checked at COMMIT, not row-by-row.
    FOREIGN KEY (tenant_id, manager_id)
        REFERENCES itsm.sys_user (tenant_id, user_id) ON DELETE SET NULL
        DEFERRABLE INITIALLY DEFERRED
);

-- ── cmdb_ci ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS itsm.cmdb_ci (
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
CREATE TABLE IF NOT EXISTS itsm.asset (
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
CREATE TABLE IF NOT EXISTS itsm.problem (
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
CREATE TABLE IF NOT EXISTS itsm.change (
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

-- ── indexes (reference tables) ───────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_change_state   ON itsm.change (tenant_id, state);
CREATE INDEX IF NOT EXISTS idx_problem_status ON itsm.problem (tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_problem_relinc ON itsm.problem USING gin (related_incidents);
