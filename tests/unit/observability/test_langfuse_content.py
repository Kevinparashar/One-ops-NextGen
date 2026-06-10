"""Langfuse content redaction — the PII hard-gate.

Proves dual-layer redaction before content reaches a span:
  (a) RBAC field-policy strips confidential/restricted field VALUES + blanks
      internal-content arrays, and
  (b) PII patterns scrub emails/phones/etc.
Plus the independent LANGFUSE_CAPTURE_CONTENT switch and the always-on
non-content dimensions (model/tokens/cost, tenant_id/request_id).
"""
from __future__ import annotations

import json

import pytest

from oneops.observability import langfuse_content as lc
from oneops.use_cases._shared.field_policy import FieldPolicy, set_field_policy


class _Span:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, k: str, v: object) -> None:
        self.attrs[k] = v


@pytest.fixture
def content_on(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "true")


@pytest.fixture
def content_off(monkeypatch):
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)


@pytest.fixture
def policy():
    """A policy with a confidential + restricted field, restored after."""
    pol = FieldPolicy(
        default_classification="internal",
        withhold_at_or_above="confidential",
        classifications={"tenant_id": "restricted", "salary": "confidential",
                         "title": "internal"},
    )
    set_field_policy(pol)
    yield pol
    set_field_policy(None)  # reset to default loader


# ── layer (a): RBAC field-policy ────────────────────────────────────────────


def test_restricted_and_confidential_field_values_are_stripped(content_on, policy):
    out = lc.redact_for_span(
        {"title": "VPN down", "tenant_id": "T001", "salary": 999999})
    d = json.loads(out)
    assert d["title"] == "VPN down"                       # internal → kept
    assert d["tenant_id"] == "[REDACTED_RESTRICTED]"      # restricted → stripped
    assert d["salary"] == "[REDACTED_CONFIDENTIAL]"       # confidential → stripped
    assert "999999" not in out and "T001" not in out


def test_internal_content_arrays_are_blanked(content_on, policy):
    out = lc.redact_for_span(
        {"work_notes": [{"text": "secret staff note"}],
         "comments": ["internal chatter"], "title": "ok"})
    assert "secret staff note" not in out
    assert "internal chatter" not in out
    assert "REDACTED_INTERNAL_CONTENT" in out
    assert json.loads(out)["title"] == "ok"


# ── layer (b): PII patterns ─────────────────────────────────────────────────


def test_pii_in_string_values_is_redacted(content_on, policy):
    out = lc.redact_for_span(
        {"description": "call me at john.doe@acme.com or 555-123-4567"})
    assert "john.doe@acme.com" not in out
    assert "555-123-4567" not in out
    assert "REDACTED" in out


def test_pii_redacted_in_nested_and_list_values(content_on, policy):
    out = lc.redact_for_span(
        {"steps": [{"note": "email admin@corp.io"}]})
    assert "admin@corp.io" not in out


# ── the content flag gates raw content, not structure ───────────────────────


def test_generation_emits_structure_without_content_when_flag_off(content_off):
    sp = _Span()
    lc.set_langfuse_generation(
        sp, model="gpt-4o", prompt=[{"role": "user", "content": "x@y.com"}],
        completion="hello", input_tokens=10, output_tokens=5, cost_usd=0.01)
    assert sp.attrs["langfuse.observation.type"] == "generation"
    assert sp.attrs["gen_ai.request.model"] == "gpt-4o"
    assert sp.attrs["gen_ai.usage.input_tokens"] == 10
    assert sp.attrs["gen_ai.usage.cost"] == 0.01
    # content NOT emitted when flag off (neither gen_ai.* nor native keys)
    assert "gen_ai.prompt" not in sp.attrs
    assert "gen_ai.completion" not in sp.attrs
    assert "langfuse.observation.input" not in sp.attrs
    assert "langfuse.observation.output" not in sp.attrs


def test_generation_emits_redacted_content_when_flag_on(content_on, policy):
    sp = _Span()
    lc.set_langfuse_generation(
        sp, model="gpt-4o",
        prompt=[{"role": "user", "content": "ticket from bob@corp.io"}],
        completion="resolved for tenant T001", input_tokens=10, output_tokens=5,
        cost_usd=0.01)
    assert "gen_ai.prompt" in sp.attrs
    assert "bob@corp.io" not in sp.attrs["gen_ai.prompt"]
    assert "REDACTED" in sp.attrs["gen_ai.prompt"]
    # Native Langfuse keys are set too (these are what render the generation's
    # input/output in the UI) — same redacted content as gen_ai.*.
    assert sp.attrs["langfuse.observation.input"] == sp.attrs["gen_ai.prompt"]
    assert sp.attrs["langfuse.observation.output"] == sp.attrs["gen_ai.completion"]
    assert "bob@corp.io" not in sp.attrs["langfuse.observation.input"]


def test_io_content_gated_and_redacted(content_off):
    sp = _Span()
    lc.set_langfuse_io(sp, input={"q": "a@b.com"}, output={"r": "ok"})
    assert sp.attrs["langfuse.observation.type"] == "span"
    assert "langfuse.observation.input" not in sp.attrs   # flag off → no content


# ── trace-level dimensions are ALWAYS present (not content-gated) ────────────


def test_trace_dimensions_present_without_content_flag(content_off):
    sp = _Span()
    lc.set_langfuse_trace(
        sp, tenant_id="T001", user_id="u1", session_id="s1", request_id="r1",
        name="chat", input="summarize INC1")
    assert sp.attrs["langfuse.trace.metadata.tenant_id"] == "T001"
    assert sp.attrs["langfuse.trace.metadata.request_id"] == "r1"
    assert sp.attrs["user.id"] == "u1"
    assert sp.attrs["session.id"] == "s1"
    assert "langfuse.trace.input" not in sp.attrs          # query is content → gated


def test_trace_input_redacted_when_flag_on(content_on, policy):
    sp = _Span()
    lc.set_langfuse_trace(sp, tenant_id="T001", input="my email is z@z.com")
    assert "z@z.com" not in sp.attrs["langfuse.trace.input"]


# ── never raises ────────────────────────────────────────────────────────────


def test_redact_never_raises_on_weird_input(content_on):
    class Weird:
        def __repr__(self): raise RuntimeError("boom")
    assert isinstance(lc.redact_for_span({"x": Weird()}), str)  # no exception


def test_setters_tolerate_none_span(content_on):
    lc.set_langfuse_generation(None, model="m")     # must not raise
    lc.set_langfuse_io(None, input={"a": 1})
    lc.set_langfuse_trace(None, tenant_id="T001")
