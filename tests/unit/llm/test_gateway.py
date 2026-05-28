"""LlmGateway tests — the single egress: quota, redaction, retry, fallback, cost."""
from __future__ import annotations

import pytest

from oneops.errors import LLMGatewayError, QuotaExceededError
from oneops.llm.cost import CostTracker
from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest
from oneops.llm.quota import QuotaGuard
from oneops.llm.transport import EchoTransport


def _req(content="summarize INC0048213", *, model="gpt-4o-mini", tenant="tenant-a"):
    return LlmRequest(messages=(LlmMessage("user", content),),
                      model=model, tenant_id=tenant)


# ── happy path ───────────────────────────────────────────────────────────


async def test_call_returns_a_response():
    gw = LlmGateway(EchoTransport())
    resp = await gw.call(_req())
    assert "summarize INC0048213" in resp.content
    assert resp.model == "gpt-4o-mini"
    assert resp.total_tokens > 0


# ── redaction ────────────────────────────────────────────────────────────


async def test_prompt_pii_is_redacted_before_the_transport_sees_it():
    gw = LlmGateway(EchoTransport())
    resp = await gw.call(_req("my email is dave@example.com"))
    # EchoTransport echoes what it received — proof the transport saw the
    # scrubbed prompt, not the raw email.
    assert "dave@example.com" not in resp.content
    assert "email" in resp.redacted_pii


async def test_redaction_can_be_disabled():
    gw = LlmGateway(EchoTransport(), redact=False)
    resp = await gw.call(_req("ping 10.0.0.1"))
    assert resp.redacted_pii == ()


# ── cost accounting ──────────────────────────────────────────────────────


async def test_cost_is_recorded_per_tenant_per_model():
    tracker = CostTracker()
    gw = LlmGateway(EchoTransport(), cost_tracker=tracker)
    await gw.call(_req(tenant="tenant-a", model="gpt-4o-mini"))
    await gw.call(_req(tenant="tenant-a", model="gpt-4o"))
    usage = tracker.usage("tenant-a")
    assert set(usage) == {"gpt-4o-mini", "gpt-4o"}
    assert tracker.total_cost("tenant-a") > 0


async def test_response_carries_the_call_cost():
    gw = LlmGateway(EchoTransport())
    resp = await gw.call(_req())
    assert resp.cost_usd > 0


# ── quota ────────────────────────────────────────────────────────────────


async def test_quota_blocks_a_tenant_over_budget():
    gw = LlmGateway(EchoTransport(), quota_guard=QuotaGuard(default_limit=1))
    await gw.call(_req())
    with pytest.raises(QuotaExceededError):
        await gw.call(_req())


# ── retry + fallback ─────────────────────────────────────────────────────


async def test_transient_failures_are_retried():
    # 2 simulated failures, max_retries=2 → 3 attempts, the 3rd succeeds.
    gw = LlmGateway(EchoTransport(fail_times=2), max_retries=2)
    resp = await gw.call(_req())
    assert resp.content                              # eventually succeeded
    assert resp.fell_back is False


async def test_failover_to_the_fallback_model():
    # Primary fails its 2 attempts, then the fallback attempt succeeds.
    gw = LlmGateway(EchoTransport(fail_times=2), max_retries=1,
                    fallback_model="gpt-4o")
    resp = await gw.call(_req(model="gpt-4o-mini"))
    assert resp.fell_back is True
    assert resp.model == "gpt-4o"


async def test_exhausted_retries_raise_gateway_error():
    gw = LlmGateway(EchoTransport(fail_times=99), max_retries=1)
    with pytest.raises(LLMGatewayError, match="after 2 attempt"):
        await gw.call(_req())


# ── embeddings egress ────────────────────────────────────────────────────


async def test_embed_goes_through_the_gateway():
    tracker = CostTracker()
    gw = LlmGateway(EchoTransport(embed_dims=8), cost_tracker=tracker)
    vecs = await gw.embed(["hello", "world"], model="text-embedding-3-large",
                          tenant_id="tenant-a")
    assert len(vecs) == 2 and len(vecs[0]) == 8
    # The same text embeds to the same vector (deterministic transport).
    again = await gw.embed(["hello"], model="text-embedding-3-large",
                           tenant_id="tenant-a")
    assert again[0] == vecs[0]
    # Embedding usage is recorded per tenant per model. A few tokens cost
    # sub-micro-dollar, so assert on the exact usage record, not the
    # micro-dollar-rounded total.
    usage = tracker.usage("tenant-a")["text-embedding-3-large"]
    assert usage["calls"] == 2
    assert usage["prompt_tokens"] > 0
