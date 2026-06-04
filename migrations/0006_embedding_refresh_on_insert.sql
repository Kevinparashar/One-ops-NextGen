-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 0006 — embedding refresh on INSERT (close 2026-05-31 gap)
--
-- Issue: triggers 0004/0005 fire only on UPDATE. The guard
--   `IF OLD.content_hash_X IS DISTINCT FROM NEW.content_hash_X`
-- evaluates OLD as NULL on INSERT, and the IS-DISTINCT condition still
-- evaluates correctly (NULL IS DISTINCT FROM <value> = TRUE), but the
-- trigger event itself was registered only on UPDATE — so the function
-- never fires for new rows.
--
-- Real-world impact: new tickets, requests, and KB articles created via
-- INSERT (portal, email, API, bulk-load) never enqueued for embedding,
-- making them invisible to UC-2, UC-3, UC-5, and any future UC that
-- reads `ai.embeddings_<service>`.
--
-- Fix: replace the AFTER UPDATE trigger registration with
-- AFTER INSERT OR UPDATE. Function bodies already handle the no-OLD case
-- because `NULL IS DISTINCT FROM <hash>` is TRUE for any non-null hash.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── incident
DROP TRIGGER IF EXISTS trg_incident_embedding_refresh ON itsm.incident;
CREATE TRIGGER trg_incident_embedding_refresh
  AFTER INSERT OR UPDATE ON itsm.incident
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_incident();

-- ── request
DROP TRIGGER IF EXISTS trg_request_embedding_refresh ON itsm.request;
CREATE TRIGGER trg_request_embedding_refresh
  AFTER INSERT OR UPDATE ON itsm.request
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_request();

-- ── kb_knowledge
DROP TRIGGER IF EXISTS trg_kb_embedding_refresh ON itsm.kb_knowledge;
CREATE TRIGGER trg_kb_embedding_refresh
  AFTER INSERT OR UPDATE ON itsm.kb_knowledge
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_kb();

-- ─────────────────────────────────────────────────────────────────────────────
-- ROLLBACK:
--   DROP TRIGGER IF EXISTS trg_incident_embedding_refresh ON itsm.incident;
--   CREATE TRIGGER trg_incident_embedding_refresh
--     AFTER UPDATE ON itsm.incident
--     FOR EACH ROW EXECUTE FUNCTION ai.enqueue_embedding_refresh_incident();
--   -- (and the same for request + kb_knowledge)
-- ─────────────────────────────────────────────────────────────────────────────
