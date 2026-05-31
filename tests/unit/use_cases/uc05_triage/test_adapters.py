"""Phase 1 tests — gateway-backed UC-5 adapters.

Covers the production wiring: each factory builds an async callable that
goes through LlmGateway (cost, redaction, retries) and composes its prompt
via policy.composer (Profile-locked safety/tenant/JSON blocks).
"""
from __future__ import annotations

import pytest

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import (
    LlmRequest,
    ResponseFormat,
    TransportResult,
)
from oneops.use_cases.uc05_triage.adapters import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_EMBED_MODEL,
    make_embed_fn,
    make_infer_fn,
    make_tag_fn,
    make_tiebreak_fn,
)

# ── Fake transport — captures what the gateway would have sent ──────────────

class _CapturingTransport:
    def __init__(self, *, embed_dim: int = 1536, chat_reply: str = '{"impact":"On Users","urgency":"Medium"}'):
        self.embed_calls: list[tuple[list[str], str, int | None]] = []
        self.chat_calls: list[LlmRequest] = []
        self._embed_dim = embed_dim
        self._chat_reply = chat_reply

    async def embed(self, texts, *, model: str, dimensions: int | None) -> list[list[float]]:
        self.embed_calls.append((list(texts), model, dimensions))
        return [[0.0] * self._embed_dim for _ in texts]

    async def complete(self, req: LlmRequest) -> TransportResult:
        self.chat_calls.append(req)
        return TransportResult(
            content=self._chat_reply,
            prompt_tokens=10, completion_tokens=5,
            actual_model=req.model,
        )


def _gateway(transport: _CapturingTransport) -> LlmGateway:
    return LlmGateway(transport=transport, redact=False)


# ── embed_fn ────────────────────────────────────────────────────────────────

class TestEmbedFn:
    @pytest.mark.asyncio
    async def test_embed_routes_through_gateway(self) -> None:
        t = _CapturingTransport(embed_dim=1536)
        fn = make_embed_fn(_gateway(t), tenant_id="T001", user_id="tech1")
        v = await fn("VPN drops at Mumbai")
        assert len(v) == 1536
        assert len(t.embed_calls) == 1
        texts, model, dim = t.embed_calls[0]
        assert texts == ["VPN drops at Mumbai"]
        assert model == DEFAULT_EMBED_MODEL
        assert dim == 1536


# ── tiebreak_fn ─────────────────────────────────────────────────────────────

class TestTiebreakFn:
    @pytest.mark.asyncio
    async def test_tiebreak_uses_feature_agent_policy(self) -> None:
        t = _CapturingTransport(chat_reply="vpn")
        fn = make_tiebreak_fn(_gateway(t), tenant_id="T001", user_id="tech1")
        choice = await fn(
            probe_text="VPN drops at Mumbai",
            field="subcategory",
            candidates=[
                {"value": "vpn", "vote_count": 2, "example_titles": ["VPN drops after 10 minutes"]},
                {"value": "wifi", "vote_count": 2, "example_titles": ["Office Wi-Fi unreachable"]},
            ],
            ticket_row={"title": "VPN drops", "description": "tunnel keeps dropping"},
        )
        assert choice == "vpn"
        # one chat call hit the gateway
        assert len(t.chat_calls) == 1
        req = t.chat_calls[0]
        assert req.tenant_id == "T001"
        assert req.user_id == "tech1"
        assert req.model == DEFAULT_CHAT_MODEL
        assert req.max_tokens == 30
        # system message exists and contains tiebreak instruction
        sys_msg = req.messages[0].content
        assert "pick the most semantically appropriate value" in sys_msg

    @pytest.mark.asyncio
    async def test_tiebreak_empty_response_returns_none(self) -> None:
        t = _CapturingTransport(chat_reply="")
        fn = make_tiebreak_fn(_gateway(t), tenant_id="T001")
        choice = await fn(
            probe_text="x", field="category",
            candidates=[{"value": "a", "vote_count": 1, "example_titles": []}],
            ticket_row={"title": "x", "description": "x"},
        )
        assert choice is None


# ── tag_fn ──────────────────────────────────────────────────────────────────

class TestTagFn:
    @pytest.mark.asyncio
    async def test_tag_uses_json_profile(self) -> None:
        t = _CapturingTransport(chat_reply='["vpn", "tunnel", "wi-fi"]')
        fn = make_tag_fn(_gateway(t), tenant_id="T001", user_id="tech1")
        tags = await fn(
            probe_title="VPN drops", probe_description="tunnel keeps dropping",
            neighbour_titles=["VPN drops after 10 minutes"],
            neighbour_descriptions=["..."],
            candidate_pool=["vpn", "tunnel", "drops"],
        )
        assert tags == ["vpn", "tunnel", "wi-fi"]
        req = t.chat_calls[0]
        assert req.response_format == ResponseFormat.JSON
        sys_msg = req.messages[0].content
        assert "GOOD tags" in sys_msg
        assert "BAD tags" in sys_msg

    @pytest.mark.asyncio
    async def test_tag_malformed_json_returns_empty(self) -> None:
        t = _CapturingTransport(chat_reply="not a json list")
        fn = make_tag_fn(_gateway(t), tenant_id="T001")
        tags = await fn(
            probe_title="x", probe_description="x",
            neighbour_titles=[], neighbour_descriptions=[],
            candidate_pool=[],
        )
        assert tags == []


# ── infer_fn ────────────────────────────────────────────────────────────────

class TestInferFn:
    @pytest.mark.asyncio
    async def test_infer_returns_motadata_pair(self) -> None:
        t = _CapturingTransport(chat_reply='{"impact":"On Department","urgency":"High"}')
        fn = make_infer_fn(_gateway(t), tenant_id="T001", user_id="tech1")
        out = await fn(
            service_id="incident",
            ticket_row={"title": "VPN drops", "description": "tunnel"},
            suggested_category="network", suggested_subcategory="vpn",
        )
        assert out == {"impact": "On Department", "urgency": "High"}
        req = t.chat_calls[0]
        assert req.response_format == ResponseFormat.JSON
        sys_msg = req.messages[0].content
        assert "IMPACT values" in sys_msg
        assert "URGENCY values" in sys_msg
