"""LLM summariser for the diagnosis_trail chunk.

A ticket's diagnosis trail (work_notes JSON for incidents, comments JSON for
requests) is a noisy chronological log: "tried reboot", "user not in office",
"escalating to L2". For semantic similarity we want a focused summary of
*what diagnostic ground has been covered*, not the raw log.

Output is bounded (≤ 200 words, 3-5 bullets) so the diagnosis_trail content_text
always fits in the embedding window regardless of how many work-notes the
ticket has accumulated. Cost ~$0.0003 per refresh on gpt-4o-mini.

Used by `src/oneops/embeddings/worker.py` when it processes a
chunk_type='diagnosis_trail' message from pgmq.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest
from oneops.observability import get_logger
from oneops.policy import Profile, compose

_log = get_logger(__name__)

_SYSTEM = (
    "You summarise the diagnostic trail of an ITSM ticket for similarity search. "
    "Output exactly 3 to 5 short bullet points covering: "
    "(1) symptoms confirmed or ruled out, "
    "(2) fixes attempted and their outcome, "
    "(3) current hypothesis or next step. "
    "Be terse and technical. No fluff, no PII, no timestamps, no person names. "
    "Cap output at 200 words total. "
    "Output bullets only; no preamble; no closing remarks."
)


def _extract_trail_text(raw_json: Any) -> str:
    """Pull just the text fields from a list of {text,author,timestamp,...} entries.

    Returns empty string when the input is empty or unrecognisable — caller
    decides whether to skip embedding.
    """
    if not raw_json:
        return ""
    if isinstance(raw_json, str):
        try:
            raw_json = json.loads(raw_json)
        except json.JSONDecodeError:
            return ""
    if not isinstance(raw_json, list):
        return ""
    lines: list[str] = []
    for entry in raw_json:
        if not isinstance(entry, Mapping):
            continue
        text = (entry.get("text") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


async def summarise_diagnosis(
    *,
    gateway: LlmGateway,
    tenant_id: str,
    entity_id: str,
    raw_trail: Any,
    model: str = "gpt-4o-mini",
) -> str:
    """Summarise a diagnosis trail into ≤ 200 words of bullet points.

    Returns the summary text (to be embedded). Returns empty string when the
    trail itself is empty — caller skips the embedding write.

    Raises on LLM failure (caller catches → retries via pgmq visibility).
    """
    trail = _extract_trail_text(raw_trail)
    if not trail.strip():
        return ""

    system_prompt = compose(
        Profile.FEATURE_AGENT,
        context={"tenant_id": tenant_id, "ticket_id": entity_id},
        extra_sections=[_SYSTEM],
    )
    request = LlmRequest(
        messages=(
            LlmMessage(role="system", content=system_prompt),
            LlmMessage(role="user",
                       content=f"Work-note entries (oldest first):\n{trail}"),
        ),
        model=model, tenant_id=tenant_id, user_id="", max_tokens=400,
    )
    out = await gateway.call(request)
    summary = (out.content or "").strip()
    if not summary:
        _log.warning("uc05.summarise_diagnosis.empty_output",
                      entity_id=entity_id, tenant_id=tenant_id)
    return summary


__all__ = ["summarise_diagnosis"]
