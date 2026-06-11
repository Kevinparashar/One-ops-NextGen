"""Catalog completeness validation — the load-time guardrail (§2.7).

Enforces the invariants that make silent no-op fulfilment *structurally*
impossible — the root-cause class behind the split-brain onboarding bug:

  1. an ``automated`` task MUST declare a ``tool_id`` — otherwise the
     executor has nothing to dispatch and the task completes having done
     nothing (a silent failure);
  2. a ``tool_id``, if present, MUST name a real integration (the canonical
     ``VALID_TASK_TOOL_IDS`` surface) — catches typos and stale renames;
  3. a tool task's ``input_template`` keys MUST match the integration's
     parameters exactly — catches a template the executor cannot dispatch.

The loader (``database/catalog_fulfillment/load_data.py``) calls
:func:`validate_catalog_items` before a single row is written, so a
malformed catalog fails the load loudly instead of seeding items that
no-op at runtime. All problems are aggregated — never fail-on-first — so
one load run surfaces the full repair list.
"""
from __future__ import annotations

import inspect
from typing import Any

from oneops.use_cases.uc08_fulfillment.adapters.protocol import (
    FORWARD_TOOL_IDS,
    VALID_TASK_TOOL_IDS,
    IntegrationAdapter,
)

# Framework-supplied params every integration takes — not business inputs a
# catalog task's input_template is responsible for.
_RESERVED_PARAMS = frozenset({"self", "tenant_id", "idempotency_key"})


class CatalogValidationError(ValueError):
    """One or more catalog items violate the completeness invariants.

    The message aggregates every problem found in the batch.
    """


def required_params(tool_id: str) -> frozenset[str]:
    """Business parameters an integration expects (excluding the
    framework-supplied ``tenant_id`` / ``idempotency_key``).

    Introspected from the :class:`IntegrationAdapter` Protocol so it tracks
    the contract rather than a hand-kept list — a renamed param is caught,
    not silently accepted.
    """
    fn = getattr(IntegrationAdapter, tool_id, None)
    if fn is None:
        return frozenset()
    return frozenset(
        p for p in inspect.signature(fn).parameters if p not in _RESERVED_PARAMS
    )


def validate_catalog_items(items: list[dict[str, Any]]) -> None:
    """Raise :class:`CatalogValidationError` if any task breaks an invariant.

    No-op when every task is valid. Safe to call on the full catalog batch.
    """
    problems: list[str] = []
    for item in items:
        cid = item.get("catalog_item_id", "?")
        for task in item.get("tasks") or []:
            problems.extend(_validate_task(cid, task))
    if problems:
        raise CatalogValidationError(
            f"{len(problems)} catalog task invariant violation(s):\n  - "
            + "\n  - ".join(problems)
        )


def _validate_task(cid: str, task: dict[str, Any]) -> list[str]:
    tid = task.get("task_id", "?")
    task_type = task.get("type")
    tool_id = task.get("tool_id")
    out: list[str] = []

    # 1. automated ⇒ tool_id (the silent-no-op guard)
    if task_type == "automated" and not tool_id:
        out.append(
            f"{cid}/{tid}: type=automated but no tool_id "
            f"(would silently no-op at runtime)"
        )

    # 2. tool_id ⇒ a known integration
    if tool_id and tool_id not in VALID_TASK_TOOL_IDS:
        out.append(
            f"{cid}/{tid}: tool_id '{tool_id}' is not a known integration "
            f"(valid: {sorted(VALID_TASK_TOOL_IDS)})"
        )

    # 3. tool task input_template must match the integration's parameters
    if tool_id in FORWARD_TOOL_IDS:
        template = task.get("input_template") or {}
        want = required_params(tool_id)
        extra = set(template) - want
        missing = want - set(template)
        if extra or missing:
            out.append(
                f"{cid}/{tid}: input_template for '{tool_id}' has keys "
                f"{sorted(template)} but the integration expects {sorted(want)} "
                f"(extra={sorted(extra)}, missing={sorted(missing)})"
            )

    return out
