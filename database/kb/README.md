# kb/

Self-contained slice for `itsm.kb_knowledge` + `ai.embeddings_kb_knowledge`.
Requires `_foundation/` first (FK: `created_by → sys_user`).

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.kb_knowledge` (+ content_tsv) + content_hash_kb + indexes | `psql "$POSTGRES_URL" -f database/kb/01_schema.sql` |
| `02_embeddings.sql` | `ai.embeddings_kb_knowledge` + queue `embedding_refresh_kb_knowledge` + trigger | `psql "$POSTGRES_URL" -f database/kb/02_embeddings.sql` |
| `load_data.py` | `kb_knowledge.json` → `itsm.kb_knowledge` | `.venv/bin/python database/kb/load_data.py` |
| `backfill.py` | embed all articles (anchor + body chunks) | `.venv/bin/python database/kb/backfill.py` |
| `worker.py` | drains `embedding_refresh_kb_knowledge` → anchor + body chunks | `python database/kb/worker.py` |

One article → 1 anchor + N body chunks (adaptive chunking + overlap). The worker
prunes leftover body rows when an article shrinks.
