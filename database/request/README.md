# request/

Self-contained slice for `itsm.request` + `ai.embeddings_request`. Requires
`_foundation/` **and** `catalog_fulfillment/` first (FK: `catalog_item_id →
itsm.catalog_item`).

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.request` + search_tsv + content_hash doorbells + indexes | `psql "$POSTGRES_URL" -f database/request/01_schema.sql` |
| `02_embeddings.sql` | `ai.embeddings_request` + queue `embedding_refresh_request` + trigger | `psql "$POSTGRES_URL" -f database/request/02_embeddings.sql` |
| `load_data.py` | `request.json` → `itsm.request` | `.venv/bin/python database/request/load_data.py` |
| `backfill.py` | embed all existing requests (one-shot) | `.venv/bin/python database/request/backfill.py` |
| `worker.py` | drains `embedding_refresh_request` → `ai.embeddings_request` | `python database/request/worker.py` |

Symptom chunk = title/description/category/catalog/CI; diagnosis chunk =
`comments` thread summarised.
