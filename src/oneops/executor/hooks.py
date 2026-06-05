"""Lifecycle hooks — deterministic gates around a step (AgentScript pattern).

An agent record declares `hooks.before_invocation` / `after_invocation` —
lists of hook ids. Before a step runs, its before-hooks run; after it
produces a result, its after-hooks run. Hooks run **in code**, never in a
prompt: auth re-checks, state validation, output transformation/redaction.

A before-hook that raises `HookError` **aborts the step** — a typed, traced
abort, never a swallowed exception. An after-hook may mutate
`HookContext.result` (e.g. redact a field) and may also abort.

`HookRegistry` maps hook id → callable; `default_hook_registry()` ships the
platform built-ins. Service-dependent hooks (e.g. an authz re-check) register
the same way, reading what they need from `HookContext.services`.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from oneops.errors import OneOpsError


class HookError(OneOpsError):
    """A lifecycle hook gate failed — the step must not proceed / is invalid."""

    code = "HOOK_GATE_FAILED"


class HookPhase(StrEnum):
    BEFORE = "before"
    AFTER = "after"


@dataclass
class HookContext:
    """What a hook receives. Mutable so an after-hook can transform `result`."""

    agent_id: str
    phase: HookPhase
    step: dict[str, Any]
    request: dict[str, Any]
    result: dict[str, Any] | None = None        # set for AFTER hooks
    services: dict[str, Any] = field(default_factory=dict)   # injected deps


# A hook is an async callable over a HookContext; it raises HookError to abort.
Hook = Callable[[HookContext], Awaitable[None]]


class HookRegistry:
    """Hook id → hook callable. Immutable lookups; explicit registration."""

    def __init__(self) -> None:
        self._hooks: dict[str, Hook] = {}

    def register(self, hook_id: str, hook: Hook) -> None:
        if hook_id in self._hooks:
            raise ValueError(f"hook '{hook_id}' is already registered")
        self._hooks[hook_id] = hook

    def get(self, hook_id: str) -> Hook:
        hook = self._hooks.get(hook_id)
        if hook is None:
            # A declared-but-unregistered hook is a misconfiguration — fail
            # loud at run time rather than silently skip a gate.
            raise HookError(f"hook '{hook_id}' is declared by an agent but not registered")
        return hook

    async def run(self, hook_ids: list[str], ctx: HookContext) -> None:
        """Run each hook in declared order. The first `HookError` aborts."""
        for hook_id in hook_ids:
            await self.get(hook_id)(ctx)

    @property
    def registered(self) -> frozenset[str]:
        return frozenset(self._hooks)


# ── Built-in hooks ───────────────────────────────────────────────────────


async def hook_state_validate(ctx: HookContext) -> None:
    """BEFORE — sanity-check the step is well-formed before any work runs.

    A planner contract violation (no agent_id, malformed parameters) is caught
    here as a typed abort, not as an obscure failure deep in the handler."""
    if not ctx.step.get("agent_id"):
        raise HookError("state_validate: step has no agent_id")
    params = ctx.step.get("parameters")
    if params is not None and not isinstance(params, dict):
        raise HookError(f"state_validate: step parameters must be a dict, got {type(params)}")


async def hook_output_redact(ctx: HookContext) -> None:
    """AFTER — mark the step output as having passed redaction.

    P6 ships the hook-point and the contract; the policy engine (P10) supplies
    the field-level redaction rules. Until then this records that the step's
    output crossed the redaction boundary."""
    if ctx.result is not None:
        ctx.result["redacted"] = True


async def hook_authz_recheck(ctx: HookContext) -> None:
    """BEFORE — re-evaluate RBAC + ABAC at step entry (Component Spec C16).

    The router authorizes the *plan* up front, but a step that is dispatched
    later (after an interrupt, a fan-out, or a multi-wave run) may execute
    under different facts — a role downgrade, a tenant freeze, an updated
    ABAC policy. Action-tier agents declare this hook as their default
    `before_invocation` gate so the decision is re-taken at the moment of
    action, not at the moment of planning.

    Inputs (via `ctx.services`, injected by the executor):
      * `services["authz"]` — `AuthzService` (deny-by-default)
      * `services["agent"]` — the active `AgentRecord` (resource facts)

    Inputs (via `ctx.request`):
      * `tenant_id`, `user_id`, `role`, and ABAC `attributes` (tuple of pairs)

    Failure modes:
      * services missing → loud `HookError` (misconfiguration, never silent).
      * authz DENY → `HookError("authz_recheck: …reasons")` — the executor
        records the typed abort with the deny reasons in the trace.
    """
    # Lazy imports keep the hook module decoupled from the authz package at
    # import time (cold-start friendly — see scale concern 21).
    from oneops.authz.models import (
        DataClass,
        Principal,
        ResourceDescriptor,
        Tier,
    )
    from oneops.authz.service import AuthzService
    from oneops.registry.models import AgentRecord

    authz = ctx.services.get("authz")
    agent = ctx.services.get("agent")
    if not isinstance(authz, AuthzService):
        raise HookError(
            "authz_recheck: services['authz'] is not an AuthzService — "
            "executor wiring is incomplete (no silent skip)")
    if not isinstance(agent, AgentRecord):
        raise HookError(
            "authz_recheck: services['agent'] is not an AgentRecord — "
            "executor wiring is incomplete (no silent skip)")

    tenant_id = str(ctx.request.get("tenant_id") or "").strip()
    user_id = str(ctx.request.get("user_id") or "").strip()
    role = str(ctx.request.get("role") or "").strip()
    if not tenant_id or not role:
        raise HookError(
            "authz_recheck: request envelope missing tenant_id/role — "
            "cannot construct a Principal (deny-by-default)")

    raw_attrs = ctx.request.get("attributes") or ()
    if isinstance(raw_attrs, dict):
        attributes = tuple((str(k), str(v)) for k, v in raw_attrs.items())
    else:
        attributes = tuple(
            (str(k), str(v)) for k, v in raw_attrs
            if isinstance(k, str) or isinstance(v, str) or True
        )

    principal = Principal(
        tenant_id=tenant_id, user_id=user_id, role=role, attributes=attributes,
    )

    # `data_classification` on AbacTags is the registry enum; map by name —
    # data-driven, no static catalogue (Component Spec C12). Unknown values
    # collapse to INTERNAL (deny-by-default for stricter classes still bites
    # in the ABAC rule eval).
    data_class_name = agent.abac_tags.data_classification.value
    try:
        data_class = DataClass(data_class_name)
    except ValueError:
        data_class = DataClass.INTERNAL

    # Tier — evaluate at the STEP's effective tier, not blanket the agent tier.
    # The executor injects `step_is_action` (from `_step_is_action`, the same
    # decision that drives the action-approval interrupt): an action-tier AGENT
    # may own read tools (analysis / propose) and action tools (apply); a
    # read-only step under such an agent must be checked as READ, so generating
    # a recommend-only proposal does not demand write-class permission. When the
    # hint is absent (non-executor caller), fall back to the agent tier.
    step_is_action = ctx.services.get("step_is_action")
    if step_is_action is None:
        tier = Tier.ACTION if agent.abac_tags.tier.value == "action" else Tier.READ
    else:
        tier = Tier.ACTION if step_is_action else Tier.READ

    resource = ResourceDescriptor(
        resource_id=agent.id,
        resource_tenant_id=tenant_id,
        tier=tier,
        data_classification=data_class,
        audience=tuple(agent.abac_tags.audience),
        required_scopes=(),
    )

    decision = await authz.check(principal, resource)
    if not decision.allowed:
        raise HookError(
            "authz_recheck: " + "; ".join(decision.reasons) if decision.reasons
            else "authz_recheck: denied")


def default_hook_registry() -> HookRegistry:
    """The platform hook registry with the built-in hooks registered.

    `builtin:authz_recheck` is the default gate for action-tier agents — any
    agent record may reference it in `hooks.before_invocation` without each
    UC team re-implementing the same RBAC+ABAC re-check (Component Spec C16,
    closes substrate gap G5)."""
    registry = HookRegistry()
    registry.register("hook_state_validate", hook_state_validate)
    registry.register("hook_output_redact", hook_output_redact)
    registry.register("builtin:authz_recheck", hook_authz_recheck)
    return registry


__all__ = [
    "HookError",
    "HookPhase",
    "HookContext",
    "Hook",
    "HookRegistry",
    "hook_state_validate",
    "hook_output_redact",
    "hook_authz_recheck",
    "default_hook_registry",
]
