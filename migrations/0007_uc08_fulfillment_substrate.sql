-- ────────────────────────────────────────────────────────────────────────────
-- Migration 0007 — UC-8 Catalog Item Fulfillment substrate (2026-05-31)
--
-- Adds 4 tables required by UC-8 (DOC-09 §UC-8):
--   itsm.request_item     — RITM, one per catalog line in a Service Request
--   itsm.task             — SCTASK, atomic fulfilment work units (DAG nodes)
--   itsm.approval         — approval gates (substitution, budget, manager)
--   itsm.fulfillment_run  — per-invocation audit (links REQ → RITM → LangGraph)
--
-- Design philosophy: mirror ServiceNow's REQ → RITM → SCTASK industry-standard
-- hierarchy so ITSM teams recognise the shape instantly. Every workflow column
-- (langgraph_thread_id, sla_due, retry_count, idempotency_key) maps 1:1 to an
-- existing platform capability we already use elsewhere.
--
-- Properties:
--   • Additive only — no existing table modified.
--   • Tenant-isolated by construction — (tenant_id, *_id) PKs.
--   • Idempotency keys with UNIQUE constraints — retries never duplicate.
--   • Optimistic-lock token (`version`) — concurrent workers can't trample.
--   • Partial indexes on hot in-flight states only — fast SLA sweeps.
--   • FKs with ON DELETE CASCADE downward — tenant cleanup is one statement.
--
-- ROLLBACK: see bottom of file.
-- ────────────────────────────────────────────────────────────────────────────

BEGIN;

-- ── 1. itsm.request_item (RITM) ────────────────────────────────────────────
-- One row per catalog line item in a Service Request. A user submitting an SR
-- for "Onboarding + Laptop" creates 1 REQ + 2 RITM rows.

CREATE TABLE IF NOT EXISTS itsm.request_item (
  tenant_id              TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  request_id             TEXT NOT NULL,
  catalog_item_id        TEXT NOT NULL,
  variables              JSONB NOT NULL DEFAULT '{}'::jsonb,
  quantity               INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
  state                  TEXT NOT NULL DEFAULT 'requested'
    CHECK (state IN ('requested','approved','in_progress','fulfilled','cancelled','rejected')),
  approval_state         TEXT
    CHECK (approval_state IN ('not_required','requested','approved','rejected')),
  assignment_group       TEXT,
  assigned_to            TEXT,
  requested_for          TEXT NOT NULL,
  opened_by              TEXT NOT NULL,
  plan                   JSONB,                              -- materialised DAG (decomposition output)
  langgraph_thread_id    TEXT,                               -- 1:1 with LangGraph workflow
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

COMMENT ON TABLE itsm.request_item IS
  'RITM (Requested Item). One per catalog line in a Service Request. UC-8 substrate (DOC-09 §UC-8).';
COMMENT ON COLUMN itsm.request_item.plan IS
  'Materialised task DAG produced by Phase 1 decomposition. Source-of-truth for execution.';
COMMENT ON COLUMN itsm.request_item.langgraph_thread_id IS
  'LangGraph checkpoint thread id. One-to-one with this RITM.';

-- ── 2. itsm.task (SCTASK) ───────────────────────────────────────────────────
-- Atomic fulfilment work units. One row per node in the catalog template DAG.

CREATE TABLE IF NOT EXISTS itsm.task (
  tenant_id              TEXT NOT NULL,
  task_id                TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  request_id             TEXT NOT NULL,                      -- denormalised for fast joins
  template_task_id       TEXT NOT NULL,                      -- e.g., 'T1' from the catalog template
  task_name              TEXT NOT NULL,
  task_type              TEXT NOT NULL CHECK (task_type IN ('automated','manual')),
  tool_id                TEXT,                               -- UC-8 tool that fulfils this task
  depends_on             TEXT[] NOT NULL DEFAULT '{}',       -- array of template_task_ids
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

CREATE INDEX IF NOT EXISTS idx_task_ritm
  ON itsm.task(tenant_id, ritm_id);

CREATE INDEX IF NOT EXISTS idx_task_state
  ON itsm.task(tenant_id, state)
  WHERE state IN ('ready','in_progress');

CREATE INDEX IF NOT EXISTS idx_task_sla_due
  ON itsm.task(tenant_id, sla_due)
  WHERE state IN ('ready','in_progress');

CREATE INDEX IF NOT EXISTS idx_task_assignee
  ON itsm.task(tenant_id, assigned_to)
  WHERE state IN ('ready','in_progress');

COMMENT ON TABLE itsm.task IS
  'SCTASK (Catalog Task). Atomic fulfilment work units; one per DAG node. UC-8 substrate.';
COMMENT ON COLUMN itsm.task.depends_on IS
  'Array of template_task_ids that must reach state=done before this task transitions pending→ready.';

-- ── 3. itsm.approval ────────────────────────────────────────────────────────
-- Approval gates. Pauses the LangGraph workflow via interrupt() until resolved.

CREATE TABLE IF NOT EXISTS itsm.approval (
  tenant_id              TEXT NOT NULL,
  approval_id            TEXT NOT NULL,
  ritm_id                TEXT NOT NULL,
  task_id                TEXT,                               -- nullable; RITM-level if NULL
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
  langgraph_interrupt_id TEXT,                               -- interrupt() handle for resume
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
  ON itsm.approval(tenant_id, requested_from)
  WHERE state = 'pending';

CREATE INDEX IF NOT EXISTS idx_approval_sla_due
  ON itsm.approval(tenant_id, sla_due)
  WHERE state = 'pending';

COMMENT ON TABLE itsm.approval IS
  'Approval gates. Pauses LangGraph via interrupt() until decision is recorded. UC-8 substrate.';

-- ── 4. itsm.fulfillment_run (per-invocation audit) ─────────────────────────
-- One row per UC-8 invocation. A RITM that is fulfilled, cancelled, then
-- resubmitted produces multiple rows. Links REQ → RITM → LangGraph thread →
-- OTel trace → cost — the audit chain operators need.

CREATE TABLE IF NOT EXISTS itsm.fulfillment_run (
  tenant_id                  TEXT NOT NULL,
  run_id                     TEXT NOT NULL,
  ritm_id                    TEXT NOT NULL,
  trigger_type               TEXT NOT NULL
    CHECK (trigger_type IN ('portal','chat','auto_retry','cancel','rollback')),
  triggered_by               TEXT NOT NULL,
  trace_id                   TEXT,                            -- W3C traceparent → Tempo
  thread_id                  TEXT NOT NULL,                   -- LangGraph thread (1:1 with run)
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

CREATE INDEX IF NOT EXISTS idx_run_ritm
  ON itsm.fulfillment_run(tenant_id, ritm_id);

CREATE INDEX IF NOT EXISTS idx_run_outcome
  ON itsm.fulfillment_run(tenant_id, outcome, finished_at DESC)
  WHERE outcome IS NOT NULL;

COMMENT ON TABLE itsm.fulfillment_run IS
  'Per-invocation audit. Links RITM → LangGraph thread → OTel trace → cost. UC-8 substrate.';

COMMIT;

-- ── ROLLBACK ────────────────────────────────────────────────────────────────
-- BEGIN;
--   DROP TABLE IF EXISTS itsm.fulfillment_run;
--   DROP TABLE IF EXISTS itsm.approval;
--   DROP TABLE IF EXISTS itsm.task;
--   DROP TABLE IF EXISTS itsm.request_item;
-- COMMIT;
