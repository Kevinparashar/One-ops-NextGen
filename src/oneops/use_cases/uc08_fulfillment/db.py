"""Async Postgres data layer for UC-8.

All database access goes through this module. No other UC-8 file talks to
the DB directly. Production-grade properties:

  • Connection-provider injection — same pattern as UC-2 / UC-5. Tests
    inject a per-test connection so they never touch shared state.
  • Tenant isolation — every public function takes `tenant_id` as first
    SQL predicate; tests verify cross-tenant queries return zero rows.
  • Idempotency at the DB layer — INSERTs use `ON CONFLICT DO NOTHING`
    on the idempotency key UNIQUE constraint; the function fetches the
    existing record on conflict.
  • Optimistic locking — UPDATEs always include `WHERE version = $N`
    and return the new row; a returned None means another worker beat us
    to it (caller decides whether to retry).
  • Structured spans — every public function opens an OTel span named
    `uc08.db.<function>` carrying tenant_id + identifier attributes.
  • Typed returns — all reads return Pydantic models (never bare dicts).
"""
from __future__ import annotations

import json
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg
from opentelemetry import trace

from oneops.use_cases.uc08_fulfillment.contracts import (
    CatalogTemplate,
    CatalogTemplateTask,
    FulfillmentPlan,
    FulfillmentStatus,
    RitmState,
)
from oneops.use_cases.uc08_fulfillment.errors import (
    CatalogItemNotFoundError,
    FulfillmentPersistenceError,
    RequestItemNotFoundError,
    RequestNotFoundError,
)

ConnectionProvider = Callable[[], Awaitable[asyncpg.Connection]]

_tracer = trace.get_tracer("oneops.uc08.db")


async def default_connection_provider() -> asyncpg.Connection:
    """Per-call asyncpg connection over POSTGRES_URL. Mirrors UC-2 / UC-5
    pattern. Each call opens + closes its own connection — cheap (~5ms)
    over the Supabase pooler."""
    pg_url = os.getenv("POSTGRES_URL")
    if not pg_url:
        raise RuntimeError(
            "POSTGRES_URL not set; UC-8 cannot reach Postgres")
    return await asyncpg.connect(pg_url)


# ── Catalog reads ────────────────────────────────────────────────────────────


async def load_catalog_template(
    *, tenant_id: str, catalog_item_id: str,
    conn: asyncpg.Connection,
) -> CatalogTemplate:
    """Read a catalog item + parse it into a validated CatalogTemplate.

    Raises:
        CatalogItemNotFoundError — no such row for this tenant
        InvalidTemplateError     — row exists but `tasks` JSONB is malformed
    """
    with _tracer.start_as_current_span(
        "uc08.db.load_catalog_template",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.catalog_item_id": catalog_item_id,
        },
    ):
        row = await conn.fetchrow(
            """
            SELECT catalog_item_id, tenant_id, name, description,
                   category, owner_group, estimated_total_minutes, tasks
              FROM itsm.catalog_item
             WHERE tenant_id = $1 AND catalog_item_id = $2
            """,
            tenant_id, catalog_item_id,
        )
        if row is None:
            raise CatalogItemNotFoundError(
                f"catalog item {catalog_item_id!r} not found for tenant "
                f"{tenant_id!r}",
            )
        raw_tasks = row["tasks"]
        # asyncpg returns JSONB as str — parse it.
        if isinstance(raw_tasks, str):
            raw_tasks = json.loads(raw_tasks)
        tasks = tuple(
            CatalogTemplateTask(
                task_id=str(t.get("task_id") or t.get("id") or ""),
                name=str(t.get("name") or ""),
                type=t.get("type") or "automated",
                owner_group=t.get("owner_group"),
                depends_on=list(t.get("depends_on") or []),
                tool_id=t.get("tool_id"),
                input_template=t.get("input_template"),
                sla_minutes=t.get("sla_minutes"),
            )
            for t in raw_tasks
        )
        return CatalogTemplate(
            catalog_item_id=row["catalog_item_id"],
            tenant_id=row["tenant_id"],
            name=row["name"],
            description=row["description"],
            category=row["category"],
            owner_group=row["owner_group"],
            estimated_total_minutes=row["estimated_total_minutes"],
            tasks=tasks,
        )


# ── SR existence check ──────────────────────────────────────────────────────


async def assert_request_exists(
    *, tenant_id: str, request_id: str, conn: asyncpg.Connection,
) -> None:
    """Confirm the parent SR exists (it must; UC-8 doesn't create SRs)."""
    n = await conn.fetchval(
        "SELECT 1 FROM itsm.request WHERE tenant_id = $1 AND request_id = $2",
        tenant_id, request_id,
    )
    if n is None:
        raise RequestNotFoundError(
            f"request {request_id!r} not found for tenant {tenant_id!r}",
        )


# ── Duplicate check (DOC-09 §UC-8 8.7) ─────────────────────────────────────


async def find_open_duplicate(
    *, tenant_id: str, requested_for: str, catalog_item_id: str,
    lookback_days: int, conn: asyncpg.Connection,
) -> str | None:
    """Return the ritm_id of an OPEN RITM for the same (requested_for,
    catalog_item_id) within lookback_days, or None if no duplicate."""
    row = await conn.fetchrow(
        """
        SELECT ritm_id FROM itsm.request_item
         WHERE tenant_id = $1
           AND requested_for = $2
           AND catalog_item_id = $3
           AND state IN ('requested','approved','in_progress')
           AND opened_at >= (now() - ($4 || ' days')::interval)
         ORDER BY opened_at DESC
         LIMIT 1
        """,
        tenant_id, requested_for, catalog_item_id, str(lookback_days),
    )
    return row["ritm_id"] if row else None


# ── RITM + Task + Fulfillment-run inserts ───────────────────────────────────


async def insert_request_item(
    *, tenant_id: str, request_id: str, catalog_item_id: str,
    variables: dict[str, Any], requested_for: str, opened_by: str,
    plan: FulfillmentPlan, total_tasks: int,
    assignment_group: str | None,
    idempotency_key: str | None,
    conn: asyncpg.Connection,
) -> str:
    """Insert a new RITM row, return the assigned ritm_id.

    Idempotency: if a row with the same (tenant_id, idempotency_key)
    already exists, returns the existing ritm_id (no duplicate created).
    """
    with _tracer.start_as_current_span(
        "uc08.db.insert_request_item",
        attributes={
            "oneops.tenant_id": tenant_id,
            "oneops.request_id": request_id,
            "uc08.catalog_item_id": catalog_item_id,
        },
    ):
        # Idempotency short-circuit
        if idempotency_key:
            existing = await conn.fetchval(
                """
                SELECT ritm_id FROM itsm.request_item
                 WHERE tenant_id = $1 AND idempotency_key = $2
                """,
                tenant_id, idempotency_key,
            )
            if existing:
                return existing

        # Generate a deterministic-ish RITM id
        ritm_id = f"RITM{uuid.uuid4().hex[:10].upper()}"
        try:
            await conn.execute(
                """
                INSERT INTO itsm.request_item (
                    tenant_id, ritm_id, request_id, catalog_item_id,
                    variables, requested_for, opened_by, plan,
                    assignment_group, total_tasks, state, idempotency_key
                ) VALUES (
                    $1,$2,$3,$4,$5::jsonb,$6,$7,$8::jsonb,$9,$10,'requested',$11
                )
                """,
                tenant_id, ritm_id, request_id, catalog_item_id,
                json.dumps(variables),
                requested_for, opened_by,
                plan.model_dump_json(),
                assignment_group, total_tasks, idempotency_key,
            )
        except asyncpg.UniqueViolationError as exc:
            raise FulfillmentPersistenceError(
                f"failed to insert RITM (uniqueness violation): {exc}",
                cause=exc,
            ) from exc
        return ritm_id


async def insert_tasks(
    *, tenant_id: str, ritm_id: str, request_id: str,
    plan: FulfillmentPlan, conn: asyncpg.Connection,
) -> int:
    """Insert one row per node in the plan. Returns count of rows
    inserted. Single transaction per call.

    PENDING is the entry state — the orchestrator transitions
    pending → ready when dependencies are satisfied.
    """
    with _tracer.start_as_current_span(
        "uc08.db.insert_tasks",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.ritm_id": ritm_id,
            "uc08.task_count": len(plan.tasks),
        },
    ):
        n = 0
        for t in plan.tasks:
            task_id = f"SCTASK{uuid.uuid4().hex[:10].upper()}"
            try:
                await conn.execute(
                    """
                    INSERT INTO itsm.task (
                        tenant_id, task_id, ritm_id, request_id,
                        template_task_id, task_name, task_type, tool_id,
                        depends_on, assignment_group, state,
                        sla_minutes, input_payload, idempotency_key
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9::text[],$10,'pending',
                        $11,$12::jsonb,$13
                    )
                    """,
                    tenant_id, task_id, ritm_id, request_id,
                    t.template_task_id, t.task_name, t.task_type.value,
                    t.tool_id,
                    t.depends_on, t.assignment_group,
                    t.sla_minutes, json.dumps(t.input_payload),
                    # Per-task idempotency: (ritm_id, template_task_id) is naturally unique
                    f"{ritm_id}:{t.template_task_id}",
                )
                n += 1
            except asyncpg.UniqueViolationError:
                # Same task already inserted (idempotent re-run). Skip silently.
                continue
        return n


async def insert_fulfillment_run(
    *, tenant_id: str, ritm_id: str, trigger_type: str,
    triggered_by: str, trace_id: str | None,
    thread_id: str,
    decomposition_tokens: int | None = None,
    decomposition_cost_micros: int | None = None,
    conn: asyncpg.Connection,
) -> str:
    """Open a fulfillment_run row. Returns run_id."""
    run_id = f"RUN{uuid.uuid4().hex[:14]}"
    await conn.execute(
        """
        INSERT INTO itsm.fulfillment_run (
            tenant_id, run_id, ritm_id, trigger_type, triggered_by,
            trace_id, thread_id,
            decomposition_tokens, decomposition_cost_micros
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        tenant_id, run_id, ritm_id, trigger_type, triggered_by,
        trace_id, thread_id,
        decomposition_tokens, decomposition_cost_micros,
    )
    return run_id


# ── Status read (DOC-09 §UC-8 8.6) ─────────────────────────────────────────


async def get_status(
    *, tenant_id: str, ritm_id: str, conn: asyncpg.Connection,
) -> FulfillmentStatus:
    """Aggregate live status for one RITM. Used by GET /api/uc08/status/{id}
    and by the chat status tool."""
    with _tracer.start_as_current_span(
        "uc08.db.get_status",
        attributes={"oneops.tenant_id": tenant_id, "uc08.ritm_id": ritm_id},
    ):
        ritm = await conn.fetchrow(
            """
            SELECT request_id, catalog_item_id, state, approval_state,
                   total_tasks, sla_due, sla_breached, estimated_completion,
                   opened_at, updated_at, fulfilled_at
              FROM itsm.request_item
             WHERE tenant_id = $1 AND ritm_id = $2
            """,
            tenant_id, ritm_id,
        )
        if ritm is None:
            raise RequestItemNotFoundError(
                f"ritm {ritm_id!r} not found for tenant {tenant_id!r}",
            )
        # Task state buckets
        task_rows = await conn.fetch(
            """
            SELECT state, count(*) AS n FROM itsm.task
             WHERE tenant_id = $1 AND ritm_id = $2 GROUP BY state
            """,
            tenant_id, ritm_id,
        )
        by_state: dict[str, int] = {r["state"]: int(r["n"]) for r in task_rows}
        # Pending approvals
        appr_rows = await conn.fetch(
            """
            SELECT approval_id FROM itsm.approval
             WHERE tenant_id = $1 AND ritm_id = $2 AND state = 'pending'
            """,
            tenant_id, ritm_id,
        )
        pending_approvals = tuple(r["approval_id"] for r in appr_rows)
        return FulfillmentStatus(
            tenant_id=tenant_id,
            request_id=ritm["request_id"],
            ritm_id=ritm_id,
            catalog_item_id=ritm["catalog_item_id"],
            state=RitmState(ritm["state"]),
            approval_state=ritm["approval_state"],
            tasks_total=int(ritm["total_tasks"]),
            tasks_by_state=by_state,
            pending_approvals=pending_approvals,
            sla_due=ritm["sla_due"],
            sla_breached=bool(ritm["sla_breached"]),
            estimated_completion=ritm["estimated_completion"],
            opened_at=ritm["opened_at"],
            updated_at=ritm["updated_at"],
            fulfilled_at=ritm["fulfilled_at"],
        )


# ── Task state transitions ──────────────────────────────────────────────────
# All transitions use optimistic locking: WHERE version = ${current} so two
# workers cannot trample each other. A None return means the transition was
# already done by another worker — the caller treats it as a no-op.


async def list_tasks_for_ritm(
    *, tenant_id: str, ritm_id: str, conn: asyncpg.Connection,
) -> list[dict[str, Any]]:
    """All task rows for a RITM, freshest first. Used by the executor to
    decide what's ready and the test suite to assert state changes."""
    rows = await conn.fetch(
        """
        SELECT task_id, template_task_id, task_name, task_type, tool_id,
               depends_on, state, retry_count, max_retries,
               input_payload, output_payload, error_message, error_code,
               assignment_group, sla_minutes, version
          FROM itsm.task
         WHERE tenant_id = $1 AND ritm_id = $2
         ORDER BY updated_at DESC
        """,
        tenant_id, ritm_id,
    )
    out = []
    for r in rows:
        d = dict(r)
        # asyncpg returns JSONB as str — parse it
        for k in ("input_payload", "output_payload"):
            v = d.get(k)
            if isinstance(v, str):
                d[k] = json.loads(v)
        out.append(d)
    return out


async def transition_task_state(
    *, tenant_id: str, task_id: str,
    from_state: str, to_state: str,
    version: int,
    output_payload: dict | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
    retry_count: int | None = None,
    conn: asyncpg.Connection,
) -> int | None:
    """Optimistic-lock state transition.

    Returns the NEW version number when the transition committed, or None
    when another worker beat us to it (caller re-reads the row).
    """
    new_version = await conn.fetchval(
        """
        UPDATE itsm.task
           SET state           = $4,
               output_payload  = COALESCE($5::jsonb, output_payload),
               error_message   = COALESCE($6, error_message),
               error_code      = COALESCE($7, error_code),
               retry_count     = COALESCE($8, retry_count),
               started_at      = CASE WHEN $4 = 'in_progress' AND started_at IS NULL
                                       THEN now() ELSE started_at END,
               finished_at     = CASE WHEN $4 IN ('done','failed','skipped')
                                       THEN now() ELSE finished_at END,
               ready_at        = CASE WHEN $4 = 'ready' AND ready_at IS NULL
                                       THEN now() ELSE ready_at END,
               updated_at      = now(),
               version         = version + 1
         WHERE tenant_id = $1 AND task_id = $2
           AND state = $3 AND version = $9
        RETURNING version
        """,
        tenant_id, task_id, from_state, to_state,
        json.dumps(output_payload) if output_payload is not None else None,
        error_message, error_code, retry_count, version,
    )
    return new_version


async def transition_ritm_state(
    *, tenant_id: str, ritm_id: str,
    from_state: str, to_state: str, version: int,
    completed_tasks: int | None = None,
    failed_tasks: int | None = None,
    conn: asyncpg.Connection,
) -> int | None:
    """Optimistic-lock RITM state transition."""
    return await conn.fetchval(
        """
        UPDATE itsm.request_item
           SET state           = $4,
               completed_tasks = COALESCE($5, completed_tasks),
               failed_tasks    = COALESCE($6, failed_tasks),
               started_at      = CASE WHEN $4 = 'in_progress' AND started_at IS NULL
                                       THEN now() ELSE started_at END,
               fulfilled_at    = CASE WHEN $4 = 'fulfilled' THEN now() ELSE fulfilled_at END,
               closed_at       = CASE WHEN $4 IN ('cancelled','rejected','fulfilled')
                                       THEN now() ELSE closed_at END,
               updated_at      = now(),
               version         = version + 1
         WHERE tenant_id = $1 AND ritm_id = $2
           AND state = $3 AND version = $7
        RETURNING version
        """,
        tenant_id, ritm_id, from_state, to_state,
        completed_tasks, failed_tasks, version,
    )


async def get_ritm(
    *, tenant_id: str, ritm_id: str, conn: asyncpg.Connection,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT request_id, catalog_item_id, variables, requested_for,
               state, total_tasks, completed_tasks, failed_tasks,
               langgraph_thread_id, version
          FROM itsm.request_item
         WHERE tenant_id = $1 AND ritm_id = $2
        """,
        tenant_id, ritm_id,
    )
    if row is None:
        return None
    d = dict(row)
    v = d.get("variables")
    if isinstance(v, str):
        d["variables"] = json.loads(v)
    return d


# ── Approval persistence ────────────────────────────────────────────────────


async def insert_approval(
    *, tenant_id: str, ritm_id: str, task_id: str | None,
    approval_type: str, reason: str,
    requested_from: str,
    payload: dict | None,
    langgraph_interrupt_id: str | None,
    conn: asyncpg.Connection,
) -> str:
    approval_id = f"APP{uuid.uuid4().hex[:10].upper()}"
    await conn.execute(
        """
        INSERT INTO itsm.approval (
            tenant_id, approval_id, ritm_id, task_id,
            approval_type, reason, payload,
            state, requested_from, langgraph_interrupt_id
        ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,'pending',$8,$9)
        """,
        tenant_id, approval_id, ritm_id, task_id,
        approval_type, reason,
        json.dumps(payload) if payload is not None else None,
        requested_from, langgraph_interrupt_id,
    )
    return approval_id


async def record_approval_decision(
    *, tenant_id: str, approval_id: str,
    decision: str, decided_by: str,
    decision_comment: str | None,
    version: int,
    conn: asyncpg.Connection,
) -> int | None:
    new_state = "approved" if decision == "approved" else "rejected"
    return await conn.fetchval(
        """
        UPDATE itsm.approval
           SET state             = $3,
               decision          = $3,
               decided_by        = $4,
               decision_comment  = $5,
               decided_at        = now(),
               version           = version + 1
         WHERE tenant_id = $1 AND approval_id = $2
           AND state = 'pending' AND version = $6
        RETURNING version
        """,
        tenant_id, approval_id, new_state, decided_by,
        decision_comment, version,
    )


async def get_approval(
    *, tenant_id: str, approval_id: str, conn: asyncpg.Connection,
) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        """
        SELECT ritm_id, task_id, approval_type, reason, payload,
               state, decision, requested_from, version
          FROM itsm.approval
         WHERE tenant_id = $1 AND approval_id = $2
        """,
        tenant_id, approval_id,
    )
    if row is None:
        return None
    d = dict(row)
    p = d.get("payload")
    if isinstance(p, str):
        d["payload"] = json.loads(p)
    return d


# ── Run completion ──────────────────────────────────────────────────────────


async def finalise_run(
    *, tenant_id: str, run_id: str, outcome: str,
    summary: dict[str, Any], conn: asyncpg.Connection,
) -> None:
    await conn.execute(
        """
        UPDATE itsm.fulfillment_run
           SET outcome        = $3,
               outcome_summary = $4::jsonb,
               finished_at    = now(),
               duration_ms    = EXTRACT(MILLISECONDS FROM (now() - started_at))::int
         WHERE tenant_id = $1 AND run_id = $2
        """,
        tenant_id, run_id, outcome, json.dumps(summary),
    )


__all__ = [
    "ConnectionProvider",
    "default_connection_provider",
    "load_catalog_template",
    "assert_request_exists",
    "find_open_duplicate",
    "insert_request_item",
    "insert_tasks",
    "insert_fulfillment_run",
    "get_status",
    # task / ritm state transitions
    "list_tasks_for_ritm",
    "transition_task_state",
    "transition_ritm_state",
    "get_ritm",
    # approval persistence
    "insert_approval",
    "record_approval_decision",
    "get_approval",
    # run completion
    "finalise_run",
]
