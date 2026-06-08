-- conversation/01_schema.sql
--
-- conversation_events — append-only conversation event log (ARCHITECTURE.md §6).
-- No embeddings, no seed/sync — populated at runtime by the app (one INSERT per
-- turn; SELECT to replay; retention DELETE to prune). No UPDATE path: an event,
-- once written, is immutable. Lives in the public schema (not itsm). Idempotent.

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

-- Primary read path: every event for one session, in order.
CREATE INDEX IF NOT EXISTS ix_conv_events_session
    ON conversation_events (tenant_id, session_id, seq);

-- Retention prune path: delete a tenant's events older than a cutoff.
CREATE INDEX IF NOT EXISTS ix_conv_events_retention
    ON conversation_events (tenant_id, occurred_at_ms);
