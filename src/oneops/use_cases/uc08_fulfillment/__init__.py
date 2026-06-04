"""UC-8 — Catalog Item Fulfillment with AI Workflow (DOC-09 §UC-8).

A generic fulfillment engine: takes a request against ANY catalog item, reads
its template from `itsm.catalog_item`, decomposes the request into a task DAG,
and orchestrates execution through UC-8's OWN executor (executor.py) — a wave
runner over the task DAG. Approval gates are DB-state: a `request_human_approval`
task persists an itsm.approval row and transitions the task to `blocked`. (This
package does NOT use the LangGraph executor graph or `interrupt()`; the broader
chat/agent pipeline does, but UC-8 fulfillment runs its own engine.)

Substrate:
  • `itsm.catalog_item` (existing, 30 rows)  — the catalog with task templates
  • `itsm.request` (existing, 130 rows)      — Service Requests (SR)
  • `itsm.request_item` (new, migration 0007) — RITM line items
  • `itsm.task` (new, migration 0007)         — atomic fulfillment work
  • `itsm.approval` (new, migration 0007)     — approval gates
  • `itsm.fulfillment_run` (new, migration 0007) — per-invocation audit

Entry-point parity guarantee:
  • POST /api/uc08/fulfill (portal)  → same handler
  • chat ("onboard John Smith")       → same handler
Both publish the same NATS subject `oneops.agent.uc08_fulfillment`.

Module layout:
  contracts.py  — Pydantic models (frozen by default) + enums
  adapters/     — IntegrationAdapter Protocol + mock + (later) real bindings
  handlers.py   — entry point + tool wrappers
  core.py       — handler logic (decomposition + persistence)
  executor.py   — fulfillment wave runner (task DAG; DB-state approval gates)
  db.py         — itsm.* persistence (catalog/request/task/approval/run)
  fixtures/     — demo templates (read-only data, not the product)
  tools.py      — registry-resolved tool handlers

This package never imports from the API layer — the dependency arrow runs
from `oneops.api.*` down to here, not the other way around.
"""
