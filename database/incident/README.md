# incident/

Self-contained slice for `itsm.incident` + `ai.embeddings_incident`. Runnable on
its own; touches no other service. Requires `_foundation/` first (FK refs:
sys_user, cmdb_ci, problem, change).

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.incident` table + search_tsv + content_hash doorbells + indexes | `psql "$POSTGRES_URL" -f database/incident/01_schema.sql` |
| `02_embeddings.sql` | `ai.embeddings_incident` + queue `embedding_refresh_incident` + trigger | `psql "$POSTGRES_URL" -f database/incident/02_embeddings.sql` |
| `load_data.py` | `incident.json` → `itsm.incident` (idempotent) | `.venv/bin/python database/incident/load_data.py` |
| `backfill.py` | embed all existing incidents (one-shot, hash-gated) | `.venv/bin/python database/incident/backfill.py` |
| `worker.py` | live worker: drains `embedding_refresh_incident` → `ai.embeddings_incident` | `python database/incident/worker.py` |

**Change a column** → edit `01_schema.sql`, re-run it. **Change embedding shape**
→ edit `02_embeddings.sql` (+ `worker.py`/`backfill.py` builders). **Re-embed** →
`backfill.py`. The worker keeps it fresh on every ticket change (own queue → no
head-of-line blocking from other services).
