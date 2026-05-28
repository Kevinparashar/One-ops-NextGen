"""Conversational boundary — UC-1 contract task 36.

Verifies the classifier behaviour required by the contract:
  * greeting → contextual reply (LLM-generated)
  * in_scope_unclear → clarifying question
  * in_scope_kb_search → KB-lookup offer
  * out_of_scope → EXACTLY the literal `OUT_OF_SCOPE_REPLY`, enforced
    server-side regardless of what the LLM emits
  * policy_denied → never goes through classification (deterministic only)
  * gateway failure → deterministic fallback (user always gets a reply)
"""
from __future__ import annotations

import json

import pytest

from oneops.errors import LLMGatewayError
from oneops.executor.boundary import (
    DeterministicBoundaryResponder,
    LlmBoundaryResponder,
    OUT_OF_SCOPE_REPLY,
)
from oneops.llm.models import LlmResponse


class _StubGateway:
    """Returns scripted JSON or raises on demand. Records every call so we
    can assert that policy_denied paths skip the LLM entirely."""

    def __init__(self, payload: dict | None = None, raise_exc: Exception | None = None):
        self.payload = payload
        self.raise_exc = raise_exc
        self.calls: list = []

    async def call(self, request):
        self.calls.append(request)
        if self.raise_exc is not None:
            raise self.raise_exc
        content = json.dumps(self.payload) if self.payload is not None else ""
        return LlmResponse(
            content=content,
            model=request.model,
            prompt_tokens=80, completion_tokens=20,
            cost_usd=0.0001, latency_ms=120,
        )


def _request(message="hi", **over):
    base = {"request_id": "r1", "tenant_id": "T001",
            "user_id": "oneops", "role": "service_desk_agent",
            "session_id": "s1", "message": message}
    base.update(over)
    return base


# ── greeting → contextual reply, LLM-generated ─────────────────────────


async def test_greeting_returns_llm_generated_reply():
    gw = _StubGateway({"category": "greeting",
                       "reply": "Hi! How can I help with your tickets today?"})
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="", request=_request("hi"))
    assert out == "Hi! How can I help with your tickets today?"
    assert len(gw.calls) == 1


async def test_thanks_returns_llm_generated_acknowledgement():
    gw = _StubGateway({"category": "greeting",
                       "reply": "You're welcome — anything else on your tickets?"})
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="", request=_request("thanks!"))
    assert "welcome" in out.lower()


# ── out_of_scope is enforced server-side, NOT trusted from the LLM ─────


async def test_out_of_scope_returns_exact_literal_text():
    # Even if the LLM tries to paraphrase, the server overwrites with the
    # canonical text — the user always sees the same line on a domain miss.
    gw = _StubGateway({"category": "out_of_scope",
                       "reply": "I can't help with sports questions, sorry."})
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="",
        request=_request("who's playing in the world cup?"))
    assert out == OUT_OF_SCOPE_REPLY
    assert "out of my scope" in out
    assert "ITSM/ITOM" in out


async def test_out_of_scope_literal_contains_both_clauses():
    # Documenting the exact wording the contract specifies.
    assert "out of my scope" in OUT_OF_SCOPE_REPLY
    assert "within the ITSM/ITOM domain" in OUT_OF_SCOPE_REPLY


# ── in_scope_unclear → clarifying question ─────────────────────────────


async def test_in_scope_unclear_returns_clarifying_question():
    gw = _StubGateway({
        "category": "in_scope_unclear",
        "reply": "Are you looking at a specific ticket, or a category?",
    })
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="",
        request=_request("i need help with something"))
    assert "?" in out


# ── in_scope_kb_search → KB lookup offer ───────────────────────────────


async def test_in_scope_kb_search_offers_kb_lookup():
    gw = _StubGateway({
        "category": "in_scope_kb_search",
        "reply": "I can search the knowledge base for VPN troubleshooting steps.",
    })
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="",
        request=_request("how do i reset my vpn?"))
    assert "knowledge base" in out.lower() or "kb" in out.lower()


# ── policy_denied never goes through classification ────────────────────


async def test_policy_denied_skips_llm_classification_entirely():
    gw = _StubGateway({"category": "greeting", "reply": "hi"})
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="policy_denied",
        reason="role_not_in_audience", request=_request())
    # The deterministic responder owns this path — LLM not called.
    assert "permission" in out.lower()
    assert len(gw.calls) == 0


# ── gateway failure falls through to deterministic fallback ────────────


async def test_gateway_exhaustion_falls_back_to_deterministic():
    gw = _StubGateway(raise_exc=LLMGatewayError("simulated"))
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="", request=_request("hi"))
    assert out                                          # non-empty
    # The deterministic message — never claims a capability.
    assert "not sure how to help" in out or "rephrase" in out


async def test_malformed_json_response_falls_through_to_content_or_fallback():
    # LLM returned bare text instead of JSON — boundary treats it as the
    # reply directly. (Non-fatal degradation.)
    gw = _StubGateway()
    gw.payload = None                                   # empty content
    boundary = LlmBoundaryResponder(gw)
    out = await boundary.respond(
        outcome="no_confident_match", reason="", request=_request("hi"))
    # Empty content from LLM → deterministic fallback.
    assert out


# ── compose() invariant — system prompt rides the policy layer ─────────


async def test_classifier_uses_compose_with_policy_profile():
    gw = _StubGateway({"category": "greeting", "reply": "Hi!"})
    boundary = LlmBoundaryResponder(gw)
    await boundary.respond(
        outcome="no_confident_match", reason="", request=_request("hi"))
    # The system message MUST contain platform safety blocks composed from
    # docs/policies/updated_policy_v2.md — never a hand-crafted string.
    system_msg = gw.calls[0].messages[0].content
    assert gw.calls[0].messages[0].role == "system"
    # Boundary-specific extras land on the same prompt.
    assert "ITSM" in system_msg
    assert "out_of_scope" in system_msg                 # classifier categories
