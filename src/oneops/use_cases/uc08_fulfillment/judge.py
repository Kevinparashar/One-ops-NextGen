"""UC-8 LLM-as-judge — faithfulness verification for generated artifacts.

Two LLM-generated artifacts ship to the user in UC-8:

  1. Extracted SR title + description (from `text_extract.py`)
  2. Catalog match decision     (from `catalog_reranker.py`)

This module runs a SECOND, independent LLM call after each to judge
whether the generated artifact is faithful to the input. The verdict
is closed-enum (FAITHFUL / UNFAITHFUL / UNCERTAIN) with a numeric
confidence and a short reason.

Design discipline:

  • Same egress (LlmGateway + Profile.FEATURE_AGENT_JSON) as every
    other LLM call in UC-8 — policy, tenant cost, OTel apply uniformly.
  • Reasoning-first JSON (chain-of-thought enforcer).
  • Closed-enum validation in the parser; UNCERTAIN on any parse
    failure (we never raise into business code — a flaky judge MUST
    NOT break the main flow).
  • Production-grade timeout (60s default; overridable via env).
  • Metrics + OTel span per call so we can ratchet UNFAITHFUL rate
    and drive prompt improvements.
  • Stateless and idempotent — safe to retry, safe to call in
    parallel with other UC-8 work.

The judge does NOT mutate the artifact. The caller (route handler)
decides what to do with the verdict — typically surface
`judge_verdict` + `judge_reasoning` in the API response so the
frontend can flag low-confidence outputs to the user.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum

import structlog
from opentelemetry import trace

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability.metrics import histogram, increment
from oneops.policy.composer import Profile, compose

# Telemetry/HTTP literals → constants (sonar S1192).
_AI_UC08_JUDGE_VERDICT_TOTAL = "ai.uc08.judge.verdict.total"
_UC08_JUDGE_VERDICT = "uc08.judge.verdict"

_log = structlog.get_logger("oneops.uc08.judge")
_tracer = trace.get_tracer("oneops.uc08.judge")


JUDGE_MODEL = os.environ.get("UC08_JUDGE_MODEL", "gpt-4o-mini")
JUDGE_TIMEOUT_S = float(os.environ.get("UC08_JUDGE_TIMEOUT_S", "60"))
JUDGE_MAX_INPUT_CHARS = int(os.environ.get("UC08_JUDGE_MAX_INPUT_CHARS", "4000"))
JUDGE_PROMPT_VERSION = "v1"


class JudgeVerdict(StrEnum):
    FAITHFUL = "FAITHFUL"
    UNFAITHFUL = "UNFAITHFUL"
    UNCERTAIN = "UNCERTAIN"


_VALID_VERDICTS = {v.value for v in JudgeVerdict}


@dataclass(frozen=True)
class JudgeResult:
    verdict: JudgeVerdict
    confidence: float           # 0.0 – 1.0
    reasoning: str              # short rationale, ≤ 240 chars
    model: str                  # which model produced the verdict
    judge_name: str             # 'extraction' | 'rerank'
    raw_response: str           # for audit


_EXTRACTION_JUDGE_INSTRUCTION = """
You are a faithfulness judge for a Service Request structurer. Given
the user's original free-text request and the structured output a
prior model produced (title + description), decide whether the output
is FAITHFUL to the input.

═════════════════════════════════════════════════════════════════════
FAITHFULNESS DEFINITION
═════════════════════════════════════════════════════════════════════

FAITHFUL means ALL of:
  1. The title accurately captures the user's primary intent — no
     invented people, products, or actions.
  2. The description preserves the user's request (light whitespace
     normalisation OK; paraphrasing or summarisation NOT OK).
  3. No hallucinated facts (no fabricated names, dates, asset IDs,
     departments not present in the user's text).
  4. No dropped critical facts that would change downstream routing
     (e.g., losing "VIP", "URGENT", a named system, or a deadline).

UNFAITHFUL means ANY of:
  • Invented entity (a name, system, or asset not in the input).
  • Paraphrased description (changed wording in a way that loses
    meaning or adds new claims).
  • Wrong intent in the title (e.g., user asked for password reset,
    title says onboarding).
  • Lost a critical fact (VIP flag, severity word, deadline).

UNCERTAIN is allowed only when the input is genuinely ambiguous or
empty/garbage — never use it as a hedge for clear cases.

═════════════════════════════════════════════════════════════════════
OUTPUT FORMAT (STRICT JSON)
═════════════════════════════════════════════════════════════════════

Return ONLY this JSON, no prose, no markdown fences:

{
  "reasoning": "<one sentence, ≤ 240 chars, citing the specific signal>",
  "verdict": "FAITHFUL" | "UNFAITHFUL" | "UNCERTAIN",
  "confidence": <float 0.0 - 1.0>
}

Examples of good reasoning:
  "Title captures onboarding intent; description preserves all named entities (Maria, Senior Dev, Monday)."
  "Title says password reset but user asked for VPN access — wrong intent."
  "Description added 'urgent' which user never said — hallucinated."
"""


_RERANK_JUDGE_INSTRUCTION = """
You are a faithfulness judge for a catalog-match decision. Given the
user's request text and the catalog item a prior model chose (with
its short label/description), decide whether the chosen catalog is a
plausible fulfillment for the request.

═════════════════════════════════════════════════════════════════════
FAITHFULNESS DEFINITION
═════════════════════════════════════════════════════════════════════

FAITHFUL means the chosen catalog item could plausibly satisfy what
the user asked for. It need not be the perfect catalog; close
substitutes (e.g., "VPN Access" for "install VPN client") are
FAITHFUL. Different domain entirely is UNFAITHFUL.

UNFAITHFUL means the catalog is in a different intent domain from
the request. Examples:
  • User asks for a laptop, catalog is password reset.
  • User asks a how-to question (information seeking, not a service
    request), catalog is a provisioning workflow.
  • User says "cancel my SR", catalog is "create new SR".

UNCERTAIN is allowed only when the request is genuinely ambiguous
between two domains — never use it as a hedge for clear cases.

═════════════════════════════════════════════════════════════════════
OUTPUT FORMAT (STRICT JSON)
═════════════════════════════════════════════════════════════════════

Return ONLY this JSON, no prose, no markdown fences:

{
  "reasoning": "<one sentence, ≤ 240 chars, citing the specific signal>",
  "verdict": "FAITHFUL" | "UNFAITHFUL" | "UNCERTAIN",
  "confidence": <float 0.0 - 1.0>
}
"""


def _truncate(text: str, limit: int = JUDGE_MAX_INPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " […truncated]"


def _parse(raw: str, _judge_name: str) -> tuple[JudgeVerdict, float, str]:
    """Closed-enum parse. Returns (verdict, confidence, reasoning).

    On any parse failure: UNCERTAIN, 0.0, '<reason>'. Never raises.
    """
    text = (raw or "").strip().lstrip("`").rstrip("`")
    if text.startswith("json"):
        text = text[4:].strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return JudgeVerdict.UNCERTAIN, 0.0, "judge JSON unparseable"
    if not isinstance(obj, dict):
        return JudgeVerdict.UNCERTAIN, 0.0, "judge output not an object"
    verdict_raw = str(obj.get("verdict", "")).strip().upper()
    if verdict_raw not in _VALID_VERDICTS:
        return JudgeVerdict.UNCERTAIN, 0.0, f"unknown verdict '{verdict_raw}'"
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reasoning = str(obj.get("reasoning", "")).strip()[:240] or "no reason given"
    return JudgeVerdict(verdict_raw), confidence, reasoning


async def _call_judge(
    *,
    gateway: LlmGateway,
    tenant_id: str,
    user_id: str,
    instruction: str,
    user_payload: str,
    judge_name: str,
) -> JudgeResult:
    sys_prompt = compose(
        Profile.FEATURE_AGENT_JSON,
        extra_sections=[instruction],
    )
    started = time.perf_counter()
    with _tracer.start_as_current_span(
        f"uc08.judge.{judge_name}",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.judge.name": judge_name,
            "uc08.judge.input_chars": len(user_payload),
            "uc08.judge.prompt_version": JUDGE_PROMPT_VERSION,
        },
    ) as span:
        try:
            resp = await asyncio.wait_for(
                gateway.call(LlmRequest(
                    messages=(
                        LlmMessage(role="system", content=sys_prompt),
                        LlmMessage(role="user", content=user_payload),
                    ),
                    model=JUDGE_MODEL,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    temperature=0.0,
                    max_tokens=300,
                    response_format=ResponseFormat.JSON,
                )),
                timeout=JUDGE_TIMEOUT_S,
            )
            raw = resp.content or ""
        except TimeoutError:
            _log.warning(
                "uc08.judge.timeout",
                judge=judge_name,
                timeout_s=JUDGE_TIMEOUT_S,
                tenant_id=tenant_id,
            )
            span.set_attribute(_UC08_JUDGE_VERDICT, "UNCERTAIN")
            span.set_attribute("uc08.judge.failure", "timeout")
            increment(
                _AI_UC08_JUDGE_VERDICT_TOTAL,
                judge=judge_name,
                verdict="UNCERTAIN",
                failure="timeout",
            )
            return JudgeResult(
                verdict=JudgeVerdict.UNCERTAIN,
                confidence=0.0,
                reasoning=f"judge call exceeded {JUDGE_TIMEOUT_S}s",
                model=JUDGE_MODEL,
                judge_name=judge_name,
                raw_response="",
            )
        except Exception as exc:                                       # noqa: BLE001
            _log.warning(
                "uc08.judge.gateway_failure",
                judge=judge_name,
                error=f"{type(exc).__name__}: {exc}"[:200],
                tenant_id=tenant_id,
            )
            span.set_attribute(_UC08_JUDGE_VERDICT, "UNCERTAIN")
            span.set_attribute("uc08.judge.failure", type(exc).__name__)
            increment(
                _AI_UC08_JUDGE_VERDICT_TOTAL,
                judge=judge_name,
                verdict="UNCERTAIN",
                failure="gateway",
            )
            return JudgeResult(
                verdict=JudgeVerdict.UNCERTAIN,
                confidence=0.0,
                reasoning=f"judge gateway failure: {type(exc).__name__}",
                model=JUDGE_MODEL,
                judge_name=judge_name,
                raw_response="",
            )

        verdict, confidence, reasoning = _parse(raw, judge_name)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        span.set_attribute(_UC08_JUDGE_VERDICT, verdict.value)
        span.set_attribute("uc08.judge.confidence", confidence)
        span.set_attribute("uc08.judge.latency_ms", elapsed_ms)
        increment(
            _AI_UC08_JUDGE_VERDICT_TOTAL,
            judge=judge_name,
            verdict=verdict.value,
        )
        histogram(
            "ai.uc08.judge.latency_ms",
            elapsed_ms,
            judge=judge_name,
        )
        _log.info(
            "uc08.judge.completed",
            judge=judge_name,
            verdict=verdict.value,
            confidence=confidence,
            reasoning=reasoning,
            latency_ms=round(elapsed_ms, 1),
            tenant_id=tenant_id,
        )
        return JudgeResult(
            verdict=verdict,
            confidence=confidence,
            reasoning=reasoning,
            model=JUDGE_MODEL,
            judge_name=judge_name,
            raw_response=raw,
        )


async def judge_extraction(
    *,
    gateway: LlmGateway,
    tenant_id: str,
    user_id: str,
    user_text: str,
    extracted_title: str,
    extracted_description: str,
) -> JudgeResult:
    """Judge an extracted SR title + description against the original text."""
    payload = (
        f"USER_TEXT:\n{_truncate(user_text)}\n\n"
        f"EXTRACTED_TITLE:\n{_truncate(extracted_title, 240)}\n\n"
        f"EXTRACTED_DESCRIPTION:\n{_truncate(extracted_description)}"
    )
    return await _call_judge(
        gateway=gateway,
        tenant_id=tenant_id,
        user_id=user_id,
        instruction=_EXTRACTION_JUDGE_INSTRUCTION,
        user_payload=payload,
        judge_name="extraction",
    )


async def judge_rerank(
    *,
    gateway: LlmGateway,
    tenant_id: str,
    user_id: str,
    user_text: str,
    chosen_catalog_id: str,
    chosen_catalog_label: str,
    chosen_catalog_description: str,
) -> JudgeResult:
    """Judge a catalog-match decision against the original request."""
    payload = (
        f"USER_REQUEST:\n{_truncate(user_text)}\n\n"
        f"CHOSEN_CATALOG_ID: {chosen_catalog_id}\n"
        f"CHOSEN_CATALOG_LABEL: {_truncate(chosen_catalog_label, 240)}\n"
        f"CHOSEN_CATALOG_DESCRIPTION:\n{_truncate(chosen_catalog_description)}"
    )
    return await _call_judge(
        gateway=gateway,
        tenant_id=tenant_id,
        user_id=user_id,
        instruction=_RERANK_JUDGE_INSTRUCTION,
        user_payload=payload,
        judge_name="rerank",
    )


__all__ = [
    "JudgeVerdict",
    "JudgeResult",
    "judge_extraction",
    "judge_rerank",
    "JUDGE_MODEL",
    "JUDGE_TIMEOUT_S",
]
