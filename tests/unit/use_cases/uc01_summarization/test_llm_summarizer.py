"""UC-1 LLM summariser factory — verifies the gateway → response contract.

Builds a `SummarizeFn` against a stub gateway and asserts:
  * The response shape is `{summary, key_details, model, usage}`.
  * `key_details` is the humanised record (deterministic, no LLM dep).
  * Strict-JSON output is parsed; plain-text fallback also works.
  * Gateway failures propagate (loud, never silent).
  * The system prefix is the SAME across every call (prompt-cache-friendly).
"""
from __future__ import annotations

import pytest

from oneops.llm.models import LlmResponse
from oneops.use_cases.uc01_summarization.llm_summarizer import build_summarize_fn


class _StubGateway:
    """Captures every request and returns a scripted response. The fixture
    asserts what the SummarizeFn handed to the gateway."""

    def __init__(self, content: str, *, fail: bool = False):
        self._content = content
        self._fail = fail
        self.calls: list = []

    async def call(self, request):
        self.calls.append(request)
        if self._fail:
            from oneops.errors import LLMGatewayError
            raise LLMGatewayError("simulated gateway failure")
        return LlmResponse(
            content=self._content,
            model=request.model,
            prompt_tokens=120,
            completion_tokens=80,
            cost_usd=0.0002,
            latency_ms=240,
        )


# ── happy path ───────────────────────────────────────────────────────────


async def test_summary_response_carries_summary_and_key_details():
    gw = _StubGateway('{"summary": "VPN tunnel resets repeatedly."}')
    fn = build_summarize_fn(gw, model="gpt-4o-mini")

    record = {
        "incident_id": "INC0001001",
        "title": "VPN drops",
        "status": "in_progress",
        "priority": "P2",
        "assignment_group": "GRP-NETOPS",
    }
    out = await fn(record, tenant_id="T001", model_override="")
    assert out["summary"] == "VPN tunnel resets repeatedly."
    # key_details is the humanised projection — labels, not snake_case.
    assert out["key_details"]["Incident ID"] == "INC0001001"
    # Title is intentionally hidden from key_details — it lives in the
    # Summary paragraph (avoids double-printing the long-form fields).
    assert "Title" not in out["key_details"]
    # Canonical label is "Assignment Group" per the user-facing spec.
    assert out["key_details"]["Assignment Group"] == "GRP-NETOPS"
    # usage carries through unchanged.
    assert out["usage"]["prompt_tokens"] == 120
    assert out["usage"]["completion_tokens"] == 80
    assert out["model"] == "gpt-4o-mini"


async def test_summary_uses_model_override_when_supplied():
    gw = _StubGateway('{"summary": "ok"}')
    fn = build_summarize_fn(gw, model="default-model")
    await fn({"incident_id": "INC0001001"},
             tenant_id="T001", model_override="bigger-model")
    assert gw.calls[0].model == "bigger-model"


# ── plain-text fallback — providers that ignore response_format ─────────


async def test_summary_handles_plain_text_response():
    gw = _StubGateway("Plain text not JSON. Two sentences here.")
    fn = build_summarize_fn(gw)
    out = await fn({"incident_id": "INC0001001", "status": "open"},
                   tenant_id="T001", model_override="")
    assert out["summary"] == "Plain text not JSON. Two sentences here."


async def test_summary_handles_json_with_extra_whitespace():
    gw = _StubGateway('   {"summary": "trimmed"}   ')
    fn = build_summarize_fn(gw)
    out = await fn({"incident_id": "INC0001001"},
                   tenant_id="T001", model_override="")
    assert out["summary"] == "trimmed"


# ── system-prefix stability (prompt-cache-friendly) ─────────────────────


async def test_system_prompt_uses_platform_policy_compose():
    """Per [[feedback_policy_layer_mandatory]]: every LLM call goes through
    `compose(Profile.X, ...)`. UC-1 uses `FEATURE_AGENT_JSON` + a UC-specific
    `extra_sections` block. Verify the composed prompt contains both pieces
    (platform policy + UC extras) — never a hand-crafted prefix."""
    gw = _StubGateway('{"summary": "ok"}')
    fn = build_summarize_fn(gw)
    await fn({"incident_id": "INC0001001"},
             tenant_id="T001", model_override="")
    system = gw.calls[0].messages[0].content
    # Platform policy blocks present (any of the canonical block titles).
    assert ("COMMON_SAFETY_RULES" in system
            or "## Common Safety Rules" in system
            or "## Output Schema" in system
            or "Registry Grounding" in system)
    # UC-1's own extras land at the tail.
    assert "UC-1 Summary Rules" in system
    # System block is the FIRST message; never user/assistant first.
    assert gw.calls[0].messages[0].role == "system"


async def test_system_prompt_is_byte_identical_for_identical_context():
    """The composer's static-portion + same context => byte-identical
    prefix across calls. Prompt-cache invariant: provider's cache fires."""
    gw = _StubGateway('{"summary": "ok"}')
    fn = build_summarize_fn(gw)
    record = {"incident_id": "INC0001001", "title": "VPN drops"}
    await fn(record, tenant_id="T001", model_override="")
    await fn(record, tenant_id="T001", model_override="")
    assert gw.calls[0].messages[0].content == gw.calls[1].messages[0].content


async def test_system_prompt_tenant_isolated():
    """Tenant id is interpolated into the policy template — two tenants
    see DIFFERENT system prompts. This is the tenant-stamping invariant
    the policy layer provides ([[feedback_policy_layer_mandatory]])."""
    gw = _StubGateway('{"summary": "ok"}')
    fn = build_summarize_fn(gw)
    await fn({"incident_id": "INC0001001"},
             tenant_id="T001", model_override="")
    await fn({"incident_id": "INC0001001"},
             tenant_id="T002", model_override="")
    a = gw.calls[0].messages[0].content
    b = gw.calls[1].messages[0].content
    # The tenant context section differs; the rest is the same shape.
    assert a != b
    # Each prompt names its own tenant.
    assert "T001" in a
    assert "T002" in b


# ── gateway failures propagate loudly ──────────────────────────────────


async def test_gateway_failure_propagates_to_caller():
    from oneops.errors import LLMGatewayError
    gw = _StubGateway("ignored", fail=True)
    fn = build_summarize_fn(gw)
    with pytest.raises(LLMGatewayError, match="simulated"):
        await fn({"incident_id": "INC0001001"},
                 tenant_id="T001", model_override="")


# ── tenant-scoped — every request stamps the right tenant_id ────────────


async def test_request_carries_envelope_tenant_id():
    gw = _StubGateway('{"summary": "ok"}')
    fn = build_summarize_fn(gw)
    await fn({"incident_id": "INC0001001"},
             tenant_id="tenant-a", model_override="")
    await fn({"incident_id": "INC0001002"},
             tenant_id="tenant-b", model_override="")
    assert gw.calls[0].tenant_id == "tenant-a"
    assert gw.calls[1].tenant_id == "tenant-b"
