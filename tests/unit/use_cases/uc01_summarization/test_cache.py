"""UC-1 summary-cache handlers — Component Spec conformance.

Verifies structured output (C8), tenant isolation (C13), and no-silent-failure
(C17). The cache contract: a miss is an explicit `outcome="miss"`, never a
bare `None`; an invalid request is an explicit `outcome="invalid_request"`,
never an exception.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc01_summarization.cache import (
    InMemorySummaryCacheStore,
    get_cached_summary,
    put_cached_summary,
    set_summary_cache_store,
)

_GET_KEYS = {"outcome", "fingerprint", "message", "summary", "age_s"}
_PUT_KEYS = {"outcome", "fingerprint", "message"}


@pytest.fixture
def store() -> InMemorySummaryCacheStore:
    s = InMemorySummaryCacheStore()
    set_summary_cache_store(s)
    return s


# ── C8 — structured output ────────────────────────────────────────────────


async def test_get_output_has_the_declared_shape(store):
    out = await get_cached_summary({"fingerprint": "fp1"}, {"tenant_id": "t1"})
    assert set(out) == _GET_KEYS
    assert out["outcome"] == "miss"
    assert out["message"]


async def test_put_output_has_the_declared_shape(store):
    out = await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"text": "hi"}},
        {"tenant_id": "t1"})
    assert set(out) == _PUT_KEYS
    assert out["outcome"] == "stored"
    assert out["message"]


# ── round-trip + idempotent put ──────────────────────────────────────────


async def test_put_then_get_returns_hit_with_summary(store):
    await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"text": "the summary"}},
        {"tenant_id": "t1"})
    out = await get_cached_summary({"fingerprint": "fp1"}, {"tenant_id": "t1"})
    assert out["outcome"] == "hit"
    assert out["summary"] == {"text": "the summary"}
    assert out["age_s"] is not None
    assert out["age_s"] >= 0


async def test_put_is_idempotent_same_fingerprint_overwrites(store):
    await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"v": 1}}, {"tenant_id": "t1"})
    await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"v": 2}}, {"tenant_id": "t1"})
    out = await get_cached_summary({"fingerprint": "fp1"}, {"tenant_id": "t1"})
    assert out["summary"] == {"v": 2}


# ── C13 — tenant isolation is structural, not advisory ───────────────────


async def test_one_tenants_summary_is_invisible_to_another(store):
    await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"text": "tenant-a's"}},
        {"tenant_id": "tenant-a"})
    out = await get_cached_summary(
        {"fingerprint": "fp1"}, {"tenant_id": "tenant-b"})
    assert out["outcome"] == "miss"
    assert out["summary"] is None


# ── C17 — every bad input is an explicit invalid_request outcome ─────────


async def test_missing_fingerprint_on_get_is_invalid_request(store):
    out = await get_cached_summary({}, {"tenant_id": "t1"})
    assert out["outcome"] == "invalid_request"
    assert out["summary"] is None
    assert out["message"]


async def test_whitespace_fingerprint_on_get_is_invalid_request(store):
    out = await get_cached_summary({"fingerprint": "   "}, {"tenant_id": "t1"})
    assert out["outcome"] == "invalid_request"


async def test_missing_tenant_on_get_is_invalid_request(store):
    out = await get_cached_summary({"fingerprint": "fp1"}, {})
    assert out["outcome"] == "invalid_request"


async def test_missing_fingerprint_on_put_is_invalid_request(store):
    out = await put_cached_summary({"summary": {"v": 1}}, {"tenant_id": "t1"})
    assert out["outcome"] == "invalid_request"


async def test_missing_tenant_on_put_is_invalid_request(store):
    out = await put_cached_summary(
        {"fingerprint": "fp1", "summary": {"v": 1}}, {})
    assert out["outcome"] == "invalid_request"


async def test_empty_summary_on_put_is_invalid_request(store):
    # An empty dict means "I produced nothing" — caching it would mask a real
    # generation failure on the next get-hit. Refuse explicitly.
    out = await put_cached_summary(
        {"fingerprint": "fp1", "summary": {}}, {"tenant_id": "t1"})
    assert out["outcome"] == "invalid_request"


async def test_non_dict_summary_on_put_is_invalid_request(store):
    out = await put_cached_summary(
        {"fingerprint": "fp1", "summary": "raw string"}, {"tenant_id": "t1"})
    assert out["outcome"] == "invalid_request"
