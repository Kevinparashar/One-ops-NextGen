-- Migration 0008 — add 'failed' to itsm.request_item.state vocabulary.
--
-- Motivation: the original schema (0007) modelled terminal RITM states as
-- {fulfilled, cancelled, rejected}, omitting system-detected unrecoverable
-- failure. The UC-8 executor distinguishes three terminal causes:
--
--   • fulfilled — all tasks done
--   • cancelled — user-initiated abort + saga rollback
--   • failed    — system unable to complete (permanent adapter error,
--                 max waves exceeded, etc.)
--
-- Without `failed`, the executor's terminal write violates the CHECK
-- constraint and the orchestration aborts in CheckViolationError. This
-- migration widens the constraint; no data backfill needed since no
-- existing row holds a non-listed value.
--
-- Production-grade properties:
--   • Idempotent (DROP IF EXISTS + ADD CONSTRAINT).
--   • Tenant-agnostic (constraint is global; same vocabulary every tenant).
--   • Backwards-compatible (every previously-allowed value still allowed).

ALTER TABLE itsm.request_item
  DROP CONSTRAINT IF EXISTS request_item_state_check;

ALTER TABLE itsm.request_item
  ADD CONSTRAINT request_item_state_check
  CHECK (state = ANY (ARRAY[
    'requested',
    'approved',
    'in_progress',
    'fulfilled',
    'cancelled',
    'rejected',
    'failed'
  ]));
