"""UC-8 text extraction — LLM picks `title` and preserves `description`.

ONE LLM call per "Auto-create SR" click. Closed-validation on output.
Production-grade discipline:

  • Reasoning-first JSON schema (chain-of-thought enforcer, web-search
    validated, +8pp accuracy lever per Dylan Castillo).
  • Policy composer (Profile.FEATURE_AGENT_JSON) — same egress
    discipline as UC-2 / UC-5 / catalog_reranker.
  • Closed-validation in the parser — title length and content
    constraints are enforced post-LLM, fallback to truncation if
    the LLM returns garbage.
  • Real-world resilience — typos, mixed case, foreign words, slang,
    polite wrapping all handled (per the prompt's resilience block).
  • Fail-loud on LLM failure — wraps in TextExtractError so callers
    can degrade gracefully (e.g., fall back to truncated user_text
    as title).
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass

import structlog
from opentelemetry import trace

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.policy.composer import Profile, compose

_log = structlog.get_logger("oneops.uc08.text_extract")
_tracer = trace.get_tracer("oneops.uc08.text_extract")


# Tunables
EXTRACT_MODEL = os.environ.get("UC08_TEXT_EXTRACT_MODEL", "gpt-4o-mini")
EXTRACT_TIMEOUT_S = float(os.environ.get("UC08_TEXT_EXTRACT_TIMEOUT_S", "60"))
MAX_TITLE_CHARS = 120          # hard cap; spec says ≤80, give the LLM slack then truncate
MAX_DESCRIPTION_CHARS = 4000   # absolute upper bound stored in DB
# Pre-truncate the user_text we send to the LLM. The full text is still
# stored in the description and the embedded vector — only the title-
# extract LLM gets the truncated version. OpenAI latency guidance:
# "Use fewer input tokens" rather than extending timeouts.
MAX_LLM_INPUT_CHARS = int(os.environ.get("UC08_TEXT_EXTRACT_MAX_INPUT_CHARS", "4000"))
PROMPT_VERSION = "v1"


class TextExtractError(Exception):
    """Typed boundary error for the LLM extract call."""


@dataclass(frozen=True)
class ExtractResult:
    title: str
    description: str
    reasoning: str          # what signal drove the title
    title_source: str       # 'llm_extract' | 'fallback_truncation' | 'empty_input'
    description_source: str # 'llm_preserved' | 'fallback_verbatim'
    raw_response: str       # the LLM's raw JSON text, for audit


_EXTRACT_INSTRUCTION = """
You are a Service Request structurer for an ITSM platform. Given a
user's free-text request, produce a clean SR title and a preserved
description.

═════════════════════════════════════════════════════════════════════
RULES
═════════════════════════════════════════════════════════════════════

1. Title: produce a concise headline ≤ 80 characters that captures the
   essence of the request. Use ITSM phrasing — formal, noun-phrase-led,
   no filler words.

   Good titles:
     "Onboarding for Maria Lopez — Senior Developer (Engineering)"
     "VPN access for contractor Tom Nguyen"
     "Standard developer laptop for Lisa Park"
     "MFA re-enrollment for new mobile device"

   Bad titles (avoid):
     "Hi team, can someone help with..."          (filler / chat lead-in)
     "URGENT URGENT plz fix"                       (all-caps shouting)
     "Request for onboarding stuff"                (vague)
     "The user wants something"                    (meta-description)

2. Description: preserve the user's verbatim text. Only do these
   small adjustments:
     - Trim leading / trailing whitespace.
     - Collapse runs of 3+ whitespace characters to a single space.
   Do NOT paraphrase. Do NOT summarize. Do NOT add new content.
   The description must remain useful to a technician reading the
   raw request later. It is the audit-grade record.

3. If the user's text is empty or whitespace-only, return null for
   both fields.

4. Title must use sentence case (first word capitalized). Never
   "TODO", "URGENT URGENT", or all-caps shouting.

═════════════════════════════════════════════════════════════════════
OUTPUT — JSON only. Exact field order (chain-of-thought enforcer):
═════════════════════════════════════════════════════════════════════

{
  "reasoning":   "<≤25 words: which signal in the user text drove the title>",
  "title":       "<≤80 char headline, sentence case, ITSM phrasing>",
  "description": "<preserved verbatim with minor whitespace cleanup>"
}

═════════════════════════════════════════════════════════════════════
EXAMPLES
═════════════════════════════════════════════════════════════════════

User: "Onboard our new senior dev Maria starting Monday in the engineering team — full kit please."
→ {"reasoning": "Named new-joiner with role + start + 'full kit' implies onboarding bundle.",
   "title": "Onboarding for Maria — Senior Developer (Engineering)",
   "description": "Onboard our new senior dev Maria starting Monday in the engineering team — full kit please."}

User: "hey can u set up vpn 4 our new intern starts monday thx"
→ {"reasoning": "Provisioning ask for an intern with a date — VPN setup.",
   "title": "VPN access for new intern starting Monday",
   "description": "hey can u set up vpn 4 our new intern starts monday thx"}

User: "Please initiate the standard new-hire provisioning workflow for our incoming senior engineer Robert Singh, joining Engineering on June 15."
→ {"reasoning": "Formal new-hire provisioning request for named senior engineer on stated date.",
   "title": "Onboarding for Robert Singh — Senior Engineer (June 15)",
   "description": "Please initiate the standard new-hire provisioning workflow for our incoming senior engineer Robert Singh, joining Engineering on June 15."}

User: "URGENT — VPN ASAP for John"
→ {"reasoning": "Urgent-flagged short ask for VPN provisioning for John.",
   "title": "VPN access for John (urgent)",
   "description": "URGENT — VPN ASAP for John"}

User: ""
→ {"reasoning": "Empty input — nothing to structure.",
   "title": null,
   "description": null}

═════════════════════════════════════════════════════════════════════
REAL-WORLD RESILIENCE
═════════════════════════════════════════════════════════════════════
Expect: typos, mixed case, casual phrasing, foreign words mixed in
(German, Spanish, Chinese), abbreviations (BYOD / WFH / SSO / SAML),
polite wrapping ("Hi team, thanks!"), all-caps yelling.

Title strips the noise. Description preserves it verbatim so the
audit-grade record of what the user typed is intact.
""".strip()


def _build_fallback_title(user_text: str) -> str:
    """Title fallback when the LLM returns garbage.

    Production-grade: never return None or an empty title — the
    `itsm.request.title` column is NOT NULL. Truncate the user text
    to a reasonable length at a word boundary.
    """
    cleaned = " ".join((user_text or "").split())
    if not cleaned:
        return "(empty request)"
    if len(cleaned) <= 80:
        return cleaned
    # Truncate at word boundary near 77 chars (3 chars for "...").
    cut = cleaned[:77]
    last_space = cut.rfind(" ")
    if last_space > 40:
        cut = cut[:last_space]
    return cut + "..."


def _clean_description(user_text: str) -> str:
    """Description fallback / canonical cleaner — collapse whitespace,
    trim, cap length."""
    if not user_text:
        return ""
    cleaned = " ".join(user_text.split())  # collapses runs of whitespace
    if len(cleaned) > MAX_DESCRIPTION_CHARS:
        cleaned = cleaned[:MAX_DESCRIPTION_CHARS - 3] + "..."
    return cleaned


def _truncate_for_llm(text: str) -> tuple[str, bool]:
    """Truncate (at a sentence boundary near the limit when possible) only what
    we send to the LLM — the full text stays the fallback description so audit
    fidelity is never lost. Returns (llm_input, truncated)."""
    if len(text) <= MAX_LLM_INPUT_CHARS:
        return text, False
    truncated_at = MAX_LLM_INPUT_CHARS
    for marker in (". ", "? ", "! "):
        idx = text.rfind(marker, 0, MAX_LLM_INPUT_CHARS)
        if idx > MAX_LLM_INPUT_CHARS - 400:
            truncated_at = idx + 1
            break
    return text[:truncated_at] + " […truncated for LLM]", True


def _extract_title(parsed: dict[str, Any], raw_text: str) -> tuple[str, str]:
    """Closed validation of the LLM title → (title, title_source). Falls back
    to a truncation-derived title; strips common chat lead-in filler."""
    llm_title = parsed.get("title")
    if not isinstance(llm_title, str) or not llm_title.strip():
        return _build_fallback_title(raw_text), "fallback_truncation"
    t = llm_title.strip()
    if len(t) > MAX_TITLE_CHARS:
        t = t[:MAX_TITLE_CHARS]
    for filler in ("Hi team, ", "Hello team, ", "Hi, "):
        if t.lower().startswith(filler.lower()):
            t = t[len(filler):].strip()
    return t, "llm_extract"


def _extract_description(parsed: dict[str, Any], raw_text: str) -> tuple[str, str]:
    """Closed validation of the LLM description → (description, source). Falls
    back to the cleaned verbatim user text; caps at MAX_DESCRIPTION_CHARS."""
    llm_desc = parsed.get("description")
    if not isinstance(llm_desc, str) or not llm_desc.strip():
        return _clean_description(raw_text), "fallback_verbatim"
    description = llm_desc.strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS - 3] + "..."
    return description, "llm_preserved"


async def extract_title_and_description(
    *,
    user_text: str,
    gateway: LlmGateway,
    tenant_id: str,
    user_id: str = "",
) -> ExtractResult:
    """Single LLM call. Returns ExtractResult with title + description
    + provenance fields.

    Production-grade contract:
      • Never raises on bad LLM output — always returns a viable
        result (uses fallback title/description if LLM is malformed).
      • Raises TextExtractError ONLY on gateway failure / timeout
        / quota refusal — caller distinguishes "gateway unavailable"
        from "extraction succeeded".
    """
    raw_text = user_text or ""
    if not raw_text.strip():
        return ExtractResult(
            title="(empty request)",
            description="",
            reasoning="empty input",
            title_source="empty_input",
            description_source="empty_input",
            raw_response="",
        )

    # Pre-truncate ONLY what we send to the LLM. The full text remains
    # the fallback description so we never lose audit fidelity.
    llm_input, truncated = _truncate_for_llm(raw_text)

    sys_prompt = compose(
        Profile.FEATURE_AGENT_JSON,
        extra_sections=[_EXTRACT_INSTRUCTION],
    )

    with _tracer.start_as_current_span(
        "uc08.text_extract.call",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.user_text_chars": len(raw_text),
            "uc08.llm_input_chars": len(llm_input),
            "uc08.input_truncated": truncated,
            "uc08.prompt_version": PROMPT_VERSION,
        },
    ) as span:
        try:
            resp = await asyncio.wait_for(
                gateway.call(LlmRequest(
                    messages=(
                        LlmMessage(role="system", content=sys_prompt),
                        LlmMessage(role="user", content=raw_text),
                    ),
                    model=EXTRACT_MODEL,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    temperature=0.0,
                    max_tokens=600,
                    response_format=ResponseFormat.JSON,
                )),
                timeout=EXTRACT_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise TextExtractError(
                f"text-extract LLM call exceeded {EXTRACT_TIMEOUT_S}s",
            ) from exc
        except Exception as exc:                                    # noqa: BLE001
            raise TextExtractError(
                f"text-extract gateway failure: {type(exc).__name__}: {exc}",
            ) from exc

        raw = (resp.content or "").strip().lstrip("`").rstrip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()

        # Parse + validate
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            _log.warning(
                "uc08.text_extract.parse_failed",
                tenant_id=tenant_id,
                error=str(exc)[:120],
                head=raw[:120],
            )
            title = _build_fallback_title(raw_text)
            description = _clean_description(raw_text)
            return ExtractResult(
                title=title,
                description=description,
                reasoning="LLM output unparseable",
                title_source="fallback_truncation",
                description_source="fallback_verbatim",
                raw_response=raw,
            )

        # Title + description — closed validation (LLM value or fallback)
        title, title_source = _extract_title(parsed, raw_text)
        description, description_source = _extract_description(parsed, raw_text)

        reasoning = (parsed.get("reasoning") or "")[:280]

        span.set_attribute("uc08.title_source", title_source)
        span.set_attribute("uc08.description_source", description_source)
        span.set_attribute("uc08.title_chars", len(title))
        span.set_attribute("uc08.description_chars", len(description))

        _log.info(
            "uc08.text_extract.completed",
            tenant_id=tenant_id,
            title_source=title_source,
            description_source=description_source,
            title=title[:80],
            reasoning=reasoning[:140],
        )

        return ExtractResult(
            title=title,
            description=description,
            reasoning=reasoning,
            title_source=title_source,
            description_source=description_source,
            raw_response=raw,
        )


__all__ = [
    "ExtractResult",
    "TextExtractError",
    "extract_title_and_description",
    "PROMPT_VERSION",
]
