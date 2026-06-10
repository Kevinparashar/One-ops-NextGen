# agent/

Self-contained slice for `itsm.agent` + `ai.embeddings_agent` — the registry
record + its routing vectors. Data comes from registry FILES (not data/itsm),
so the data step is `sync.py`, not `load_data.py`. Requires `_foundation/` first.

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.agent` (agent_id, version, status, body, content_hash) | `psql "$POSTGRES_URL" -f database/agent/01_schema.sql` |
| `02_embeddings.sql` | `ai.embeddings_agent` (global, multi-chunk) + queue `embedding_refresh_agent` + trigger | `psql "$POSTGRES_URL" -f database/agent/02_embeddings.sql` |
| `sync.py` | `registries/v2/agents/*.json` → `itsm.agent` (hash-gated) | `.venv/bin/python database/agent/sync.py` |
| `worker.py` | drains `embedding_refresh_agent` → description/use_when/example chunks | `python database/agent/worker.py` |

Chunk builder: `src/oneops/embeddings/agent_input.py` (description + use_when +
example; **not_when excluded** — LLM-disambiguator only). The agent→tools link is
`body.tool_refs` (no junction). Editing a card → `sync.py` flips content_hash →
trigger enqueues → worker re-embeds only the changed agent.

> Note: `sync.py` populates the table → fires the trigger; there is no separate
> `backfill.py` (the worker handles both first-fill and live refresh).
