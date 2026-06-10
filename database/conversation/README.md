# conversation/

Self-contained slice for `conversation_events` — the append-only chat event log.
**No embeddings, no seed/sync** — the app writes one row per turn at runtime.
Lives in the `public` schema (not `itsm`).

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `conversation_events` (append-only) + read/retention indexes | `psql "$POSTGRES_URL" -f database/conversation/01_schema.sql` |

Append-only by contract: INSERT (append), SELECT (replay), retention DELETE
(prune). No UPDATE path — events are immutable once written.
