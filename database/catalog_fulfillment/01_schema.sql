-- catalog_fulfillment/01_schema.sql  (uc08)
--
-- The catalog item + onboarding template tables + the catalog change-detection
-- doorbell. These must exist BEFORE the request slice (request FK ->
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
    PRIMARY KEY (tenant_id, catalog_item_id)
);

CREATE TABLE IF NOT EXISTS itsm.onboarding_template (
    tenant_id               text    NOT NULL,
    template_id             text    NOT NULL,
    name                    text    NOT NULL,
    description             text,
    department              text,
    default_catalog_item_id text,
    required_inputs         text[]  NOT NULL DEFAULT '{}',
    tasks                   jsonb   NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (tenant_id, template_id),
    FOREIGN KEY (tenant_id, default_catalog_item_id)
        REFERENCES itsm.catalog_item (tenant_id, catalog_item_id) ON DELETE SET NULL
);

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
          'Owner: '       || coalesce(owner_group, ''),
          'sha256'
        )
      ) STORED;
  END IF;
END$$;
