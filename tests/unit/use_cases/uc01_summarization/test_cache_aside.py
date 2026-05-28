"""UC-1 cache-aside flow (E3) — `build_cached_summarize_fn`.

Verifies the production contract:
  * First call for a fingerprint = MISS → LLM is invoked → result stored
  * Second call for the SAME fingerprint = HIT → NO LLM call → cached body
  * Different tenant on the same content = MISS (tenant partition)
  * Mutated record on the same entity = MISS (content invalidation)
  * Cache read failure falls through to LLM, never corrupts the answer
  * Cache write failure is non-fatal; LLM result still returned
"""
from __future__ import annotations

import pytest

from oneops.llm.models import LlmResponse
from oneops.use_cases.uc01_summarization.cache import InMemorySummaryCacheStore
from oneops.use_cases.uc01_summarization.llm_summarizer import (
    build_cached_summarize_fn,
)


class _RecordingGateway:
    """Counts every `call(...)` so we can assert MISS triggered LLM and
    HIT did not."""

    def __init__(self):
        self.calls = 0

    async def call(self, request):
        self.calls += 1
        return LlmResponse(
            content='{"summary": "Synthetic summary for ' + request.model + '."}',
            model=request.model,
            prompt_tokens=120, completion_tokens=80,
            cost_usd=0.0002, latency_ms=240,
        )


@pytest.fixture
def store():
    return InMemorySummaryCacheStore()


@pytest.fixture
def gateway():
    return _RecordingGateway()


def _incident_record(*, tenant_id="T001", entity_id="INC0001001", title="VPN drops"):
    return {
        "incident_id": entity_id,
        "tenant_id": tenant_id,                          # already removed by policy; OK
        "title": title,
        "status": "in_progress",
        "priority": "P2",
    }


# ── miss → hit on the SAME (tenant, entity, content) ───────────────────


async def test_first_call_is_miss_second_call_is_hit(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    record = _incident_record()

    # 1st call — MISS, LLM invoked
    out1 = await fn(record, tenant_id="T001", model_override="")
    assert gateway.calls == 1
    assert out1["_cache"]["hit"] is False
    assert out1["summary"]                              # non-empty

    # 2nd call — HIT, LLM NOT invoked
    out2 = await fn(record, tenant_id="T001", model_override="")
    assert gateway.calls == 1, "LLM should not be called on cache hit"
    assert out2["_cache"]["hit"] is True
    assert out2["_cache"]["age_s"] is not None
    # Same paragraph as the first call.
    assert out2["summary"] == out1["summary"]
    # key_details and usage round-trip exactly.
    assert out2["key_details"] == out1["key_details"]


# ── tenant partitioning — cache never crosses tenants ──────────────────


async def test_different_tenant_is_a_miss_even_for_identical_content(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    record = _incident_record()
    await fn(record, tenant_id="T001", model_override="")           # miss, store
    assert gateway.calls == 1

    # Same record, different tenant — separate fingerprint.
    await fn(record, tenant_id="T002", model_override="")
    assert gateway.calls == 2, "tenant-B must NOT see tenant-A's cache"


# ── content invalidation — mutating the row mutates the fingerprint ────


async def test_record_content_change_invalidates_the_cache(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    record_v1 = _incident_record(title="VPN drops")
    record_v2 = _incident_record(title="VPN drops every few minutes")

    await fn(record_v1, tenant_id="T001", model_override="")        # miss
    await fn(record_v1, tenant_id="T001", model_override="")        # hit
    assert gateway.calls == 1
    await fn(record_v2, tenant_id="T001", model_override="")        # miss again
    assert gateway.calls == 2


# ── different entities under the same tenant — independent cache rows ──


async def test_different_entity_is_a_separate_fingerprint(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    await fn(_incident_record(entity_id="INC0001001"),
             tenant_id="T001", model_override="")
    await fn(_incident_record(entity_id="INC0001002"),
             tenant_id="T001", model_override="")
    assert gateway.calls == 2


# ── service partition — same digits across services = different keys ──


async def test_same_digits_different_service_dont_collide(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    incident = {"incident_id": "0001001", "title": "x"}
    request = {"request_id":  "0001001", "title": "x"}
    await fn(incident, tenant_id="T001", model_override="")
    await fn(request,  tenant_id="T001", model_override="")
    assert gateway.calls == 2


# ── no safe key → bypass cache, never persist a wrong row ──────────────


async def test_unrecognised_service_bypasses_the_cache(store, gateway):
    """No safe fingerprint key → bypass. The handler still gets an LLM
    answer; we just don't risk planting a cache entry under a malformed
    key (which would either silently miss forever or collide oddly)."""
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    # No primary-key field → service detection fails → bypass.
    weird_record = {"some_other_id": "xyz", "title": "no canonical PK"}
    out = await fn(weird_record, tenant_id="T001", model_override="")
    assert gateway.calls == 1
    assert out["_cache"]["hit"] is False
    assert out["_cache"]["reason"] == "no_key"
    # Subsequent identical call also bypasses (no stale entry got planted).
    await fn(weird_record, tenant_id="T001", model_override="")
    assert gateway.calls == 2


# ── cache-read failure falls through to LLM, never corrupts the answer ─


async def test_cache_read_failure_falls_through_to_llm(store, gateway):
    class _BrokenStore:
        async def get(self, *, fingerprint, tenant_id):
            raise RuntimeError("dragonfly down")
        async def put(self, *, fingerprint, tenant_id, summary):
            pass
    fn = build_cached_summarize_fn(
        gateway, cache_store=_BrokenStore(), model="m")        # type: ignore[arg-type]
    out = await fn(_incident_record(), tenant_id="T001", model_override="")
    assert gateway.calls == 1
    # The user gets a real summary; cache failure is logged, not surfaced.
    assert out["summary"]
    assert out["_cache"]["hit"] is False


async def test_cache_write_failure_is_nonfatal(store, gateway):
    class _ReadOkWriteBroken:
        async def get(self, *, fingerprint, tenant_id):
            return None                                          # always miss
        async def put(self, *, fingerprint, tenant_id, summary):
            raise RuntimeError("dragonfly write failed")
    fn = build_cached_summarize_fn(
        gateway, cache_store=_ReadOkWriteBroken(), model="m")    # type: ignore[arg-type]
    out = await fn(_incident_record(), tenant_id="T001", model_override="")
    # User got their summary even though write failed.
    assert gateway.calls == 1
    assert out["summary"]


# ── hit response shape is the same shape as the miss response ──────────


async def test_hit_response_shape_matches_miss_response_shape(store, gateway):
    fn = build_cached_summarize_fn(gateway, cache_store=store, model="m")
    record = _incident_record()
    miss = await fn(record, tenant_id="T001", model_override="")
    hit  = await fn(record, tenant_id="T001", model_override="")
    # Every key on the miss side is present on the hit side.
    for k in ("summary", "key_details", "model", "usage", "_cache"):
        assert k in hit, f"hit response missing {k!r}"
        assert k in miss
    # Only `_cache.hit` differs.
    assert miss["_cache"]["hit"] is False
    assert hit["_cache"]["hit"] is True
