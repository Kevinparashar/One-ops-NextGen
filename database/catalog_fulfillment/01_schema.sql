-- catalog_fulfillment/01_schema.sql  (uc08)
--
-- The catalog item table + the catalog change-detection
-- doorbell. This must exist BEFORE the request slice (request FK ->
-- catalog_item). The fulfillment WORKFLOW tables (request_item/task/approval/
-- fulfillment_run) live in 03_fulfillment.sql and run AFTER request (they FK ->
-- itsm.request). See database/README.md for the global order.
--
-- Requires: _foundation (itsm schema, pgcrypto). Idempotent.

CREATE TABLE IF NOT EXISTS itsm.catalog_item (
    tenant_id               text    NOT NULL,
    catalog_item_id         text    NOT NULL,
    name                    text    NOT NULL,
    description             text,
    category                text,
    owner_group             text,
    estimated_total_minutes integer,
    tasks                   jsonb   NOT NULL DEFAULT '[]'::jsonb,
    -- request_fields: the per-item INTAKE FORM (what the agent asks the user).
    -- [{field_name,label,type,required,options?}]. Empty [] = not intake-ready
    -- (hidden from get_service_request_list). See uc08 intake tools.
    request_fields          jsonb   NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (tenant_id, catalog_item_id)
);
-- Idempotent add for pre-existing tables (CREATE IF NOT EXISTS skips them).
ALTER TABLE itsm.catalog_item
    ADD COLUMN IF NOT EXISTS request_fields jsonb NOT NULL DEFAULT '[]'::jsonb;

-- intent_keywords (2026-06-12): DERIVED discriminative phrasings of how a user
-- would ask for this item ("forgot password, locked out, can't log in" for
-- Password Reset). Recall-first embeddings over generic descriptions matched
-- many queries weakly (e.g. "Application Access" surfaced under "order a
-- headset"); embedding the intent keywords as a 5th field_map line sharpens
-- discrimination at the source. Populated by
-- database/catalog_fulfillment/derive_intent_keywords.py (LLM, hash-gated),
-- NOT hand-authored. Empty until derived; the embed-text builder skips NULLs.
ALTER TABLE itsm.catalog_item
    ADD COLUMN IF NOT EXISTS intent_keywords text;

-- Change-detection doorbell. Covers a SUPERSET of embeddable fields; the worker
-- consults ai.embedding_field_map (02_embeddings.sql) for what to actually embed.
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
          'Owner: '       || coalesce(owner_group, '')  || E'\n' ||
          'Intent keywords: ' || coalesce(intent_keywords, ''),
          'sha256'
        )
      ) STORED;
  END IF;
END$$;

-- Self-heal (2026-06-12): rebuild content_hash_catalog on already-deployed
-- tables whose generated expression predates intent_keywords. Generated columns
-- can't be ALTERed, so DROP + re-ADD. Without this the doorbell would not fire
-- when intent_keywords changes, so a live keyword update would not re-embed.
DO $$
DECLARE expr text;
BEGIN
  SELECT pg_get_expr(d.adbin, d.adrelid) INTO expr
  FROM pg_attrdef d JOIN pg_attribute a
    ON a.attrelid=d.adrelid AND a.attnum=d.adnum
  WHERE d.adrelid='itsm.catalog_item'::regclass AND a.attname='content_hash_catalog';
  IF expr IS NOT NULL AND position('intent_keywords' in expr) = 0 THEN
    ALTER TABLE itsm.catalog_item DROP COLUMN content_hash_catalog;
    ALTER TABLE itsm.catalog_item
      ADD COLUMN content_hash_catalog bytea
      GENERATED ALWAYS AS (
        digest(
          'Name: '        || coalesce(name, '')         || E'\n' ||
          'Description: ' || coalesce(description, '')  || E'\n' ||
          'Category: '    || coalesce(category, '')     || E'\n' ||
          'Owner: '       || coalesce(owner_group, '')  || E'\n' ||
          'Intent keywords: ' || coalesce(intent_keywords, ''),
          'sha256'
        )
      ) STORED;
  END IF;
END$$;

-- ── Lexical search vector (FTS branch of the HYBRID catalog retriever) ──────
-- Generated tsvector over the item's searchable text (name + description +
-- category), mirroring itsm.kb_knowledge.content_tsv. Auto-populates every row
-- and stays in sync on insert/update — no backfill, no trigger, no worker.
-- INDEPENDENT of content_hash_catalog (above) and of ai.embeddings_catalog_item,
-- so adding it NEVER changes the hash or triggers a re-embed. Consumed by
-- find_closest_catalog_items' FTS branch, RRF-fused with the dense (cosine)
-- branch before the LLM listwise reranker.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema='itsm' AND table_name='catalog_item'
      AND column_name='content_tsv'
  ) THEN
    ALTER TABLE itsm.catalog_item
      ADD COLUMN content_tsv tsvector
      GENERATED ALWAYS AS (
        to_tsvector('english',
          coalesce(name, '')        || ' ' ||
          coalesce(description, '')  || ' ' ||
          coalesce(category, ''))
      ) STORED;
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_catalog_tsv
  ON itsm.catalog_item USING gin (content_tsv);
