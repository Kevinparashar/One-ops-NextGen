# tool/

Self-contained slice for `itsm.tool`. **No embeddings** — tools are selected
deterministically (explicit tool_id + parameter shape), never by vector, so there
is no `02_embeddings.sql`, no queue, and no worker. Requires `_foundation/`.

| file | what it does | run |
|------|--------------|-----|
| `01_schema.sql` | `itsm.tool` (tool_id, version, agent_id, status, body) | `psql "$POSTGRES_URL" -f database/tool/01_schema.sql` |
| `sync.py` | `registries/v2/tools/<agent_id>/*.json` → `itsm.tool` (hash-gated) | `.venv/bin/python database/tool/sync.py` |

`agent_id` = the owning agent (the `tools/<agent_id>/` folder). The agent→tools
direction is `itsm.agent.body.tool_refs`; this is the reverse pointer. Soft
reference (no DB FK — versioned, card-driven; validated at boot by check_integrity).
