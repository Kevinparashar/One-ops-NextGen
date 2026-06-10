# database/

All database operations, organised as **per-service vertical slices**. Each
service owns everything for its table(s) in one folder — schema, embeddings, data
load, backfill, and (if it has embeddings) its own worker — so a service can be
created, changed, loaded, and re-embedded **on its own without disturbing any
other**.

## Layout

```
database/
├── _foundation/          shared infra + reference tables (run FIRST)
├── _lib/                 shared-code package — _loader.py (load mechanics)
│                         + _worker_base.py (worker poll/ack loop + UPSERT + CLI)
├── _utils/               whole-DB utilities (generate / seed / restore / demo)
│
├── incident/             ┐
├── request/              │  entity slices: 01_schema · 02_embeddings ·
├── kb/                   │  load_data.py · backfill.py · worker.py · README
├── catalog_fulfillment/  ┘  (uc08: also 03_fulfillment.sql)
│
├── agent/                registry + embeddings: 01_schema · 02_embeddings · sync.py · worker.py
├── tool/                 registry, NO embeddings: 01_schema · sync.py
├── uc_schema/            registry, NO embeddings: 01_schema · sync.py
└── conversation/         plain table: 01_schema (runtime-populated)
```

## The per-service contract

| service kind | 01_schema | 02_embeddings | data step | backfill | worker |
|--------------|:--:|:--:|:--:|:--:|:--:|
| entity (incident/request/kb/catalog) | ✅ | ✅ | `load_data.py` | ✅ | `worker.py` |
| registry + embeddings (agent) | ✅ | ✅ | `sync.py` | (worker) | `worker.py` |
| registry, no embeddings (tool/uc_schema) | ✅ | — | `sync.py` | — | — |
| plain table (conversation) | ✅ | — | — | — | — |

Each service has its **own pgmq queue** `embedding_refresh_<service>` and its **own
worker process** (`python database/<service>/worker.py`) — no shared lane, no
head-of-line blocking. Workers are NOT started by the API; run one per service.

## Apply order (FK-driven)

```bash
# 1. foundation — extensions, schemas, reference tables + data
psql "$POSTGRES_URL" -f database/_foundation/01_extensions_schemas.sql
psql "$POSTGRES_URL" -f database/_foundation/02_reference_tables.sql
.venv/bin/python database/_foundation/load_reference_data.py

# 2. catalog table BEFORE request (request FK -> catalog_item)
psql "$POSTGRES_URL" -f database/catalog_fulfillment/01_schema.sql
psql "$POSTGRES_URL" -f database/catalog_fulfillment/02_embeddings.sql

# 3. incident + kb (any order after foundation)
psql "$POSTGRES_URL" -f database/incident/01_schema.sql
psql "$POSTGRES_URL" -f database/incident/02_embeddings.sql
psql "$POSTGRES_URL" -f database/kb/01_schema.sql
psql "$POSTGRES_URL" -f database/kb/02_embeddings.sql

# 4. request (needs catalog_item)
psql "$POSTGRES_URL" -f database/request/01_schema.sql
psql "$POSTGRES_URL" -f database/request/02_embeddings.sql

# 5. fulfillment workflow tables AFTER request (request_item FK -> request)
psql "$POSTGRES_URL" -f database/catalog_fulfillment/03_fulfillment.sql
psql "$POSTGRES_URL" -f database/catalog_fulfillment/04_approval_policy.sql

# 6. registry + conversation (independent of itsm entity FKs)
psql "$POSTGRES_URL" -f database/agent/01_schema.sql
psql "$POSTGRES_URL" -f database/agent/02_embeddings.sql
psql "$POSTGRES_URL" -f database/tool/01_schema.sql
psql "$POSTGRES_URL" -f database/uc_schema/01_schema.sql
psql "$POSTGRES_URL" -f database/conversation/01_schema.sql

# 7. data
.venv/bin/python database/catalog_fulfillment/load_data.py
.venv/bin/python database/incident/load_data.py
.venv/bin/python database/request/load_data.py
.venv/bin/python database/kb/load_data.py
.venv/bin/python database/agent/sync.py
.venv/bin/python database/tool/sync.py
.venv/bin/python database/uc_schema/sync.py

# 8. backfill embeddings for existing rows (workers handle live changes)
.venv/bin/python database/incident/backfill.py
.venv/bin/python database/request/backfill.py
.venv/bin/python database/kb/backfill.py
.venv/bin/python database/catalog_fulfillment/backfill.py
# agent: sync.py already enqueued; run the worker once to drain

# 9. live workers — one process per embedding-bearing service
python database/incident/worker.py
python database/request/worker.py
python database/kb/worker.py
python database/catalog_fulfillment/worker.py
python database/agent/worker.py
```

Two FK-forced ordering rules: **`_foundation/` first**, and
**`catalog_fulfillment/01` before `request`** / **`request` before
`catalog_fulfillment/03`**. Everything else is independent.

## Adding a new service

Copy an entity slice (e.g. `incident/`): `01_schema.sql`, `02_embeddings.sql`
(own queue + trigger), `load_data.py`, `backfill.py`, `worker.py`, `README.md`,
plus a `src/oneops/embeddings/<service>_input.py` builder. Add it to the order
above. No other service changes.

## Cross-cutting utilities

`_utils/` holds whole-DB helpers that aren't service-specific
(`generate_itsm_data.py`, `seed_supabase.py`, `gen_restore_sql.py`, demo seeders).
Named `_utils/` (not `_tools/`) to avoid confusion with the `tool/` service slice.
