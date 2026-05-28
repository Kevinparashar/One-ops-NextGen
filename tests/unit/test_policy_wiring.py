"""Policy-layer wiring guard (Component Spec C15).

Every LLM call in the new system must carry the composed policy. These tests
run each LLM-driven component with a capturing fake gateway and assert the
system prompt contains the policy text — so a future component that forgets
`compose()` fails CI rather than silently shipping an unguarded LLM call.

Also covers the static-composition cache (latency + provider prompt-cache).
"""
from __future__ import annotations

import pytest

from oneops.executor.boundary import LlmBoundaryResponder
from oneops.policy.composer import _STATIC_CACHE, Profile, compose
from oneops.router.decompose import LlmDecomposer
from oneops.router.disambiguation import LlmDisambiguator
from oneops.router.retrieval import Candidate
from oneops.router.rewrite import LlmRewriter

# A distinctive phrase from COMMON_SAFETY_RULES — present in every profile.
_POLICY_MARKER = "Never invent or fabricate"


class _CapturedResponse:
    content = "{}"


class _CapturingGateway:
    """Fake LLM gateway — records the request, returns a benign response."""

    def __init__(self) -> None:
        self.request = None

    async def call(self, request):
        self.request = request
        return _CapturedResponse()


def _system_prompt(gateway: _CapturingGateway) -> str:
    msg = gateway.request.messages[0]
    assert msg.role == "system", f"first message is {msg.role}, not system"
    return msg.content


# ── every LLM component carries the policy layer ─────────────────────────


async def test_decomposer_llm_call_carries_policy():
    gw = _CapturingGateway()
    await LlmDecomposer(gw).decompose("show my incidents", request_ctx={})
    sp = _system_prompt(gw)
    assert _POLICY_MARKER in sp                  # policy blocks present
    assert "sub-quer" in sp                      # task instruction still present


async def test_disambiguator_llm_call_carries_policy():
    gw = _CapturingGateway()
    await LlmDisambiguator(gw).disambiguate(
        "summarize it", [Candidate(agent_id="uc01", score=0.9)], request_ctx={})
    sp = _system_prompt(gw)
    assert _POLICY_MARKER in sp
    assert "route an ITSM query" in sp


async def test_rewriter_llm_call_carries_policy():
    gw = _CapturingGateway()
    await LlmRewriter(gw).rewrite("close it", history=[], request_ctx={})
    sp = _system_prompt(gw)
    assert _POLICY_MARKER in sp
    assert "Implicit-Reference Resolution" in sp     # rewriter prompt section


async def test_boundary_responder_llm_call_carries_policy():
    gw = _CapturingGateway()
    await LlmBoundaryResponder(gw).respond(
        outcome="no_confident_match", reason="", request={"message": "hi"})
    sp = _system_prompt(gw)
    assert _POLICY_MARKER in sp
    assert "ITSM/ITOM" in sp                            # updated boundary prompt


# ── static-composition cache (latency + token cost) ──────────────────────


def test_static_composition_is_cached():
    _STATIC_CACHE.clear()
    key = (Profile.INTERNAL_AGENT.value, ("CACHE-PROBE",))
    assert key not in _STATIC_CACHE
    first = compose(Profile.INTERNAL_AGENT, extra_sections=["CACHE-PROBE"])
    assert key in _STATIC_CACHE                  # cached after first call
    second = compose(Profile.INTERNAL_AGENT, extra_sections=["CACHE-PROBE"])
    assert first == second
    assert second is _STATIC_CACHE[key]          # served from cache


def test_static_composition_is_byte_identical_across_calls():
    # A byte-identical prefix is what lets the provider's prompt cache apply.
    a = compose(Profile.PLATFORM_SYSTEM, extra_sections=["X"])
    b = compose(Profile.PLATFORM_SYSTEM, extra_sections=["X"])
    assert a == b


def test_per_request_context_is_not_cached():
    # A call with runtime context varies per request — must not be cached.
    _STATIC_CACHE.clear()
    compose(Profile.FEATURE_AGENT_WITH_TOOLS,
            context={"message": "one", "request_id": "r1"})
    compose(Profile.FEATURE_AGENT_WITH_TOOLS,
            context={"message": "two", "request_id": "r2"})
    assert _STATIC_CACHE == {}                   # nothing cached for dynamic calls
