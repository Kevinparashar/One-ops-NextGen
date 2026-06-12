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
| `04_approval_policy.sql` | `approval_policy` matrix + `approval.stage_index` (uc08 approval; inert until `UC08_APPROVAL_ENABLED`) | after 03 |
| `05_group_role_map.sql` | `group_role_map` (owner_group -> role/department bridge) | after 04 |
| `load_approval_policy.py` | `approval_policy.json` -> itsm.approval_policy (per tenant) | after 04 |
| `load_group_role_map.py` | `group_role_map.json` -> itsm.group_role_map | after 05 |
| `load_data.py` | `catalog_item.json` → itsm | after 01 |
| `derive_intent_keywords.py` | LLM-derives `intent_keywords` (discriminative search phrasings) per item → itsm.catalog_item | **after load, before backfill** |
| `backfill.py` | embed all catalog items (field-map driven) | after 02 + load + derive |
| `worker.py` | drains `embedding_refresh_catalog_item` → `ai.embeddings_catalog_item` | `python database/catalog_fulfillment/worker.py` |

Embeddable fields are declared in `ai.embedding_field_map`, so adding/renaming a
field is a data change (a field_map row), not a worker redeploy.

`intent_keywords` (2026-06-12) is the 5th embedded field — DERIVED data (LLM, not
authored, not in `catalog_item.json`), the catalog analogue of an embedding. It
sharpens the anchor so a query matches the item a user actually means
("forgot my password" → Password Reset, "second screen" → External Monitor)
instead of padding the shortlist with weak category-neighbours. Build order:
`load_data.py` → `derive_intent_keywords.py` → `backfill.py`. Re-run
`derive_intent_keywords.py --force` after editing item names/descriptions; it is
idempotent (skips items that already have keywords) and the change flips
`content_hash_catalog`, which re-embeds via the worker.
