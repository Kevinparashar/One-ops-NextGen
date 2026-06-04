"""Chat-turn cache — unit coverage.

Property-based smoke for the cache key + the should_cache filter +
the in-memory backend semantics. The Dragonfly backend is exercised
by integration tests.
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.api.chat_turn_cache import (
    InMemoryChatTurnCache,
    build_cache,
    cache_key,
    should_cache,
)

# ── cache_key ────────────────────────────────────────────────────────

def _k(**overrides):
    base = dict(
        tenant_id="T001", user_id="u_demo",
        role="service_desk_agent", session_id="s1",
        message="summarize PBM0003006",
    )
    base.update(overrides)
    return cache_key(**base)


def test_cache_key_is_deterministic_and_normalised():
    a = _k(message="summarize PBM0003006")
    b = _k(message="  SUMMARIZE   pbm0003006 ")
    assert a == b, "whitespace + case must not change the key"


@pytest.mark.parametrize(("field", "value"), [
    ("tenant_id", "T002"),
    ("user_id", "u_other"),
    ("role", "kb_admin"),
    ("session_id", "s2"),
    ("message", "summarize PBM0003007"),
])
def test_cache_key_changes_when_any_factor_changes(field, value):
    assert _k() != _k(**{field: value}), (
        f"changing {field} must produce a different key — otherwise "
        "tenant / session / role isolation is broken"
    )


def test_cache_key_is_hex_string():
    k = _k()
    assert isinstance(k, str)
    assert len(k) == 32
    int(k, 16)  # raises if not hex


# ── should_cache ─────────────────────────────────────────────────────

def test_should_cache_executed_useful_response():
    assert should_cache({
        "final_status": "executed",
        "final_response": "Status: open. Priority: high.",
    })


@pytest.mark.parametrize("status", ["clarification", "refused", "error", ""])
def test_should_cache_skips_non_executed_status(status):
    assert not should_cache({
        "final_status": status,
        "final_response": "some text long enough to pass length gate",
    })


def test_should_cache_skips_empty_or_tiny():
    assert not should_cache({"final_status": "executed", "final_response": ""})
    assert not should_cache({"final_status": "executed", "final_response": "ok"})


def test_should_cache_skips_out_of_scope_refusal_strings():
    assert not should_cache({
        "final_status": "executed",
        "final_response": "That request is out of my scope.",
    })


# ── InMemoryChatTurnCache ────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_inmemory_round_trip_returns_a_copy():
    c = InMemoryChatTurnCache(ttl_seconds=60)
    payload = {"final_response": "x", "n": 1}
    _run(c.put(tenant_id="T001", key="k1", value=payload))
    got = _run(c.get(tenant_id="T001", key="k1"))
    assert got == payload
    got["mutated"] = True  # mutating the returned dict must not poison the cache
    again = _run(c.get(tenant_id="T001", key="k1"))
    assert "mutated" not in again


def test_inmemory_tenant_isolation():
    c = InMemoryChatTurnCache(ttl_seconds=60)
    _run(c.put(tenant_id="T001", key="k1", value={"x": 1}))
    assert _run(c.get(tenant_id="T002", key="k1")) is None


def test_inmemory_ttl_expiry(monkeypatch):
    """Walk the wall clock forward so the row reads as expired."""
    import oneops.api.chat_turn_cache as mod

    fake_now = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: fake_now[0])

    c = InMemoryChatTurnCache(ttl_seconds=10)
    _run(c.put(tenant_id="T001", key="k1", value={"x": 1}))
    assert _run(c.get(tenant_id="T001", key="k1")) == {"x": 1}
    fake_now[0] += 11
    assert _run(c.get(tenant_id="T001", key="k1")) is None


# ── build_cache ──────────────────────────────────────────────────────

def test_build_cache_returns_memory_when_requested(monkeypatch):
    monkeypatch.setenv("CHAT_TURN_CACHE_BACKEND", "memory")
    c = build_cache(ttl_seconds=5)
    assert isinstance(c, InMemoryChatTurnCache)


def test_build_cache_falls_back_to_memory_when_dragonfly_unreachable(
    monkeypatch,
):
    monkeypatch.setenv("CHAT_TURN_CACHE_BACKEND", "dragonfly")
    monkeypatch.setenv("DRAGONFLY_URL", "redis://127.0.0.1:1/0")  # closed port

    # Force from_settings to raise, simulating Dragonfly outage on boot.
    import oneops.api.chat_turn_cache as mod

    def _boom(**_):
        raise RuntimeError("dragonfly down")

    monkeypatch.setattr(mod.DragonflyChatTurnCache, "from_settings",
                        classmethod(lambda cls, **kw: _boom(**kw)))
    c = build_cache(ttl_seconds=5)
    assert isinstance(c, InMemoryChatTurnCache), (
        "must degrade gracefully so the chat path keeps working"
    )


# ── Pipeline version stamp — invalidates ALL chat-cache entries on bump ─

def test_cache_key_changes_when_pipeline_version_changes(monkeypatch):
    """Bumping `PIPELINE_CACHE_VERSION` must produce a different key for
    the SAME (tenant, user, role, session, message) — that's how a render-
    rule change (e.g. _HIDDEN filter) auto-invalidates every cached chat
    response without a manual flush. Mirrors UC-1's HUMANISE_RECORD_VERSION
    invariant."""

    import oneops.api.cache_version as cv

    monkeypatch.setattr(cv, "PIPELINE_CACHE_VERSION", "v1")
    a = cache_key(tenant_id="T001", user_id="u", role="r",
                  session_id="s", message="m")
    monkeypatch.setattr(cv, "PIPELINE_CACHE_VERSION", "v2")
    b = cache_key(tenant_id="T001", user_id="u", role="r",
                  session_id="s", message="m")
    monkeypatch.setattr(cv, "PIPELINE_CACHE_VERSION", "v9")
    c = cache_key(tenant_id="T001", user_id="u", role="r",
                  session_id="s", message="m")
    assert a != b != c, (
        "PIPELINE_CACHE_VERSION must enter the cache key — otherwise stale "
        "rendered responses with leaked fields would still be served after "
        "the renderer is fixed."
    )


def test_current_pipeline_version_is_at_least_v2():
    """The 2026-05-30 leak fix is v2. Reverting reopens the leak window;
    this guard makes that fail visibly."""
    from oneops.api.cache_version import PIPELINE_CACHE_VERSION
    assert PIPELINE_CACHE_VERSION >= "v2"


# ── Production-grade default TTL guard ──────────────────────────────────
#
# 2026-05-30 fix: default was 90s which expired before realistic chat
# follow-ups landed, forcing the second identical call to pay full
# pipeline latency (~4 sec). Bumped to 600s (10 min). This test locks
# the default so a future "let me shorten it back" edit fails visibly.

def test_default_ttl_is_at_least_ten_minutes():
    import oneops.api.chat_turn_cache as m
    assert m._DEFAULT_TTL_S >= 600, (
        "Chat-turn cache TTL must be at least 600s (10 min). Shorter "
        "values mean realistic follow-ups miss the cache and pay full "
        "pipeline latency — the 2026-05-30 user-visible regression."
    )


def test_inmemory_default_ttl_matches_module_default():
    from oneops.api.chat_turn_cache import _DEFAULT_TTL_S, InMemoryChatTurnCache
    c = InMemoryChatTurnCache()
    # `_ttl` is the field exposed for the boot-log emit.
    assert c._ttl == _DEFAULT_TTL_S


def test_env_override_still_wins(monkeypatch):
    """Operators must be able to tune without code changes."""
    monkeypatch.setenv("CHAT_TURN_CACHE_TTL_S", "1200")
    monkeypatch.setenv("CHAT_TURN_CACHE_BACKEND", "memory")
    c = build_cache()
    assert c._ttl == 1200
