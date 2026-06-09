"""Streaming egress — `EchoTransport.complete_stream` + `LlmGateway.call_stream`.

The streaming path mirrors `call()` discipline (quota, redaction, cost,
observability) but delivers the answer incrementally and finalises accounting
at stream end. These tests assert that contract hermetically (EchoTransport,
no network).
"""
from __future__ import annotations

from oneops.errors import LLMGatewayError
from oneops.llm.cost import CostTracker
from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest
from oneops.llm.transport import EchoTransport


def _req(content="summarize INC0048213", *, model="gpt-4o-mini", tenant="tenant-a"):
    return LlmRequest(messages=(LlmMessage("user", content),),
                      model=model, tenant_id=tenant)


async def _collect(gw, req):
    """Drain a stream → (joined_deltas, final_response)."""
    deltas, final = [], None
    async for chunk in gw.call_stream(req):
        if chunk.done:
            final = chunk.response
        elif chunk.delta:
            deltas.append(chunk.delta)
    return "".join(deltas), final


# ── transport-level ───────────────────────────────────────────────────────


async def test_echo_transport_streams_deltas_then_final_usage():
    t = EchoTransport(canned="alpha beta gamma")
    deltas, final = [], None
    async for d in t.complete_stream(_req()):
        if d.final:
            final = d
        elif d.text:
            deltas.append(d.text)
    assert "".join(deltas) == "alpha beta gamma"   # reassembles exactly
    assert len(deltas) > 1                          # genuinely incremental
    assert final is not None and final.completion_tokens > 0


# ── gateway happy path ──────────────────────────────────────────────────────


async def test_call_stream_reassembles_content_and_finalises_response():
    gw = LlmGateway(EchoTransport())
    content, final = await _collect(gw, _req())
    assert "summarize INC0048213" in content
    assert final is not None
    assert final.content == content            # final response == joined deltas
    assert final.model == "gpt-4o-mini"
    assert final.total_tokens > 0
    assert final.finish_reason == "stop"


async def test_call_stream_records_cost_at_stream_end():
    tracker = CostTracker()
    gw = LlmGateway(EchoTransport(), cost_tracker=tracker)
    _content, final = await _collect(gw, _req(tenant="tenant-z"))
    assert final.cost_usd > 0.0
    # cost was recorded against the tenant (same accounting as call()):
    # the tenant's running total is now non-zero.
    assert tracker.total_cost("tenant-z") > 0.0


# ── gateway redaction (PII scrubbed before transport sees it) ───────────────


async def test_call_stream_redacts_pii_before_transport():
    gw = LlmGateway(EchoTransport())
    content, final = await _collect(gw, _req("my email is dave@example.com"))
    assert "dave@example.com" not in content        # transport saw scrubbed text
    assert "email" in final.redacted_pii


# ── gateway error path (transport failure → one typed error) ───────────────


async def test_call_stream_failure_is_typed_as_gateway_error():
    gw = LlmGateway(EchoTransport(fail_times=1), max_retries=0)
    raised = False
    try:
        async for _chunk in gw.call_stream(_req()):
            pass
    except LLMGatewayError:
        raised = True
    assert raised
