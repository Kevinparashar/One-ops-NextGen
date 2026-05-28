-- 0001_conversation_events.sql
-- P3 — append-only conversation event log (ARCHITECTURE.md §6, MIGRATION.md P3).
--
-- Applied by the operator's migration runner against the tenant/application
-- Postgres database. NOT applied by application code — the service never runs
-- DDL at runtime.
--
-- Append-only by contract: the application issues INSERT (append), SELECT
-- (read/replay), and a retention DELETE (prune). There is no UPDATE path — a
-- conversation event, once written, is immutable.

CREATE TABLE IF NOT EXISTS conversation_events (
    seq             BIGSERIAL    PRIMARY KEY,
    tenant_id       TEXT         NOT NULL,
    session_id      TEXT         NOT NULL,
    turn_index      BIGINT       NOT NULL,
    turn_role       TEXT         NOT NULL,
    -- Protobuf ConversationEvent payload (ADR-0001) — on-disk shape == on-wire.
    event_bytes     BYTEA        NOT NULL,
    occurred_at_ms  BIGINT       NOT NULL,           -- server time, ms (event order)
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Primary read path: every event for one session, in order, optionally from a
-- turn index. tenant_id leads the index so tenant isolation is index-enforced.
CREATE INDEX IF NOT EXISTS ix_conv_events_session
    ON conversation_events (tenant_id, session_id, seq);

-- Retention prune path: delete a tenant's events older than a cutoff.
CREATE INDEX IF NOT EXISTS ix_conv_events_retention
    ON conversation_events (tenant_id, occurred_at_ms);
