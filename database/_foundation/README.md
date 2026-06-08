# _foundation/

Shared infrastructure + reference data that **every service depends on**. Run
**first**, before any service slice.

The ITSM tables are an FK-linked graph (e.g. `incident → sys_user, cmdb_ci,
problem, change`). These five reference tables are owned by no single service,
so they live here.

## Apply order

| step | file | creates |
|------|------|---------|
| 1 | `01_extensions_schemas.sql` | extensions (`vector`, `pgmq`, `pgcrypto`) + schemas `itsm`, `ai` |
| 2 | `02_reference_tables.sql` | `sys_user`, `cmdb_ci`, `asset`, `problem`, `change` (+ their indexes) |
| 3 | `load_reference_data.py` | loads those 5 tables (FK order, one transaction) |

```
psql "$POSTGRES_URL" -f database/_foundation/01_extensions_schemas.sql
psql "$POSTGRES_URL" -f database/_foundation/02_reference_tables.sql
.venv/bin/python database/_foundation/load_reference_data.py
```

No embedding queues are created here — each service owns its own queue +
worker (see any service's `02_embeddings.sql`).
