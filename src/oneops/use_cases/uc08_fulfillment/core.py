"""UC-8 handler core — request → plan → persist → outcome.

This is Phase 1 of DOC-09 §UC-8 (Decomposition + persistence). Phase 2
(execution orchestration via LangGraph) lives in `graph.py`. Phase 3
(exception handling) is split between adapter responses + graph retry
policy. Phase 4 (completion) is the orchestrator's final aggregate.

Production-grade properties enforced here:

  • Boundary validation — input is a `FulfillmentRequest` (Pydantic);
    invalid shapes never reach the handler body.
  • Tenant isolation — `tenant_id` is the first SQL predicate at every
    DB layer call.
  • Idempotency — same `idempotency_key` on the same tenant returns the
    existing RITM rather than creating a duplicate.
  • Duplicate detection — DOC-09 §UC-8 8.7: existing OPEN RITM for
    (requested_for, catalog_item_id) blocks the new one with `DuplicateRequestError`.
  • Hybrid decomposition — when the catalog template is well-formed,
    use it directly (deterministic, no LLM cost). LLM-driven plan
    generation is the fallback for 8.8 (no-template) — scoped to Phase 6.
  • OTel span — one parent `uc08.core.fulfill_request` span carrying
    `ritm_id`, `request_id`, `tenant_id`, `trace_id`.
  • Structured failure modes — every error is typed (see `errors.py`).
"""
from __future__ import annotations

import structlog
from opentelemetry import trace

from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment.contracts import (
    CatalogTemplate,
    FulfillmentOutcome,
    FulfillmentPlan,
    FulfillmentRequest,
    Outcome,
    TaskPlanItem,
    TaskType,
)
from oneops.use_cases.uc08_fulfillment.db import ConnectionProvider
from oneops.use_cases.uc08_fulfillment.errors import (
    DuplicateRequestError,
)

_log = structlog.get_logger("oneops.uc08.core")
_tracer = trace.get_tracer("oneops.uc08.core")

# Duplicate detection window (DOC-09 §UC-8 8.7). Catalog items rarely
# need a second fulfillment within 24h.
_DUPLICATE_LOOKBACK_DAYS = 30


# ── Hybrid decomposition ────────────────────────────────────────────────────


def _substitute_input_template(
    input_template: dict | None, variables: dict,
) -> dict:
    """Substitute `{var_name}` placeholders in input_template values
    using request variables.

    Production-grade contract:
      • Only leaf string values are substituted; nested dicts/lists
        are walked recursively.
      • A missing variable leaves the placeholder string unchanged
        (the adapter's signature validation will catch unset required
        fields — fail-loud rather than silent default).
      • Non-string values pass through untouched.
    """
    if not input_template:
        return {}

    def _walk(v):
        if isinstance(v, str):
            # Format-string substitution only for `{var}`-shaped values.
            try:
                return v.format(**variables)
            except (KeyError, IndexError):
                return v
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v

    return {k: _walk(v) for k, v in input_template.items()}


def _materialise_plan_from_template(
    *, ritm_id_placeholder: str, template: CatalogTemplate,
    variables: dict,
) -> FulfillmentPlan:
    """Deterministic plan materialisation from a well-formed catalog template.

    Used when the catalog item has a valid `tasks` JSONB. Production-grade:
      • Variable substitution into per-task `input_payload`.
      • SLA inheritance (catalog item → task) when task lacks own SLA.
      • No LLM call — zero token cost, fully reproducible.

    LLM-driven decomposition (for scenario 8.8 — no template) is added in
    Phase 6 as a fallback. Most catalog items have templates, so this
    path is the hot path.
    """
    per_task_sla_default = max(
        1, template.estimated_total_minutes // max(1, len(template.tasks)),
    )
    items: list[TaskPlanItem] = []
    for t in template.tasks:
        # Per-task input_template wins; if absent, the task gets the raw
        # variables dict (back-compat for templates that have not yet
        # been migrated to per-task templates).
        if t.input_template is not None:
            task_input = _substitute_input_template(t.input_template, variables)
        else:
            task_input = dict(variables)
        items.append(TaskPlanItem(
            template_task_id=t.task_id,
            task_name=t.name,
            task_type=t.type,
            tool_id=t.tool_id,
            depends_on=list(t.depends_on),
            assignment_group=t.owner_group,
            sla_minutes=t.sla_minutes or per_task_sla_default,
            input_payload=task_input,
        ))
    return FulfillmentPlan(
        ritm_id=ritm_id_placeholder,
        catalog_item_id=template.catalog_item_id,
        tasks=tuple(items),
        estimated_total_minutes=template.estimated_total_minutes,
    )


# ── Entry point ─────────────────────────────────────────────────────────────


async def fulfill_request(
    req: FulfillmentRequest, *,
    connection_provider: ConnectionProvider | None = None,
    trace_id: str | None = None,
    actor: str | None = None,
) -> Outcome:
    """Phase 1 of UC-8: decompose + persist.

    Returns an Outcome with `outcome=IN_PROGRESS` and a fresh `run_id`.
    Phase 2 (execution) is invoked by the caller — typically the
    fastapi route or the chat tool dispatcher — once it sees this Outcome.

    Raises:
        DuplicateRequestError — 8.7: open RITM already exists.
        CatalogItemNotFoundError — caller named an unknown catalog item.
        RequestNotFoundError — parent SR doesn't exist for this tenant.
        InvalidTemplateError — catalog template is malformed at the DB.
    """
    cp = connection_provider or _db.default_connection_provider
    conn = await cp()
    try:
        with _tracer.start_as_current_span(
            "uc08.core.fulfill_request",
            attributes={
                "oneops.tenant_id": req.tenant_id,
                "oneops.request_id": req.request_id,
                "uc08.catalog_item_id": req.catalog_item_id,
                "uc08.trigger_type": req.trigger_type.value,
                "uc08.has_idempotency_key": req.idempotency_key is not None,
            },
        ) as span:
            # 1. SR exists?
            await _db.assert_request_exists(
                tenant_id=req.tenant_id, request_id=req.request_id, conn=conn,
            )

            # 2. Duplicate gate (DOC-09 §UC-8 8.7)
            existing = await _db.find_open_duplicate(
                tenant_id=req.tenant_id,
                requested_for=req.requested_for,
                catalog_item_id=req.catalog_item_id,
                lookback_days=_DUPLICATE_LOOKBACK_DAYS,
                conn=conn,
            )
            if existing:
                span.set_attribute("uc08.duplicate_blocked_by", existing)
                raise DuplicateRequestError(
                    f"open RITM {existing} already exists for "
                    f"{req.requested_for} + {req.catalog_item_id} "
                    f"within {_DUPLICATE_LOOKBACK_DAYS} days",
                )

            # 3. Load template + materialise plan
            template = await _db.load_catalog_template(
                tenant_id=req.tenant_id,
                catalog_item_id=req.catalog_item_id,
                conn=conn,
            )
            # Placeholder ritm_id — we don't have the real one until the
            # INSERT lands. The plan validator only checks DAG structure;
            # we replace the placeholder before persistence.
            plan = _materialise_plan_from_template(
                ritm_id_placeholder="RITM_PENDING",
                template=template,
                variables=req.variables,
            )

            # 4. Insert RITM (idempotency-aware)
            ritm_id = await _db.insert_request_item(
                tenant_id=req.tenant_id,
                request_id=req.request_id,
                catalog_item_id=req.catalog_item_id,
                variables=req.variables,
                requested_for=req.requested_for,
                opened_by=req.opened_by,
                plan=plan.model_copy(update={"ritm_id": "RITM_PENDING"}),
                total_tasks=len(plan.tasks),
                assignment_group=template.owner_group,
                idempotency_key=req.idempotency_key,
                conn=conn,
            )
            span.set_attribute("uc08.ritm_id", ritm_id)

            # 5. Insert tasks
            n_tasks = await _db.insert_tasks(
                tenant_id=req.tenant_id, ritm_id=ritm_id,
                request_id=req.request_id,
                plan=plan.model_copy(update={"ritm_id": ritm_id}),
                conn=conn,
            )
            span.set_attribute("uc08.tasks_inserted", n_tasks)

            # 6. Open the fulfillment_run audit row
            thread_id = f"thread-{ritm_id}"
            run_id = await _db.insert_fulfillment_run(
                tenant_id=req.tenant_id, ritm_id=ritm_id,
                trigger_type=req.trigger_type.value,
                triggered_by=actor or req.opened_by,
                trace_id=trace_id,
                thread_id=thread_id,
                conn=conn,
            )
            span.set_attribute("uc08.run_id", run_id)

            _log.info("uc08.core.fulfill_request.persisted",
                      ritm_id=ritm_id, run_id=run_id,
                      tasks=n_tasks,
                      catalog_item_id=req.catalog_item_id,
                      tenant_id=req.tenant_id)

            display_text = (
                f"Fulfillment plan created for {template.name} — "
                f"{n_tasks} tasks queued. Status: {ritm_id}."
            )

            return Outcome(
                tenant_id=req.tenant_id,
                request_id=req.request_id,
                ritm_id=ritm_id,
                catalog_item_id=req.catalog_item_id,
                run_id=run_id,
                outcome=FulfillmentOutcome.IN_PROGRESS,
                tasks_total=n_tasks,
                tasks_completed=0,
                tasks_failed=0,
                tasks_skipped=0,
                tasks_in_progress=0,
                trace_id=trace_id,
                display_text=display_text,
            )
    finally:
        await conn.close()


__all__ = ["fulfill_request"]
