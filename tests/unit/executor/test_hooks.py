"""Lifecycle-hook framework tests."""
from __future__ import annotations

import pytest

from oneops.executor.hooks import (
    HookContext,
    HookError,
    HookPhase,
    HookRegistry,
    default_hook_registry,
    hook_authz_recheck,
    hook_output_redact,
    hook_state_validate,
)


def _ctx(phase=HookPhase.BEFORE, step=None, result=None):
    return HookContext(
        agent_id="uc_a", phase=phase,
        step=step if step is not None else {"agent_id": "uc_a", "parameters": {}},
        request={}, result=result)


# ── registry ─────────────────────────────────────────────────────────────


async def test_register_and_get():
    reg = HookRegistry()

    async def h(ctx):
        return None

    reg.register("h1", h)
    assert reg.get("h1") is h


async def test_get_unregistered_hook_fails_loud():
    with pytest.raises(HookError, match="not registered"):
        HookRegistry().get("hook_ghost")


async def test_register_duplicate_is_rejected():
    reg = HookRegistry()

    async def h(ctx):
        return None

    reg.register("h1", h)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("h1", h)


async def test_run_executes_hooks_in_order():
    reg = HookRegistry()
    calls: list[str] = []

    async def a(ctx):
        calls.append("a")

    async def b(ctx):
        calls.append("b")

    reg.register("a", a)
    reg.register("b", b)
    await reg.run(["a", "b"], _ctx())
    assert calls == ["a", "b"]


async def test_run_empty_list_is_a_noop():
    await HookRegistry().run([], _ctx())          # must not raise


async def test_a_raising_hook_aborts_run():
    reg = HookRegistry()

    async def boom(ctx):
        raise HookError("gate failed")

    reg.register("boom", boom)
    with pytest.raises(HookError, match="gate failed"):
        await reg.run(["boom"], _ctx())


# ── built-in hooks ───────────────────────────────────────────────────────


def test_default_registry_has_the_builtins():
    reg = default_hook_registry()
    assert "hook_state_validate" in reg.registered
    assert "hook_output_redact" in reg.registered


async def test_state_validate_passes_a_well_formed_step():
    await hook_state_validate(_ctx(step={"agent_id": "uc_a", "parameters": {}}))


async def test_state_validate_aborts_on_missing_agent_id():
    with pytest.raises(HookError, match="no agent_id"):
        await hook_state_validate(_ctx(step={"agent_id": "", "parameters": {}}))


async def test_state_validate_aborts_on_bad_parameters():
    with pytest.raises(HookError, match="must be a dict"):
        await hook_state_validate(_ctx(step={"agent_id": "uc_a", "parameters": [1, 2]}))


async def test_output_redact_marks_the_result():
    result: dict = {"output": {"x": 1}}
    await hook_output_redact(_ctx(phase=HookPhase.AFTER, result=result))
    assert result["redacted"] is True


async def test_output_redact_tolerates_no_result():
    await hook_output_redact(_ctx(phase=HookPhase.AFTER, result=None))   # no raise


# ── builtin:authz_recheck (substrate gap G5) ─────────────────────────────


def _agent_record(*, agent_id="uc_action", tier="action",
                  audience=(), data_class="internal"):
    """Build an AgentRecord using `model_construct` so we don't have to satisfy
    every field — we only assert against the abac_tags facts the hook reads."""
    from oneops.registry.models import (
        AbacTags,
        AgentRecord,
        DataClassification,
        ExecutionTier,
    )
    return AgentRecord.model_construct(
        id=agent_id,
        abac_tags=AbacTags(
            service=(),
            tier=ExecutionTier(tier),
            audience=tuple(audience),
            data_classification=DataClassification(data_class),
        ),
    )


class _FakeAuthz:
    """Minimal stand-in: records every check, returns scripted decisions."""

    def __init__(self, allow=True, reasons=()):
        from oneops.authz.models import AuthzDecision
        self._decision = (
            AuthzDecision.allow() if allow
            else AuthzDecision.deny(tuple(reasons) or ("policy",))
        )
        self.calls: list = []

    async def check(self, principal, resource):
        self.calls.append((principal, resource))
        return self._decision


def _recheck_ctx(*, services=None, request=None):
    return HookContext(
        agent_id="uc_action", phase=HookPhase.BEFORE,
        step={"agent_id": "uc_action", "parameters": {}},
        request=request if request is not None else {
            "tenant_id": "t1", "user_id": "u1", "role": "service_desk_agent",
        },
        services=services if services is not None else {},
    )


# Real AuthzService subclass-isinstance check — use the real type at runtime
# so the hook's isinstance gate doesn't reject the fake. Done by registering
# the fake under the real class name via duck-typing isn't enough; instead we
# patch `AuthzService` import target temporarily.
@pytest.fixture
def patched_authz_check(monkeypatch):
    """Make the hook's `isinstance(authz, AuthzService)` accept _FakeAuthz."""
    from oneops.executor import hooks as hooks_mod

    real_import_authz = hooks_mod.hook_authz_recheck

    async def wrapper(ctx):
        # Patch isinstance inside the hook by injecting via a tagged service.
        # Simpler: subclass _FakeAuthz from the real AuthzService.
        return await real_import_authz(ctx)

    return wrapper


@pytest.fixture
def authz_subclass():
    """A _FakeAuthz that IS-A AuthzService — passes the isinstance gate."""
    from oneops.authz.service import AuthzService

    class FakeAuthz(AuthzService):                  # type: ignore[misc]
        def __init__(self, *, allow=True, reasons=()):
            from oneops.authz.models import AuthzDecision
            self._decision = (
                AuthzDecision.allow() if allow
                else AuthzDecision.deny(tuple(reasons) or ("policy",))
            )
            self.calls: list = []

        async def check(self, principal, resource):    # type: ignore[override]
            self.calls.append((principal, resource))
            return self._decision

    return FakeAuthz


async def test_authz_recheck_allows_when_authz_allows(authz_subclass):
    fake = authz_subclass(allow=True)
    ctx = _recheck_ctx(services={
        "authz": fake, "agent": _agent_record(),
    })
    await hook_authz_recheck(ctx)                   # no raise
    assert len(fake.calls) == 1
    principal, resource = fake.calls[0]
    assert principal.tenant_id == "t1"
    assert principal.role == "service_desk_agent"
    assert resource.resource_id == "uc_action"
    assert resource.tier.value == "action"


async def test_authz_recheck_read_step_under_action_agent_checks_as_read(
        authz_subclass):
    # Per-tool granularity: a read-only step under an ACTION-tier agent (the
    # executor injects step_is_action=False) must be checked at READ tier, so a
    # recommend-only propose does not demand write-class permission.
    fake = authz_subclass(allow=True)
    ctx = _recheck_ctx(services={
        "authz": fake, "agent": _agent_record(tier="action"),
        "step_is_action": False,
    })
    await hook_authz_recheck(ctx)
    _, resource = fake.calls[0]
    assert resource.tier.value == "read"


async def test_authz_recheck_action_step_checks_as_action(authz_subclass):
    # The action tool under the same agent (step_is_action=True) stays ACTION.
    fake = authz_subclass(allow=True)
    ctx = _recheck_ctx(services={
        "authz": fake, "agent": _agent_record(tier="action"),
        "step_is_action": True,
    })
    await hook_authz_recheck(ctx)
    _, resource = fake.calls[0]
    assert resource.tier.value == "action"


async def test_authz_recheck_denies_with_reasons(authz_subclass):
    fake = authz_subclass(allow=False, reasons=("role_not_in_audience", "tier"))
    ctx = _recheck_ctx(services={
        "authz": fake, "agent": _agent_record(),
    })
    with pytest.raises(HookError, match="role_not_in_audience"):
        await hook_authz_recheck(ctx)


async def test_authz_recheck_fails_loud_without_authz_wiring():
    # Services dict missing 'authz' — must be HookError, never a silent pass.
    ctx = _recheck_ctx(services={"agent": _agent_record()})
    with pytest.raises(HookError, match="services\\['authz'\\]"):
        await hook_authz_recheck(ctx)


async def test_authz_recheck_fails_loud_without_agent_wiring(authz_subclass):
    ctx = _recheck_ctx(services={"authz": authz_subclass(allow=True)})
    with pytest.raises(HookError, match="services\\['agent'\\]"):
        await hook_authz_recheck(ctx)


async def test_authz_recheck_fails_loud_on_missing_tenant(authz_subclass):
    ctx = _recheck_ctx(
        services={"authz": authz_subclass(allow=True), "agent": _agent_record()},
        request={"tenant_id": "", "user_id": "u1", "role": "service_desk_agent"},
    )
    with pytest.raises(HookError, match="tenant_id/role"):
        await hook_authz_recheck(ctx)


async def test_authz_recheck_fails_loud_on_missing_role(authz_subclass):
    ctx = _recheck_ctx(
        services={"authz": authz_subclass(allow=True), "agent": _agent_record()},
        request={"tenant_id": "t1", "user_id": "u1", "role": ""},
    )
    with pytest.raises(HookError, match="tenant_id/role"):
        await hook_authz_recheck(ctx)


async def test_authz_recheck_passes_audience_and_data_class(authz_subclass):
    fake = authz_subclass(allow=True)
    ctx = _recheck_ctx(services={
        "authz": fake,
        "agent": _agent_record(audience=("service_desk_agent", "problem_manager"),
                               data_class="confidential"),
    })
    await hook_authz_recheck(ctx)
    _, resource = fake.calls[0]
    assert resource.audience == ("service_desk_agent", "problem_manager")
    assert resource.data_classification.value == "confidential"


async def test_authz_recheck_threads_abac_attributes(authz_subclass):
    fake = authz_subclass(allow=True)
    ctx = _recheck_ctx(
        services={"authz": fake, "agent": _agent_record()},
        request={
            "tenant_id": "t1", "user_id": "u1",
            "role": "service_desk_agent",
            "attributes": {"clearance": "L3", "region": "us-east"},
        },
    )
    await hook_authz_recheck(ctx)
    principal, _ = fake.calls[0]
    assert principal.attr("clearance") == "L3"
    assert principal.attr("region") == "us-east"


# ── builtin is registered by default ─────────────────────────────────────


def test_builtin_authz_recheck_is_registered_by_default():
    registry = default_hook_registry()
    assert "builtin:authz_recheck" in registry.registered
    # state_validate / output_redact still registered (no regression).
    assert "hook_state_validate" in registry.registered
    assert "hook_output_redact" in registry.registered
