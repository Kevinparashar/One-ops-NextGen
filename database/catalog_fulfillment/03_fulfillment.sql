-- catalog_fulfillment/03_fulfillment.sql  (uc08)
--
-- The fulfillment WORKFLOW tables. These FK -> itsm.request and itsm.catalog_item,
-- so run them AFTER the request slice (and after 01_schema here). Mirrors the
-- ServiceNow REQ -> RITM -> SCTASK hierarchy.
--   request_item (RITM) · task (SCTASK) · approval · fulfillment_run (audit)
-- The request_item.state vocabulary already includes 'failed' (folded in from
-- the old states migration). Additive, idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS itsm.request_item (
  tenant_id              TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  request_id             TEXT NOT NULL,
  catalog_item_id        TEXT NOT NULL,
  variables              JSONB NOT NULL DEFAULT '{}'::jsonb,
  quantity               INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
  state                  TEXT NOT NULL DEFAULT 'requested'
    CHECK (state IN ('requested','approved','in_progress','fulfilled','cancelled','rejected','failed')),
  approval_state         TEXT
    CHECK (approval_state IN ('not_required','requested','approved','rejected')),
  assignment_group       TEXT,
  assigned_to            TEXT,
  requested_for          TEXT NOT NULL,
  opened_by              TEXT NOT NULL,
  plan                   JSONB,
  langgraph_thread_id    TEXT,
  total_tasks            INTEGER NOT NULL DEFAULT 0,
  completed_tasks        INTEGER NOT NULL DEFAULT 0,
  failed_tasks           INTEGER NOT NULL DEFAULT 0,
  sla_due                TIMESTAMPTZ,
  sla_breached           BOOLEAN NOT NULL DEFAULT FALSE,
  estimated_completion   TIMESTAMPTZ,
  opened_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  approved_at            TIMESTAMPTZ,
  started_at             TIMESTAMPTZ,
  fulfilled_at           TIMESTAMPTZ,
  closed_at              TIMESTAMPTZ,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key        TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, ritm_id),
  FOREIGN KEY (tenant_id, request_id)
    REFERENCES itsm.request(tenant_id, request_id),
  FOREIGN KEY (tenant_id, catalog_item_id)
    REFERENCES itsm.catalog_item(tenant_id, catalog_item_id),
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_ritm_request
  ON itsm.request_item(tenant_id, request_id);
CREATE INDEX IF NOT EXISTS idx_ritm_state
  ON itsm.request_item(tenant_id, state)
  WHERE state NOT IN ('fulfilled','cancelled','rejected');
CREATE INDEX IF NOT EXISTS idx_ritm_sla_due
  ON itsm.request_item(tenant_id, sla_due)
  WHERE state IN ('requested','approved','in_progress');

CREATE TABLE IF NOT EXISTS itsm.task (
  tenant_id              TEXT NOT NULL,
  task_id                TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  request_id             TEXT NOT NULL,
  template_task_id       TEXT NOT NULL,
  task_name              TEXT NOT NULL,
  task_type              TEXT NOT NULL CHECK (task_type IN ('automated','manual')),
  tool_id                TEXT,
  depends_on             TEXT[] NOT NULL DEFAULT '{}',
  assignment_group       TEXT,
  assigned_to            TEXT,
  state                  TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','ready','in_progress','done','failed','skipped','blocked')),
  retry_count            INTEGER NOT NULL DEFAULT 0,
  max_retries            INTEGER NOT NULL DEFAULT 3,
  input_payload          JSONB,
  output_payload         JSONB,
  error_message          TEXT,
  error_code             TEXT,
  sla_minutes            INTEGER,
  sla_due                TIMESTAMPTZ,
  sla_breached           BOOLEAN NOT NULL DEFAULT FALSE,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  ready_at               TIMESTAMPTZ,
  started_at             TIMESTAMPTZ,
  finished_at            TIMESTAMPTZ,
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  idempotency_key        TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, task_id),
  FOREIGN KEY (tenant_id, ritm_id)
    REFERENCES itsm.request_item(tenant_id, ritm_id) ON DELETE CASCADE,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_task_ritm     ON itsm.task(tenant_id, ritm_id);
CREATE INDEX IF NOT EXISTS idx_task_state    ON itsm.task(tenant_id, state)
  WHERE state IN ('ready','in_progress');
CREATE INDEX IF NOT EXISTS idx_task_sla_due  ON itsm.task(tenant_id, sla_due)
  WHERE state IN ('ready','in_progress');
CREATE INDEX IF NOT EXISTS idx_task_assignee ON itsm.task(tenant_id, assigned_to)
  WHERE state IN ('ready','in_progress');

CREATE TABLE IF NOT EXISTS itsm.approval (
  tenant_id              TEXT NOT NULL,
  approval_id            TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  task_id                TEXT,
  approval_type          TEXT NOT NULL
    CHECK (approval_type IN ('substitution','budget','manager','security','catalog_owner')),
  reason                 TEXT NOT NULL,
  payload                JSONB,
  state                  TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','approved','rejected','expired','withdrawn')),
  decision               TEXT CHECK (decision IN ('approved','rejected')),
  decision_comment       TEXT,
  requested_from         TEXT NOT NULL,
  decided_by             TEXT,
  notify_channel         TEXT CHECK (notify_channel IN ('email','slack','in_app')),
  langgraph_interrupt_id TEXT,
  sla_due                TIMESTAMPTZ,
  sla_breached           BOOLEAN NOT NULL DEFAULT FALSE,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  decided_at             TIMESTAMPTZ,
  expires_at             TIMESTAMPTZ,
  idempotency_key        TEXT,
  version                INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (tenant_id, approval_id),
  FOREIGN KEY (tenant_id, ritm_id)
    REFERENCES itsm.request_item(tenant_id, ritm_id) ON DELETE CASCADE,
  FOREIGN KEY (tenant_id, task_id)
    REFERENCES itsm.task(tenant_id, task_id) ON DELETE CASCADE,
  UNIQUE (tenant_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_approval_pending
  ON itsm.approval(tenant_id, requested_from) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_approval_sla_due
  ON itsm.approval(tenant_id, sla_due) WHERE state = 'pending';

CREATE TABLE IF NOT EXISTS itsm.fulfillment_run (
  tenant_id                  TEXT NOT NULL,
  run_id                     TEXT NOT NULL,
  ritm_id                    TEXT NOT NULL,
  trigger_type               TEXT NOT NULL
    CHECK (trigger_type IN ('portal','chat','auto_retry','cancel','rollback')),
  triggered_by               TEXT NOT NULL,
  trace_id                   TEXT,
  thread_id                  TEXT NOT NULL,
  checkpoint_count           INTEGER NOT NULL DEFAULT 0,
  outcome                    TEXT
    CHECK (outcome IN ('fulfilled','partial','failed','cancelled')),
  outcome_summary            JSONB,
  decomposition_tokens       INTEGER,
  decomposition_cost_micros  INTEGER,
  started_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at                TIMESTAMPTZ,
  duration_ms                INTEGER,
  PRIMARY KEY (tenant_id, run_id),
  FOREIGN KEY (tenant_id, ritm_id)
    REFERENCES itsm.request_item(tenant_id, ritm_id)
);
CREATE INDEX IF NOT EXISTS idx_run_ritm    ON itsm.fulfillment_run(tenant_id, ritm_id);
CREATE INDEX IF NOT EXISTS idx_run_outcome ON itsm.fulfillment_run(tenant_id, outcome, finished_at DESC)
  WHERE outcome IS NOT NULL;

COMMIT;
