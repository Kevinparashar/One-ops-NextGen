-- catalog_fulfillment/02_embeddings.sql  (uc08)
--
-- The catalog vector store + declarative field-map + OWN queue + trigger.
-- Field-map-driven: a new embeddable field is a field_map INSERT, a rename is a
-- field_map UPDATE — no worker redeploy. Drained by
-- database/catalog_fulfillment/worker.py. Requires: 01_schema.sql + _foundation.
-- Idempotent.

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
  -- tenant_id is in the PK: entity ids are unique only per-tenant (§2.4).
  PRIMARY KEY (tenant_id, entity_id, chunk_type, chunk_index, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_emb_catalog_tenant
  ON ai.embeddings_catalog_item (tenant_id, chunk_type);

-- Self-heal: widen a legacy narrow PK (without tenant_id). Idempotent.
DO $$
DECLARE pk_cols text; pk_name text;
BEGIN
  SELECT string_agg(a.attname, ',' ORDER BY array_position(i.indkey, a.attnum))
    INTO pk_cols
  FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
  WHERE i.indrelid='ai.embeddings_catalog_item'::regclass AND i.indisprimary;
  IF pk_cols IS NOT NULL AND position('tenant_id' in pk_cols) = 0 THEN
    SELECT conname INTO pk_name FROM pg_constraint
      WHERE conrelid='ai.embeddings_catalog_item'::regclass AND contype='p';
    EXECUTE 'ALTER TABLE ai.embeddings_catalog_item DROP CONSTRAINT '||quote_ident(pk_name);
    ALTER TABLE ai.embeddings_catalog_item
      ADD PRIMARY KEY (tenant_id, entity_id, chunk_type, chunk_index, embedding_version);
  END IF;
END $$;
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

-- Declarative field-map (handles add/rename/new-type without code).
CREATE TABLE IF NOT EXISTS ai.embedding_field_map (
  source_table       text NOT NULL,
  chunk_type         text NOT NULL,
  field_role         text NOT NULL,
  source_column      text NOT NULL,
  is_active          boolean NOT NULL DEFAULT true,
  ordinal            int NOT NULL DEFAULT 0,
  embedding_version  text NOT NULL DEFAULT 'v1',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (source_table, chunk_type, field_role, embedding_version)
);
CREATE INDEX IF NOT EXISTS idx_field_map_lookup
  ON ai.embedding_field_map (source_table, chunk_type, is_active, ordinal);

INSERT INTO ai.embedding_field_map
  (source_table, chunk_type, field_role, source_column, is_active, ordinal)
VALUES
  ('itsm.catalog_item', 'catalog_anchor', 'name',        'name',        true, 1),
  ('itsm.catalog_item', 'catalog_anchor', 'description', 'description', true, 2),
  ('itsm.catalog_item', 'catalog_anchor', 'category',    'category',    true, 3),
  ('itsm.catalog_item', 'catalog_anchor', 'owner',       'owner_group', true, 4),
  -- 2026-06-12: derived discriminative phrasings (derive_intent_keywords.py).
  -- Sharpens the anchor so a query matches the item a user actually means.
  ('itsm.catalog_item', 'catalog_anchor', 'intent_keywords', 'intent_keywords', true, 5)
ON CONFLICT (source_table, chunk_type, field_role, embedding_version) DO NOTHING;

DO $$
BEGIN
  PERFORM pgmq.create('embedding_refresh_catalog_item');
EXCEPTION WHEN OTHERS THEN
  NULL;
END $$;

CREATE OR REPLACE FUNCTION ai.enqueue_embedding_refresh_catalog_item()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (TG_OP = 'INSERT')
     OR (NEW.content_hash_catalog IS DISTINCT FROM OLD.content_hash_catalog)
  THEN
    PERFORM pgmq.send('embedding_refresh_catalog_item', jsonb_build_object(
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

-- DELETE cleanup: when a catalog item is removed, drop its embeddings (no orphans).
CREATE OR REPLACE FUNCTION ai.cleanup_embeddings_catalog_item()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  DELETE FROM ai.embeddings_catalog_item
   WHERE entity_id = OLD.catalog_item_id AND tenant_id = OLD.tenant_id;
  RETURN OLD;
END $$;

DROP TRIGGER IF EXISTS trg_catalog_item_embedding_cleanup ON itsm.catalog_item;
CREATE TRIGGER trg_catalog_item_embedding_cleanup
  AFTER DELETE ON itsm.catalog_item
  FOR EACH ROW
  EXECUTE FUNCTION ai.cleanup_embeddings_catalog_item();
