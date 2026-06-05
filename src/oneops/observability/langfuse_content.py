"""Langfuse content helpers — REDACTED content on OTel spans for Langfuse.

A tracing tool only shows what you feed it: plain OTel spans carry timing, not
content. These helpers attach the prompt/response, agent input/output, and
routing "why" to spans using the attribute keys Langfuse maps (gen_ai.* for
generations; langfuse.observation.* / langfuse.trace.* for spans/traces) — but
ONLY after dual-layer redaction.

TWO INDEPENDENT, AUDITABLE SWITCHES (never overloaded):
  • OTEL_CAPTURE_TEXT       — raw text on spans (pre-existing; default off; see
    safe_attrs). This module does NOT read it.
  • LANGFUSE_CAPTURE_CONTENT — REDACTED content for Langfuse (this module).
Non-content signals that are never PII (model, token counts, cost, tenant_id /
request_id metadata, the observation TYPE) are emitted regardless of the content
flag, so Langfuse still renders the generation/graph structure with content off.

DUAL-LAYER REDACTION applied to every value before it reaches a span:
  (a) RBAC field-policy — drop the VALUE of any dict key whose data
      classification is at/above the withhold threshold (FieldPolicy.is_exposable
      → confidential/restricted), and blank the internal-content arrays
      (work_notes / comments / timeline) which are free-text staff notes.
  (b) PII patterns — redact_text() over every remaining string (emails, phones,
      ids, …).

Never raises — an observability failure must not break business code (§2.7).
"""
from __future__ import annotations

import os
from typing import Any

from oneops.observability.safe_attrs import safe_json_attr

# Free-text staff-note arrays (field_policy.json `internal_content.arrays`) — the
# highest-risk unstructured content; blanked wholesale in traces.
_INTERNAL_CONTENT_ARRAYS = frozenset({"work_notes", "comments", "timeline"})
_MAX_DEPTH = 6
_CONTENT_MAX_LEN = 8192


def langfuse_capture_content_enabled() -> bool:
    """Independent of OTEL_CAPTURE_TEXT. Read each call so tests can flip it."""
    return os.environ.get("LANGFUSE_CAPTURE_CONTENT", "").strip().lower() == "true"


def _field_policy() -> Any | None:
    try:
        from oneops.use_cases._shared.field_policy import get_field_policy
        return get_field_policy()
    except Exception:
        return None


def _redact_str(text: str) -> str:
    # Lazy import: oneops.llm.* imports observability, so a top-level import here
    # would create a cycle (observability ← langfuse_content ← llm ← observability).
    try:
        from oneops.llm.redaction import redact_text
        clean, _found = redact_text(text)
        return clean
    except Exception:
        return ""


def _redact_value(value: Any, policy: Any | None, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "[REDACTED_DEPTH]"
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, dict):
        return _redact_dict(value, policy, depth)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, policy, depth + 1) for v in value]
    # int/float/bool/None — not PII-bearing; pass through.
    return value


def _redact_dict(d: dict[str, Any], policy: Any | None, depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in d.items():
        k = str(key)
        # (a-i) blank internal-content free-text arrays wholesale.
        if k in _INTERNAL_CONTENT_ARRAYS:
            out[k] = f"[REDACTED_INTERNAL_CONTENT:{k}]"
            continue
        # (a-ii) drop values of confidential/restricted fields by classification.
        if policy is not None:
            try:
                if not policy.is_exposable(k):
                    out[k] = f"[REDACTED_{policy.classification_of(k).upper()}]"
                    continue
            except Exception:
                pass
        # (b) recurse + PII-scrub.
        out[k] = _redact_value(val, policy, depth + 1)
    return out


def redact_for_span(value: Any) -> str:
    """Dual-layer redact `value` then JSON-stringify (bounded). Never raises."""
    try:
        red = _redact_value(value, _field_policy(), 0)
        return safe_json_attr(red, max_len=_CONTENT_MAX_LEN)
    except Exception:
        return ""


def _set(span: Any, key: str, value: Any) -> None:
    if span is None or not hasattr(span, "set_attribute") or value is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception:
        pass


def set_langfuse_generation(
    span: Any,
    *,
    model: str | None,
    prompt: Any = None,
    completion: Any = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Mark a span as an LLM generation for Langfuse. Model/tokens/cost are
    emitted always (not PII); prompt/completion only when the content flag is on,
    dual-layer redacted."""
    _set(span, "langfuse.observation.type", "generation")
    _set(span, "gen_ai.request.model", model)
    _set(span, "gen_ai.usage.input_tokens", input_tokens)
    _set(span, "gen_ai.usage.output_tokens", output_tokens)
    _set(span, "gen_ai.usage.cost", cost_usd)
    if langfuse_capture_content_enabled():
        if prompt is not None:
            _set(span, "gen_ai.prompt", redact_for_span(prompt))
        if completion is not None:
            _set(span, "gen_ai.completion", redact_for_span(completion))


def set_langfuse_io(
    span: Any,
    *,
    input: Any = None,
    output: Any = None,
    observation_type: str = "span",
) -> None:
    """Attach redacted input/output to a generic (non-LLM) span — router stages,
    agent steps, tools. Content only when the flag is on."""
    _set(span, "langfuse.observation.type", observation_type)
    if langfuse_capture_content_enabled():
        if input is not None:
            _set(span, "langfuse.observation.input", redact_for_span(input))
        if output is not None:
            _set(span, "langfuse.observation.output", redact_for_span(output))


def set_langfuse_trace(
    span: Any,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    name: str | None = None,
    input: Any = None,
) -> None:
    """Set trace-level Langfuse attributes on the root span. tenant_id/request_id
    are REQUIRED dimensions (§2.6) and always set as trace metadata (not content);
    the user query `input` is content (flag-gated + redacted)."""
    _set(span, "langfuse.trace.name", name)
    _set(span, "user.id", user_id)
    _set(span, "session.id", session_id)
    _set(span, "langfuse.trace.metadata.tenant_id", tenant_id)
    _set(span, "langfuse.trace.metadata.request_id", request_id)
    if input is not None and langfuse_capture_content_enabled():
        _set(span, "langfuse.trace.input", redact_for_span(input))


__all__ = [
    "langfuse_capture_content_enabled",
    "redact_for_span",
    "set_langfuse_generation",
    "set_langfuse_io",
    "set_langfuse_trace",
]
