"""Tests for the tool-runner support pieces — resolver, variable store, idempotency."""
from __future__ import annotations

import pytest

from oneops.errors import ToolHandlerError
from oneops.toolrunner.idempotency import InMemoryIdempotencyStore
from oneops.toolrunner.models import ToolResult, VariableRef
from oneops.toolrunner.resolver import HandlerResolver
from oneops.toolrunner.variables import InMemoryVariableStore

# ── HandlerResolver ──────────────────────────────────────────────────────


async def _h(args, ctx):
    return "ok"


def test_resolve_explicitly_registered_handler():
    r = HandlerResolver()
    r.register("pkg:fn", _h)
    assert r.resolve("pkg:fn") is _h


def test_register_duplicate_is_rejected():
    r = HandlerResolver()
    r.register("pkg:fn", _h)
    with pytest.raises(ValueError, match="already registered"):
        r.register("pkg:fn", _h)


def test_resolve_by_import():
    # json.dumps is importable + callable — proves the import path works.
    fn = HandlerResolver().resolve("json:dumps")
    assert fn.__name__ == "dumps"


def test_resolve_unknown_module_raises():
    with pytest.raises(ToolHandlerError, match="could not be imported"):
        HandlerResolver().resolve("no.such.module:fn")


def test_resolve_missing_attribute_raises():
    with pytest.raises(ToolHandlerError, match="no attribute"):
        HandlerResolver().resolve("json:not_a_real_function")


def test_resolve_malformed_ref_raises():
    with pytest.raises(ToolHandlerError, match="module:function"):
        HandlerResolver().resolve("no_colon_here")


def test_resolve_non_callable_raises():
    with pytest.raises(ToolHandlerError, match="not callable"):
        HandlerResolver().resolve("json:__doc__")


# ── InMemoryVariableStore ────────────────────────────────────────────────


def test_small_value_passes_through_unchanged():
    store = InMemoryVariableStore()
    assert store.capture({"a": 1}) == {"a": 1}
    assert store.count == 0


def test_large_value_becomes_a_variable_ref():
    store = InMemoryVariableStore(threshold_bytes=100, preview_chars=20)
    big = "x" * 5000
    ref = store.capture(big, hint="ticket")
    assert isinstance(ref, VariableRef)
    assert ref.size_bytes > 100
    assert len(ref.preview) <= 21              # 20 chars + ellipsis
    assert ref.name.startswith("ticket_")


def test_variable_ref_value_is_retrievable():
    store = InMemoryVariableStore(threshold_bytes=10)
    ref = store.capture("y" * 500)
    assert store.has(ref.name)
    assert store.get(ref.name) == "y" * 500


def test_preview_is_truncated_with_ellipsis():
    store = InMemoryVariableStore(threshold_bytes=10, preview_chars=8)
    ref = store.capture("abcdefghijklmnop")
    assert ref.preview.endswith("…")
    assert len(ref.preview) == 9               # 8 + ellipsis


# ── InMemoryIdempotencyStore ─────────────────────────────────────────────


async def test_idempotency_stores_and_replays_a_success():
    store = InMemoryIdempotencyStore()
    result = ToolResult.success("t1", {"v": 1})
    await store.put("key-1", result, ttl_seconds=60)
    replayed = await store.get("key-1")
    assert replayed is not None
    assert replayed.output == {"v": 1}
    assert replayed.from_idempotency_cache is True   # flagged as a replay


async def test_idempotency_does_not_cache_a_failure():
    store = InMemoryIdempotencyStore()
    await store.put("key-2", ToolResult.failed("t1", "boom"), ttl_seconds=60)
    # A failed attempt stays retryable — nothing is cached.
    assert await store.get("key-2") is None


async def test_idempotency_miss_returns_none():
    assert await InMemoryIdempotencyStore().get("never-seen") is None
