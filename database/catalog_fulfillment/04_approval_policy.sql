-- catalog_fulfillment/04_approval_policy.sql  (uc08 — approval workflow, Phase 1)
--
-- The APPROVAL MATRIX (the rules) + a multi-stage column on itsm.approval.
-- Runs AFTER 03_fulfillment.sql (it ALTERs itsm.approval, created there).
-- Additive + idempotent: safe to re-run; touches nothing existing.
--
-- itsm.approval_policy is the deterministic, data-driven decision table:
-- rows of `match (conditions) -> stages (approver resolvers)`, evaluated
-- top-down by priority with a guaranteed fail-safe catch-all (match = '{}').
-- Nothing reads this table until UC08_APPROVAL_ENABLED is flipped on.
-- Design: docs/design/uc08-approval-workflow.md.

BEGIN;

CREATE TABLE IF NOT EXISTS itsm.approval_policy (
  tenant_id     TEXT    NOT NULL,
  policy_id     TEXT    NOT NULL,
  -- Lower priority is evaluated FIRST (most-specific rule wins). The fail-safe
  -- catch-all (match = '{}') must hold the highest priority number so it is the
  -- last row considered and always matches.
  priority      INTEGER NOT NULL,
  -- AND-ed conditions over the request's attributes (category, owner_group,
  -- item_id, …). '{}' matches anything (the catch-all). A key absent from
  -- `match` means "any value".
  match         JSONB   NOT NULL DEFAULT '{}'::jsonb,
  -- FALSE = explicit no-approval (self-service: password / MFA reset). This is
  -- the ONLY way a request auto-fulfils through the matrix — never the default.
  required      BOOLEAN NOT NULL DEFAULT TRUE,
  -- Sequential stages: [{name, approver:{type, id?}, rule, block_self_approval}]
  -- approver.type ∈ owning_group | manager_of_requester | group | user |
  --                 service_desk (fail-safe) | cost_center_owner|dynamic (P2)
  stages        JSONB   NOT NULL DEFAULT '[]'::jsonb,
  description   TEXT,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, policy_id)
);

-- Evaluation query is `WHERE tenant_id = $1 AND enabled ORDER BY priority`.
CREATE INDEX IF NOT EXISTS idx_approval_policy_eval
  ON itsm.approval_policy(tenant_id, priority)
  WHERE enabled;

-- Multi-stage support on the existing approval records (one stage per row).
-- DEFAULT 0 = the single Phase-1 stage; backfills existing rows harmlessly.
ALTER TABLE itsm.approval
  ADD COLUMN IF NOT EXISTS stage_index INTEGER NOT NULL DEFAULT 0;

COMMIT;
