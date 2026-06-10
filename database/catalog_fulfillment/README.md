# catalog_fulfillment/  (uc08)

Self-contained slice for the catalog item + its embeddings + the fulfillment
workflow tables. Two ordering touch-points because of the FK cycle with request:

- `01_schema.sql` (catalog_item) runs **before** the `request/` slice
  (request FK → catalog_item).
- `03_fulfillment.sql` runs **after** the `request/` slice
  (request_item FK → itsm.request).

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `catalog_item` + content_hash doorbell | before request |
| `02_embeddings.sql` | `ai.embeddings_catalog_item` + field-map + queue `embedding_refresh_catalog_item` + trigger | after 01 |
| `03_fulfillment.sql` | `request_item` / `task` / `approval` / `fulfillment_run` (RITM→SCTASK) | **after request/** |
| `load_data.py` | `catalog_item.json` → itsm | after 01 |
| `backfill.py` | embed all catalog items (field-map driven) | after 02 + load |
| `worker.py` | drains `embedding_refresh_catalog_item` → `ai.embeddings_catalog_item` | `python database/catalog_fulfillment/worker.py` |

Embeddable fields are declared in `ai.embedding_field_map`, so adding/renaming a
field is a data change (a field_map row), not a worker redeploy.
