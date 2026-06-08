# uc_schema/

Self-contained slice for `itsm.uc_schema` — the request/response envelope schema
registry record. **No embeddings** (structural). Requires `_foundation/`.

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.uc_schema` (schema_id, version, status, body) | `psql "$POSTGRES_URL" -f database/uc_schema/01_schema.sql` |
| `sync.py` | `registries/v2/schemas/*.json` → `itsm.uc_schema` (hash-gated) | `.venv/bin/python database/uc_schema/sync.py` |

Named `uc_schema` (not `schema`) to avoid the SQL keyword.
