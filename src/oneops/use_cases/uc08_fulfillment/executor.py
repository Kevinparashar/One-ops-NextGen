"""UC-8 orchestration runtime (Phase 6).

Drives a persisted FulfillmentPlan to terminal state. State lives in
Postgres (`itsm.request_item`, `itsm.task`, `itsm.approval`,
`itsm.fulfillment_run`) — not in process memory. Crash-safe: any worker
can pick up an in-flight RITM and continue.

Design choices (production-grade):

  • Postgres = source of truth. Every state transition is an
    optimistic-lock UPDATE (`WHERE version = ${current}`). Two workers
    cannot trample each other; the loser re-reads and retries.
  • Wave-based parallel dispatch. Each iteration: find ready tasks,
    dispatch in parallel via asyncio.gather. No per-task threads.
  • Adapter Protocol = the seam. The executor never knows whether
    integration calls a mock or a real Okta/AD. tool_id == adapter
    method name (canonical 1:1 naming, no translation table).
  • Retry policy. AdapterErrorClass.TRANSIENT → increment retry_count
    and transition back to 'ready' (caps at task.max_retries).
    PERMANENT → 'failed'; downstream deps are cascade-skipped.
  • Cascade-skip on failure. When a task is failed, its transitive
    dependents are marked 'skipped' so the wave loop can terminate.
  • Adapter call timeout. Hard cap via asyncio.wait_for; timeout is
    treated as in-band TRANSIENT for retry-budget reapplication.
  • Stuck-task recovery. Orphaned 'in_progress' tasks (worker crash)
    are reset to 'ready' at execute_plan entry.
  • Approval gates. tool_id='request_human_approval' persists an
    itsm.approval row and transitions task to 'blocked'.
  • Saga compensation. compensate_ritm() walks completed tasks in
    reverse and invokes the adapter's compensation method declared
    on the tool record. fulfilled→cancelled is a valid transition
    (matches AWS Step Functions / Temporal saga semantics).

State vocabulary aligns with the DB CHECK constraints — single source
of truth.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from opentelemetry import trace

from oneops.observability.metrics import increment as _metric_inc
from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    AdapterErrorClass,
    AdapterResponse,
    IntegrationAdapter,
)
from oneops.use_cases.uc08_fulfillment.contracts import (
    FulfillmentOutcome,
    Outcome,
    TaskType,
)
from oneops.use_cases.uc08_fulfillment.db import ConnectionProvider
from oneops.use_cases.uc08_fulfillment.errors import (
    AdapterInvocationError,
    RequestItemNotFoundError,
)

_log = structlog.get_logger("oneops.uc08.executor")
_tracer = trace.get_tracer("oneops.uc08.executor")

_MAX_WAVES = 64
_WAVE_CONCURRENCY = 8

_ADAPTER_CALL_TIMEOUT_S = float(os.environ.get(
    "UC08_ADAPTER_CALL_TIMEOUT_S", "60"))
_STUCK_TASK_RECOVERY_MINUTES = int(os.environ.get(
    "UC08_STUCK_TASK_RECOVERY_MINUTES", "10"))


# Saga compensation registry. Each entry declares the rollback contract
# for a forward tool. Production-grade alternative to a hardcoded
# if/elif chain (AWS Prescriptive Guidance + Temporal saga pattern).
#
# Future direction (deferred to follow-on): move these blocks into the
# tool registry JSONs (registries/v2/tools/uc08_fulfillment/*.json) so
# adding a new tool needs zero executor code change. Keeping it in-module
# for now because the registry-loader integration is its own work unit.
_COMPENSATION_REGISTRY: dict[str, tuple[str, str, str]] = {
    # forward_tool_id : (compensation_tool_id, kwarg_name, output_field)
    "create_directory_account": (
        "disable_directory_account", "account_id", "account_id"),
    "provision_email_mailbox": (
        "deprovision_email_mailbox", "mailbox_id", "mailbox_id"),
    "grant_vpn_access": (
        "revoke_vpn_access", "config_id", "config_id"),
    "assign_software_license": (
        "release_software_license", "license_id", "license_id"),
    "order_hardware_asset": (
        "cancel_hardware_order", "po_id", "po_id"),
}


# ── Plan inspection helpers ────────────────────────────────────────────────


def _ready_task_ids(tasks: list[dict[str, Any]]) -> list[str]:
    by_tmpl = {t["template_task_id"]: t for t in tasks}
    ready: list[str] = []
    for t in tasks:
        if t["state"] not in ("pending", "ready"):
            continue
        deps_ok = True
        for dep in t.get("depends_on") or []:
            dep_row = by_tmpl.get(dep)
            if dep_row is None or dep_row["state"] not in ("done", "skipped"):
                deps_ok = False
                break
        if deps_ok:
            ready.append(t["task_id"])
    return ready


def _transitively_blocked_task_ids(
    tasks: list[dict[str, Any]],
) -> list[tuple[str, str, int]]:
    """Returns [(task_id, current_state, version), …] for tasks that
    must be cascade-skipped because an upstream task failed."""
    failed_tmpl = {
        t["template_task_id"] for t in tasks if t["state"] == "failed"
    }
    if not failed_tmpl:
        return []
    poisoned: set[str] = set(failed_tmpl)
    changed = True
    while changed:
        changed = False
        for t in tasks:
            tmpl = t["template_task_id"]
            if tmpl in poisoned:
                continue
            for dep in t.get("depends_on") or []:
                if dep in poisoned:
                    poisoned.add(tmpl)
                    changed = True
                    break
    return [
        (t["task_id"], t["state"], t["version"])
        for t in tasks
        if t["state"] in ("pending", "ready") and t["template_task_id"] in poisoned
    ]


def _is_terminal(tasks: list[dict[str, Any]]) -> tuple[bool, str]:
    """Returns (terminal?, ritm_outcome) where outcome ∈
    {'fulfilled', 'partial', 'failed', 'blocked', ''}."""
    states = [t["state"] for t in tasks]
    in_flight = any(s in ("pending", "ready", "in_progress") for s in states)
    if in_flight:
        if all(s in ("blocked", "done", "skipped") for s in states):
            return True, "blocked"
        return False, ""
    if all(s in ("done", "skipped") for s in states):
        return True, "fulfilled"
    if any(s == "done" for s in states) and any(s == "failed" for s in states):
        return True, "partial"
    return True, "failed"


def _count_by_state(tasks: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tasks:
        out[t["state"]] = out.get(t["state"], 0) + 1
    return out


def _render_display(ritm_id: str, outcome: str, counts: dict[str, int]) -> str:
    done = counts.get("done", 0)
    failed = counts.get("failed", 0)
    total = sum(counts.values())
    if outcome == "fulfilled":
        return f"Fulfillment {ritm_id} completed: {done}/{total} tasks done."
    if outcome == "partial":
        return (
            f"Fulfillment {ritm_id} ended with partial failure: "
            f"{done}/{total} done, {failed} failed."
        )
    if outcome == "blocked":
        return f"Fulfillment {ritm_id} is blocked awaiting approval."
    return f"Fulfillment {ritm_id} failed: {failed} failed task(s)."


# ── Adapter dispatch ────────────────────────────────────────────────────────


async def _invoke_adapter(
    *, adapter: IntegrationAdapter, tool_id: str,
    tenant_id: str, idempotency_key: str,
    payload: dict[str, Any],
    sla_minutes: int | None = None,
) -> AdapterResponse:
    """Canonical naming: tool_id == adapter method name. No translation
    table. Unknown tool_id → AdapterInvocationError (fail-loud)."""
    method: Callable[..., Awaitable[AdapterResponse]] | None = getattr(
        adapter, tool_id, None,
    )
    if method is None or not callable(method):
        raise AdapterInvocationError(
            f"adapter {type(adapter).__name__} has no method {tool_id!r}",
        )
    # Per-task SLA wins (Issue 3); fall back to global default.
    if sla_minutes and sla_minutes > 0:
        # Cap real-world impact: an SLA in *minutes* is a workflow-level
        # deadline, not a single-call deadline. We use min(sla_seconds,
        # global cap) so a 1440-minute SLA doesn't translate to a 24h
        # wait_for that could mask a hang.
        timeout = min(sla_minutes * 60, _ADAPTER_CALL_TIMEOUT_S * 10)
    else:
        timeout = _ADAPTER_CALL_TIMEOUT_S
    try:
        return await asyncio.wait_for(
            method(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                **payload,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        return AdapterResponse(
            success=False,
            idempotency_key=idempotency_key,
            error_class=AdapterErrorClass.TIMEOUT,
            error_message=(
                f"adapter {tool_id} exceeded "
                f"{_ADAPTER_CALL_TIMEOUT_S}s timeout"
            ),
            error_code="ADAPTER_CALL_TIMEOUT",
        )
    except AdapterInvocationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AdapterInvocationError(
            f"adapter {tool_id} raised {type(exc).__name__}: {exc}",
        ) from exc


# ── Per-task execution ──────────────────────────────────────────────────────


async def _execute_one_task(
    *, tenant_id: str, ritm_id: str, task: dict[str, Any],
    adapter: IntegrationAdapter,
    connection_provider: ConnectionProvider,
) -> str:
    task_id = task["task_id"]
    tool_id = task["tool_id"]
    task_type = task["task_type"]
    payload = dict(task.get("input_payload") or {})
    version = task["version"]

    conn = await connection_provider()
    try:
        new_v = await _db.transition_task_state(
            tenant_id=tenant_id, task_id=task_id,
            from_state=task["state"], to_state="in_progress",
            version=version, conn=conn,
        )
        if new_v is None:
            return task["state"]
        version = new_v

        # Approval gate (tool_id-based, not task_type-based).
        if tool_id == "request_human_approval":
            approval_id = await _db.insert_approval(
                tenant_id=tenant_id, ritm_id=ritm_id, task_id=task_id,
                approval_type=payload.get("approval_type", "fulfillment_step"),
                reason=payload.get(
                    "reason", f"Approval required for {task['task_name']}"),
                requested_from=payload.get(
                    "requested_from",
                    task.get("assignment_group") or "manager"),
                payload=payload,
                langgraph_interrupt_id=None,
                conn=conn,
            )
            await _db.transition_task_state(
                tenant_id=tenant_id, task_id=task_id,
                from_state="in_progress", to_state="blocked",
                version=version,
                output_payload={"approval_id": approval_id},
                conn=conn,
            )
            return "blocked"

        # Tasks without an adapter binding → human work, marked done.
        if not tool_id:
            await _db.transition_task_state(
                tenant_id=tenant_id, task_id=task_id,
                from_state="in_progress", to_state="done",
                version=version,
                output_payload={"resolved": "no_tool_noop"},
                conn=conn,
            )
            return "done"

        if task_type == TaskType.MANUAL.value:
            await _db.transition_task_state(
                tenant_id=tenant_id, task_id=task_id,
                from_state="in_progress", to_state="done",
                version=version,
                output_payload={"resolved": "manual_auto"},
                conn=conn,
            )
            return "done"

        with _tracer.start_as_current_span(
            f"uc08.task.{tool_id}",
            attributes={
                "oneops.tenant_id": tenant_id,
                "uc08.ritm_id": ritm_id,
                "uc08.task_id": task_id,
                "uc08.tool_id": tool_id,
                "uc08.retry_count": task["retry_count"],
            },
        ) as span:
            idem_key = f"{ritm_id}:{task_id}:{task['retry_count']}"
            response = await _invoke_adapter(
                adapter=adapter, tool_id=tool_id,
                tenant_id=tenant_id, idempotency_key=idem_key,
                payload=payload,
                sla_minutes=task.get("sla_minutes"),
            )

        if response.success:
            await _db.transition_task_state(
                tenant_id=tenant_id, task_id=task_id,
                from_state="in_progress", to_state="done",
                version=version,
                output_payload=(
                    response.result.model_dump(mode="json")
                    if response.result is not None else None
                ),
                conn=conn,
            )
            return "done"

        err_class = response.error_class or AdapterErrorClass.PERMANENT
        retry_count = int(task["retry_count"]) + 1
        max_retries = int(task["max_retries"] or 3)
        span.set_attribute("uc08.error_class", err_class.value)

        # TIMEOUT is treated like TRANSIENT for retry-budget purposes.
        retryable = err_class in (
            AdapterErrorClass.TRANSIENT, AdapterErrorClass.TIMEOUT,
        )

        if retryable and retry_count < max_retries:
            await _db.transition_task_state(
                tenant_id=tenant_id, task_id=task_id,
                from_state="in_progress", to_state="ready",
                version=version,
                error_message=response.error_message,
                error_code=err_class.value,
                retry_count=retry_count,
                conn=conn,
            )
            return "ready"

        await _db.transition_task_state(
            tenant_id=tenant_id, task_id=task_id,
            from_state="in_progress", to_state="failed",
            version=version,
            error_message=response.error_message,
            error_code=err_class.value,
            retry_count=retry_count,
            conn=conn,
        )
        return "failed"
    finally:
        await conn.close()


# ── Public entry — execute_plan ────────────────────────────────────────────


async def execute_plan(
    *, tenant_id: str, ritm_id: str,
    adapter: IntegrationAdapter,
    connection_provider: ConnectionProvider | None = None,
    trace_id: str | None = None,
) -> Outcome:
    """Drive a persisted RITM toward terminal state. Idempotent.

    Concurrency: a per-RITM advisory lock (held by a dedicated
    `lock_conn` for the full execute_plan call) serialises two workers
    on the same RITM. If the lock is contended, returns a partial-status
    snapshot rather than blocking — caller retries.
    """
    cp = connection_provider or _db.default_connection_provider

    # ── Per-RITM advisory lock (Issue 4 hardening) ─────────────────────
    # int4 keyspace; hash collisions are harmless (two RITMs sharing a key
    # just serialise; the SQL predicates inside helpers prevent any
    # cross-RITM data access).
    ritm_lock_key = hash(ritm_id) % (2 ** 31)
    lock_conn = await cp()
    try:
        lock_ok = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1::int)", ritm_lock_key,
        )
        if not lock_ok:
            _log.info("uc08.executor.lock_contended", ritm_id=ritm_id)
            snap_conn = await cp()
            try:
                tasks = await _db.list_tasks_for_ritm(
                    tenant_id=tenant_id, ritm_id=ritm_id, conn=snap_conn,
                )
            finally:
                await snap_conn.close()
            return await _partial_status(
                tenant_id=tenant_id, ritm_id=ritm_id,
                tasks=tasks, connection_provider=cp,
                trace_id=trace_id,
            )

        return await _execute_plan_locked(
            tenant_id=tenant_id, ritm_id=ritm_id,
            adapter=adapter, connection_provider=cp,
            trace_id=trace_id,
        )
    finally:
        try:
            await lock_conn.fetchval(
                "SELECT pg_advisory_unlock($1::int)", ritm_lock_key,
            )
        except Exception:  # noqa: BLE001
            pass
        await lock_conn.close()


async def _execute_plan_locked(
    *, tenant_id: str, ritm_id: str,
    adapter: IntegrationAdapter,
    connection_provider: ConnectionProvider,
    trace_id: str | None = None,
) -> Outcome:
    """Wave loop. Caller must hold the per-RITM advisory lock."""
    cp = connection_provider

    with _tracer.start_as_current_span(
        "uc08.executor.execute_plan",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.ritm_id": ritm_id,
        },
    ):
        # Initial RITM transition + stuck-task recovery.
        conn = await cp()
        try:
            ritm = await _db.get_ritm(
                tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
            )
            if ritm is None:
                raise RequestItemNotFoundError(
                    f"RITM {ritm_id} not found for tenant {tenant_id}",
                )
            if ritm["state"] == "requested":
                await _db.transition_ritm_state(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    from_state="requested", to_state="in_progress",
                    version=ritm["version"], conn=conn,
                )

            recovered = await conn.fetch(
                """
                UPDATE itsm.task
                   SET state='ready',
                       retry_count = retry_count + 1,
                       error_message='recovered_from_orphan',
                       updated_at=now(),
                       version = version + 1
                 WHERE tenant_id=$1 AND ritm_id=$2
                   AND state='in_progress'
                   AND started_at < now() - ($3 || ' minutes')::interval
                RETURNING task_id
                """,
                tenant_id, ritm_id, str(_STUCK_TASK_RECOVERY_MINUTES),
            )
            if recovered:
                _log.warning("uc08.executor.stuck_tasks_recovered",
                             ritm_id=ritm_id, count=len(recovered))
        finally:
            await conn.close()

        sem = asyncio.Semaphore(_WAVE_CONCURRENCY)

        async def _bounded(coro):
            async with sem:
                return await coro

        for _wave in range(_MAX_WAVES):
            conn = await cp()
            try:
                tasks = await _db.list_tasks_for_ritm(
                    tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
                )
            finally:
                await conn.close()

            # Cascade-skip: tasks downstream of any failed task get
            # 'skipped' so _is_terminal can fire.
            to_skip = _transitively_blocked_task_ids(tasks)
            if to_skip:
                conn = await cp()
                try:
                    for task_id, cur_state, ver in to_skip:
                        await _db.transition_task_state(
                            tenant_id=tenant_id, task_id=task_id,
                            from_state=cur_state, to_state="skipped",
                            version=ver,
                            error_message="upstream_task_failed",
                            error_code="CASCADE_SKIPPED",
                            conn=conn,
                        )
                finally:
                    await conn.close()
                conn = await cp()
                try:
                    tasks = await _db.list_tasks_for_ritm(
                        tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
                    )
                finally:
                    await conn.close()

            terminal, ritm_outcome = _is_terminal(tasks)
            if terminal:
                return await _finalise(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    ritm_outcome=ritm_outcome, tasks=tasks,
                    connection_provider=cp, trace_id=trace_id,
                )

            ready_ids = _ready_task_ids(tasks)
            if not ready_ids:
                return await _partial_status(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    tasks=tasks, connection_provider=cp,
                    trace_id=trace_id,
                )

            ready_tasks = [t for t in tasks if t["task_id"] in ready_ids]
            results = await asyncio.gather(*(
                _bounded(_execute_one_task(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    task=t, adapter=adapter,
                    connection_provider=cp,
                ))
                for t in ready_tasks
            ), return_exceptions=True)

            for t, res in zip(ready_tasks, results, strict=False):
                if isinstance(res, Exception):
                    _log.error("uc08.executor.wave.task_raised",
                               ritm_id=ritm_id, task_id=t["task_id"],
                               error=str(res),
                               error_type=type(res).__name__)

        return await _finalise(
            tenant_id=tenant_id, ritm_id=ritm_id,
            ritm_outcome="failed",
            tasks=[], connection_provider=cp, trace_id=trace_id,
            failure_reason=f"max waves ({_MAX_WAVES}) exceeded",
        )


# ── Terminal / partial status helpers ──────────────────────────────────────


async def _finalise(
    *, tenant_id: str, ritm_id: str, ritm_outcome: str,
    tasks: list[dict[str, Any]],
    connection_provider: ConnectionProvider,
    trace_id: str | None = None,
    failure_reason: str | None = None,
) -> Outcome:
    """Close the RITM + fulfillment_run. State vocabulary aligns with
    DB CHECK constraints: request_item.state allows {fulfilled, failed,
    cancelled, in_progress}; fulfillment_run.outcome allows {fulfilled,
    partial, failed, cancelled}."""
    conn = await connection_provider()
    try:
        ritm = await _db.get_ritm(
            tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
        )
        if ritm is None:
            raise RequestItemNotFoundError(ritm_id)

        if not tasks:
            tasks = await _db.list_tasks_for_ritm(
                tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
            )

        counts = _count_by_state(tasks)
        # RITM state vocabulary: 'partial' isn't a valid RITM state —
        # partial fulfilment lands as 'failed' at the RITM level (with
        # completed_tasks > 0 telling the full story) and 'partial' at
        # the run level (fulfillment_run.outcome).
        ritm_new_state = {
            "fulfilled": "fulfilled",
            "partial": "failed",
            "failed": "failed",
            "blocked": "in_progress",
        }[ritm_outcome]
        if ritm["state"] != ritm_new_state:
            await _db.transition_ritm_state(
                tenant_id=tenant_id, ritm_id=ritm_id,
                from_state=ritm["state"], to_state=ritm_new_state,
                version=ritm["version"],
                completed_tasks=counts.get("done", 0),
                failed_tasks=counts.get("failed", 0),
                conn=conn,
            )

        run_row = await conn.fetchrow(
            """
            SELECT run_id FROM itsm.fulfillment_run
             WHERE tenant_id=$1 AND ritm_id=$2
             ORDER BY started_at DESC LIMIT 1
            """,
            tenant_id, ritm_id,
        )
        if run_row is not None:
            await _db.finalise_run(
                tenant_id=tenant_id, run_id=run_row["run_id"],
                outcome=ritm_outcome,
                summary={
                    "tasks_by_state": counts,
                    "failure_reason": failure_reason,
                },
                conn=conn,
            )
    finally:
        await conn.close()

    outcome_enum = {
        "fulfilled": FulfillmentOutcome.FULFILLED,
        "partial": FulfillmentOutcome.PARTIAL,
        "failed": FulfillmentOutcome.FAILED,
        "blocked": FulfillmentOutcome.IN_PROGRESS,
    }[ritm_outcome]

    # Production metrics — UC-8 fulfilment outcome distribution.
    _metric_inc("ai.uc08.fulfilment.total", 1,
                tenant_id=tenant_id,
                outcome=ritm_outcome)
    _metric_inc("ai.agent.runs.total", 1,
                agent_id="uc08_fulfillment",
                tenant_id=tenant_id,
                source="executor",
                status="ok" if ritm_outcome == "fulfilled" else ritm_outcome)

    return Outcome(
        tenant_id=tenant_id,
        request_id=ritm["request_id"],
        ritm_id=ritm_id,
        catalog_item_id=ritm["catalog_item_id"],
        run_id=(run_row["run_id"] if run_row else "RUN_UNKNOWN"),
        outcome=outcome_enum,
        tasks_total=int(ritm["total_tasks"]),
        tasks_completed=counts.get("done", 0),
        tasks_failed=counts.get("failed", 0),
        tasks_skipped=counts.get("skipped", 0),
        tasks_in_progress=counts.get("in_progress", 0),
        trace_id=trace_id,
        display_text=_render_display(ritm_id, ritm_outcome, counts),
    )


async def _partial_status(
    *, tenant_id: str, ritm_id: str,
    tasks: list[dict[str, Any]],
    connection_provider: ConnectionProvider,
    trace_id: str | None = None,
) -> Outcome:
    conn = await connection_provider()
    try:
        ritm = await _db.get_ritm(
            tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
        )
        pending_approvals = [
            row["approval_id"]
            for row in await conn.fetch(
                "SELECT approval_id FROM itsm.approval "
                "WHERE tenant_id=$1 AND ritm_id=$2 AND state='pending'",
                tenant_id, ritm_id,
            )
        ]
        latest_run = await conn.fetchval(
            "SELECT run_id FROM itsm.fulfillment_run "
            "WHERE tenant_id=$1 AND ritm_id=$2 "
            "ORDER BY started_at DESC LIMIT 1",
            tenant_id, ritm_id,
        )
    finally:
        await conn.close()

    counts = _count_by_state(tasks)
    return Outcome(
        tenant_id=tenant_id,
        request_id=ritm["request_id"],
        ritm_id=ritm_id,
        catalog_item_id=ritm["catalog_item_id"],
        run_id=latest_run or "RUN_UNKNOWN",
        outcome=FulfillmentOutcome.IN_PROGRESS,
        tasks_total=int(ritm["total_tasks"]),
        tasks_completed=counts.get("done", 0),
        tasks_failed=counts.get("failed", 0),
        tasks_skipped=counts.get("skipped", 0),
        tasks_in_progress=counts.get("in_progress", 0),
        trace_id=trace_id,
        display_text=(
            f"{ritm_id} awaiting "
            f"{len(pending_approvals)} approval(s); "
            f"{counts.get('done', 0)}/{ritm['total_tasks']} tasks done."
        ),
    )


# ── Saga compensation ──────────────────────────────────────────────────────


async def compensate_ritm(
    *, tenant_id: str, ritm_id: str,
    adapter: IntegrationAdapter,
    connection_provider: ConnectionProvider | None = None,
    reason: str = "user_cancelled",
) -> dict[str, Any]:
    """Roll back every completed integration task for a RITM. Saga
    semantics: fulfilled → cancelled is valid; only cancelled→cancelled
    is a no-op."""
    cp = connection_provider or _db.default_connection_provider

    conn = await cp()
    try:
        tasks = await _db.list_tasks_for_ritm(
            tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
        )
    finally:
        await conn.close()

    completed = sorted(
        [t for t in tasks if t["state"] == "done"],
        key=lambda t: t.get("template_task_id") or "",
        reverse=True,
    )

    results: list[dict[str, Any]] = []
    for t in completed:
        binding = _COMPENSATION_REGISTRY.get(t["tool_id"])
        if binding is None:
            results.append({
                "task_id": t["task_id"], "tool_id": t["tool_id"],
                "compensation": "skipped_no_op",
            })
            continue
        comp_tool_id, id_kwarg, id_source = binding
        comp_callable = getattr(adapter, comp_tool_id, None)
        if comp_callable is None:
            results.append({
                "task_id": t["task_id"], "tool_id": t["tool_id"],
                "compensation": comp_tool_id, "ok": False,
                "error": f"adapter missing method {comp_tool_id!r}",
            })
            continue
        output = t.get("output_payload") or {}
        if isinstance(output, dict) and "result" in output:
            output = output["result"]
        comp_id = (output or {}).get(id_source)
        if not comp_id:
            results.append({
                "task_id": t["task_id"], "tool_id": t["tool_id"],
                "compensation": comp_tool_id, "ok": False,
                "error": f"missing {id_source!r} in output_payload",
            })
            continue
        try:
            resp = await comp_callable(
                tenant_id=tenant_id,
                idempotency_key=f"compensate:{ritm_id}:{t['task_id']}",
                **{id_kwarg: comp_id},
            )
            results.append({
                "task_id": t["task_id"], "tool_id": t["tool_id"],
                "compensation": comp_tool_id,
                "ok": bool(getattr(resp, "success", False)),
            })
        except Exception as exc:  # noqa: BLE001
            _log.error("uc08.compensate.failed",
                       ritm_id=ritm_id, task_id=t["task_id"],
                       error=str(exc))
            results.append({
                "task_id": t["task_id"], "tool_id": t["tool_id"],
                "compensation": comp_tool_id, "ok": False,
                "error": str(exc),
            })

    # Saga: 'fulfilled' is a valid source for cancellation. Only
    # 'cancelled' is a no-op terminal.
    conn = await cp()
    try:
        ritm = await _db.get_ritm(
            tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
        )
        if ritm and ritm["state"] != "cancelled":
            await _db.transition_ritm_state(
                tenant_id=tenant_id, ritm_id=ritm_id,
                from_state=ritm["state"], to_state="cancelled",
                version=ritm["version"], conn=conn,
            )
    finally:
        await conn.close()

    return {
        "ritm_id": ritm_id,
        "compensated_tasks": len(results),
        "reason": reason,
        "results": results,
    }


__all__ = ["execute_plan", "compensate_ritm"]
