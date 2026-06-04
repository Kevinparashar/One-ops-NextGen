"""UC-2 per-result discriminator labels.

Closes the perception gap where the top-K results all carry the same
generic `why_similar` tags (same_category / same_service / same_group) and
look "identical" in the UI even though the underlying ranking is precise.

Single batched LLM call per UC-2 request: source + N candidate titles +
first-sentence descriptions → N short, distinct failure-mode labels.

Production guarantees:
  • Single egress through `oneops.llm.gateway` (rule §2.5).
  • Policy header injected via `compose(Profile.INTERNAL_AGENT, ...)` (§2.3).
  • Cache-friendly: temperature=0, deterministic, low max_tokens.
  • Fail-safe: any error returns an empty mapping — the result list still
    renders with no `discriminator` field; never blocks UC-2 output.
  • Span emits `uc02.discriminator.outcome` for operator visibility.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from opentelemetry import trace

from oneops.llm import LlmGateway, LlmMessage, LlmRequest, ResponseFormat
from oneops.policy import Profile, compose

_log = structlog.get_logger(__name__)
_tracer = trace.get_tracer("oneops.uc02.discriminators")


def _trim(s: str | None, n: int) -> str:
    t = (s or "").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _first_sentence(s: str | None) -> str:
    t = (s or "").strip()
    if not t:
        return ""
    # Cheap split: stop at first ".", "?", "!" or 160 chars, whichever first.
    for ch in (". ", "? ", "! "):
        i = t.find(ch)
        if 0 < i < 160:
            return t[: i + 1]
    return t[:160].rstrip() + ("…" if len(t) > 160 else "")


_INSTRUCTIONS = """You label why each candidate ticket is similar to a source ticket.

Goal: when an analyst scans the list, they should be able to tell the
candidates apart at a glance — what *specifically* makes each one similar
(or where it differs) on the failure-mode axis.

Rules:
1. Output exactly ONE short phrase per candidate (≤ 8 words). No sentences.
2. Express the SPECIFIC failure mode / symptom pattern, not the domain.
   - GOOD: "tunnel-establishment failure", "DHCP-driven session loss",
     "client-side error 809", "post-sleep reconnect failure"
   - BAD: "VPN issue", "network problem", "same as source" — generic.
3. Use the candidate's own title + first sentence. Don't invent symptoms.
4. If the candidate truly is the same failure mode as the source, say so
   crisply (e.g. "same root cause: tunnel timeout").
5. Return strictly JSON: {"labels": [{"ticket_id": "...", "label": "..."}]}.
   Preserve the input order. Use the exact ticket_id strings supplied.
"""


async def generate_discriminators(
    *,
    gateway: LlmGateway,
    model: str,
    source_title: str,
    source_desc: str,
    candidates: list[dict[str, Any]],
    tenant_id: str,
    user_id: str = "",
    request_id: str = "",
) -> dict[str, str]:
    """Return `{ticket_id: short_label}` for each candidate, or {} on failure.

    `candidates` items must carry: `ticket_id`, `title`, `description`.
    """
    if not candidates:
        return {}
    with _tracer.start_as_current_span(
        "uc02.discriminator.generate",
        attributes={
            "oneops.uc02.discriminator.count": len(candidates),
        },
    ) as span:
        try:
            system_prompt = compose(
                Profile.INTERNAL_AGENT,
                extra_sections=[_INSTRUCTIONS],
            )
            user_block = json.dumps({
                "source": {
                    "title": _trim(source_title, 180),
                    "first_sentence": _first_sentence(source_desc),
                },
                "candidates": [
                    {
                        "ticket_id": str(c.get("ticket_id") or ""),
                        "title": _trim(c.get("title"), 180),
                        "first_sentence": _first_sentence(c.get("description")),
                    }
                    for c in candidates
                ],
            }, ensure_ascii=False)

            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage("system", system_prompt, cache_control=True),
                    LlmMessage("user", user_block),
                ),
                model=model,
                tenant_id=tenant_id,
                user_id=user_id,
                request_id=request_id,
                response_format=ResponseFormat.JSON,
                temperature=0.0,
                max_tokens=400,
            ))
            doc = json.loads(resp.content or "{}")
            out: dict[str, str] = {}
            for row in (doc.get("labels") or []):
                tid = str(row.get("ticket_id") or "").strip()
                label = _trim(row.get("label"), 80)
                if tid and label:
                    out[tid] = label
            span.set_attribute("uc02.discriminator.outcome",
                               "ok" if out else "empty")
            span.set_attribute("uc02.discriminator.labelled", len(out))
            return out
        except Exception as exc:                                       # noqa: BLE001
            # Fail-safe — the result list still renders without labels.
            _log.warning("uc02.discriminator.failed",
                         error=str(exc)[:200], count=len(candidates))
            span.set_attribute("uc02.discriminator.outcome", "failed")
            span.set_attribute("uc02.discriminator.error", str(exc)[:160])
            return {}


__all__ = ["generate_discriminators"]
