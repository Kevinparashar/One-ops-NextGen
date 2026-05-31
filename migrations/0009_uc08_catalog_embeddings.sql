-- Migration 0009 — UC-8 catalog embedding substrate (field-map-driven).
--
-- Production-grade design that handles 3 catalog template evolution
-- scenarios:
--   • Scenario A (new field added) — change is opt-in via field_map INSERT.
--   • Scenario B (new catalog type added) — same pipeline serves it, zero code change.
--   • Scenario C (field renamed) — UPDATE one field_map row, no code redeploy.
--
-- 4 components:
--   1) ai.embeddings_catalog_item — the vector store (mirrors ai.embeddings_incident shape)
--   2) ai.embedding_field_map — declarative mapping of (table, chunk_type, role) → column
--   3) itsm.catalog_item.content_hash_catalog — generated column, change detector
--   4) trigger + pgmq enqueue — hash-gated refresh path
--
-- Idempotent — re-runnable.

-- ── 1. Vector store ─────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ai.embeddings_catalog_item (
  entity_id          text         NOT NULL,   -- = catalog_item_id
  chunk_type         text         NOT NULL
    CHECK (chunk_type IN ('catalog_anchor', 'catalog_overflow')),
  chunk_index        int          NOT NULL DEFAULT 0,
  tenant_id          text         NOT NULL,
  content_text       text         NOT NULL,
  content_hash       bytea        NOT NULL,
  embedding          vector(1536) NOT NULL,
  embedding_model    text         NOT NULL,
  embedding_version  text         NOT NULL DEFAULT 'v1',
  embedded_at        timestamptz  NOT NULL DEFAULT now(),
  PRIMARY KEY (entity_id, chunk_type, chunk_index, embedding_version)
);

CREATE INDEX IF NOT EXISTS idx_emb_catalog_tenant
  ON ai.embeddings_catalog_item (tenant_id, chunk_type);

-- HNSW index for fast kNN on catalog_anchor (the primary search target).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname='ai' AND indexname='idx_emb_catalog_hnsw_anchor'
  ) THEN
    EXECUTE 'CREATE INDEX idx_emb_catalog_hnsw_anchor
             ON ai.embeddings_catalog_item
             USING hnsw (embedding vector_cosine_ops)
             WHERE chunk_type = ''catalog_anchor''';
  END IF;
END$$;


-- ── 2. Declarative field-map (production-grade, handles all 3 scenarios) ──
--
-- Each row declares: "for this (source_table, chunk_type), this field_role
-- comes from this source_column." The worker reads this at refresh time.
--
-- Scenario A — add a new field:
--   INSERT INTO ai.embedding_field_map VALUES
--     ('itsm.catalog_item', 'catalog_anchor', 'business_value',
--      'business_value', true, 5, 'v1');
--
-- Scenario C — rename a field:
--   UPDATE ai.embedding_field_map
--      SET source_column = 'responsible_team'
--    WHERE source_table = 'itsm.catalog_item'
--      AND chunk_type   = 'catalog_anchor'
--      AND field_role   = 'owner';
--
-- field_role is the STABLE conceptual label that goes into the embed text
-- ("Owner: …"); source_column is the volatile schema reference.

CREATE TABLE IF NOT EXISTS ai.embedding_field_map (
  source_table       text NOT NULL,           -- 'itsm.catalog_item'
  chunk_type         text NOT NULL,           -- 'catalog_anchor'
  field_role         text NOT NULL,           -- stable label: 'name'/'description'/'category'/'owner'/...
  source_column      text NOT NULL,           -- volatile: actual column name
  is_active          boolean NOT NULL DEFAULT true,
  ordinal            int NOT NULL DEFAULT 0,  -- ordering in concatenated text
  embedding_version  text NOT NULL DEFAULT 'v1',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_table, chunk_type, field_role, embedding_version)
);

CREATE INDEX IF NOT EXISTS idx_field_map_lookup
  ON ai.embedding_field_map (source_table, chunk_type, is_active, ordinal);

-- Default mapping for catalog_anchor — the 4 canonical fields we discussed.
-- Operators can INSERT additional rows or UPDATE source_column without
-- redeploying code.
INSERT INTO ai.embedding_field_map
  (source_table, chunk_type, field_role, source_column, is_active, ordinal)
VALUES
  ('itsm.catalog_item', 'catalog_anchor', 'name',        'name',        true, 1),
  ('itsm.catalog_item', 'catalog_anchor', 'description', 'description', true, 2),
  ('itsm.catalog_item', 'catalog_anchor', 'category',    'category',    true, 3),
  ('itsm.catalog_item', 'catalog_anchor', 'owner',       'owner_group', true, 4)
ON CONFLICT (source_table, chunk_type, field_role, embedding_version) DO NOTHING;


-- ── 3. Hash column on itsm.catalog_item (change detector) ────────────────
--
-- Covers a SUPERSET of fields we might embed today or tomorrow.
-- A change to any of these fields fires the trigger; the worker then
-- consults field_map to know what to actually include in the embed text.
--
-- NOTE: adding a brand-new column to itsm.catalog_item requires both
-- (a) the column add (DDL) and (b) ALTER on this generated column.
-- That's a migration regardless — PostgreSQL has no way to make a
-- generated column reference dynamic schema.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='itsm' AND table_name='catalog_item'
      AND column_name='content_hash_catalog'
  ) THEN
    ALTER TABLE itsm.catalog_item
      ADD COLUMN content_hash_catalog bytea
      GENERATED ALWAYS AS (
        digest(
          'Name: '        || coalesce(name, '')         || E'\n' ||
          'Description: ' || coalesce(description, '')  || E'\n' ||
          'Category: '    || coalesce(category, '')     || E'\n' ||
          'Owner: '       || coalesce(owner_group, ''),
          'sha256'
        )
      ) STORED;
  END IF;
END$$;


-- ── 4. Trigger + pgmq enqueue ───────────────────────────────────────────

CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_catalog_item()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  -- Hash-gated: only enqueue when content_hash_catalog changed (or first INSERT).
  IF (TG_OP = 'INSERT')
     OR (NEW.content_hash_catalog IS DISTINCT FROM OLD.content_hash_catalog)
  THEN
    PERFORM pgmq.send('embedding_refresh', jsonb_build_object(
      'target_table',  'ai.embeddings_catalog_item',
      'entity_id',     NEW.catalog_item_id,
      'tenant_id',     NEW.tenant_id,
      'chunk_type',    'catalog_anchor',
      'enqueued_hash', encode(NEW.content_hash_catalog, 'hex')
    ));
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_catalog_item_embedding_refresh ON itsm.catalog_item;
CREATE TRIGGER trg_catalog_item_embedding_refresh
  AFTER INSERT OR UPDATE ON itsm.catalog_item
  FOR EACH ROW
  EXECUTE FUNCTION ai.enqueue_embedding_refresh_catalog_item();
