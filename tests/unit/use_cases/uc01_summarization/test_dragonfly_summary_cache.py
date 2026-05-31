"""DragonflySummaryCacheStore — production cache backend over a Redis-protocol
client. Verified with a fake client; the env-gated integration suite covers
the real Dragonfly cluster.
"""
from __future__ import annotations

import json

import pytest

from oneops.use_cases.uc01_summarization.cache import (
    DragonflySummaryCacheStore,
    _dragonfly_key,
)


class _FakeRedis:
    """Minimal async Redis stand-in. Records every call; honours TTL by
    keeping it on the value entry (no clock movement here)."""

    def __init__(self):
        self._kv: dict[str, tuple[int, bytes]] = {}
        self.calls: list = []

    async def get(self, key: str) -> bytes | None:
        self.calls.append(("get", key))
        v = self._kv.get(key)
        return v[1] if v is not None else None

    async def setex(self, key: str, ttl: int, value):
        self.calls.append(("setex", key, ttl, value))
        if isinstance(value, str):
            value = value.encode("utf-8")
        self._kv[key] = (ttl, value)


@pytest.fixture
def redis():
    return _FakeRedis()


@pytest.fixture
def store(redis):
    return DragonflySummaryCacheStore(redis, ttl_seconds=600)


# ── key shape — tenant-prefixed so it can never cross tenants ──────────


def test_key_shape_is_tenant_prefixed():
    assert _dragonfly_key("T001", "abc123") == "oneops:uc01:summary:T001:abc123"
    assert _dragonfly_key("T002", "abc123") == "oneops:uc01:summary:T002:abc123"


def test_key_refuses_empty_inputs():
    with pytest.raises(ValueError):
        _dragonfly_key("", "abc")
    with pytest.raises(ValueError):
        _dragonfly_key("T001", "")


# ── round-trip: put then get ───────────────────────────────────────────


async def test_put_then_get_round_trip(store, redis):
    summary = {"summary": "VPN drops repeatedly.",
               "key_details": {"Status": "open"},
               "model": "gpt-4o-mini", "usage": {}}
    await store.put(fingerprint="fp1", tenant_id="T001", summary=summary)
    out = await store.get(fingerprint="fp1", tenant_id="T001")
    assert out is not None
    assert out["summary"] == summary
    assert "cached_at" in out


async def test_setex_carries_the_configured_ttl(redis):
    s = DragonflySummaryCacheStore(redis, ttl_seconds=42)
    await s.put(fingerprint="fp1", tenant_id="T001",
                summary={"summary": "x"})
    # Inspect the setex call.
    setex_call = next(c for c in redis.calls if c[0] == "setex")
    assert setex_call[2] == 42


# ── tenant isolation — same fingerprint, different tenants → no leak ──


async def test_tenant_isolated_by_key_prefix(store, redis):
    await store.put(fingerprint="fp1", tenant_id="T001",
                    summary={"summary": "tenant-A"})
    # T002 reading the same fingerprint must see NOTHING.
    other = await store.get(fingerprint="fp1", tenant_id="T002")
    assert other is None
    # T001 of course still sees its own.
    own = await store.get(fingerprint="fp1", tenant_id="T001")
    assert own is not None
    assert own["summary"]["summary"] == "tenant-A"


# ── miss returns None (the cache-aside wrapper interprets as miss) ─────


async def test_miss_returns_none(store):
    assert await store.get(fingerprint="never_stored", tenant_id="T001") is None


async def test_empty_args_return_none_not_raise(store):
    assert await store.get(fingerprint="", tenant_id="T001") is None
    assert await store.get(fingerprint="fp", tenant_id="") is None


# ── put refuses missing tenant / fingerprint (loud, never silent) ──────


async def test_put_refuses_empty_tenant(store):
    with pytest.raises(ValueError, match="tenant_id"):
        await store.put(fingerprint="fp1", tenant_id="",
                        summary={"summary": "x"})


async def test_put_refuses_empty_fingerprint(store):
    with pytest.raises(ValueError, match="fingerprint"):
        await store.put(fingerprint="", tenant_id="T001",
                        summary={"summary": "x"})


# ── JSON encoding handles non-string values without crashing ──────────


async def test_put_handles_non_string_values_via_default_str(store, redis):
    import datetime as dt
    # A datetime object would normally not be JSON-serialisable; the store
    # uses `default=str` so it round-trips as ISO text.
    await store.put(fingerprint="fp1", tenant_id="T001", summary={
        "summary": "ok",
        "issued_at": dt.datetime(2026, 5, 26, 12, 0, 0),
    })
    out = await store.get(fingerprint="fp1", tenant_id="T001")
    assert out is not None
    assert isinstance(out["summary"]["issued_at"], str)


# ── bytes-or-str response handling (depends on client decode_responses) ─


async def test_get_handles_bytes_response_from_client():
    # Some Redis clients return raw bytes; the store decodes them.
    class _BytesRedis:
        async def get(self, key):
            return json.dumps({"summary": {"summary": "bytes-path"},
                               "cached_at": 1.0}).encode("utf-8")
        async def setex(self, key, ttl, value): pass

    store = DragonflySummaryCacheStore(_BytesRedis(), ttl_seconds=600)  # type: ignore[arg-type]
    out = await store.get(fingerprint="fp1", tenant_id="T001")
    assert out is not None
    assert out["summary"]["summary"] == "bytes-path"
