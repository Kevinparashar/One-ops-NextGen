"""UC-8 catalog reranker — LLM listwise reranking over top-K candidates.

When the embedding-based cosine score is in the "soft zone" (0.35-0.65),
a single LLM call re-evaluates the candidates against the SR text and
returns the best match (or NO_MATCH / WRONG_INTENT). This fixes the
failure modes that pure dense retrieval cannot solve:

  • Indirect language ("she works from cafes mostly" → VPN)
  • Negation        ("do NOT need laptop, only mailbox" → mailbox)
  • Niche jargon    ("SSO + IdP federation" → account/identity)
  • Paraphrases     ("set up remote work tunnel" → VPN)

Web-search-validated production pattern (RankGPT, OpenAI cookbook,
Cohere reranker). One LLM call per query in the soft zone — auto-pick
and clear rejections skip this stage entirely.

Production-grade properties:
  • Policy composition via Profile.FEATURE_AGENT_JSON
  • Dragonfly cache keyed by sha256(sr_text + top_K_ids) — free replays
  • Closed output taxonomy: catalog_item_id from candidates, or
    "NO_MATCH" / "WRONG_INTENT". Hallucinations refused.
  • Bounded by timeout; failure surfaces as CatalogSearchError.
  • Approval contract preserved — reranker returns SUGGESTION, never acts.

# TODO(option-C): when the pre-router intent classifier lands, the
# WRONG_INTENT branch can be removed from the prompt and result — the
# classifier will have already filtered off-domain queries before they
# reach this module. Confidence thresholds may also need re-calibration.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any

import structlog
from opentelemetry import trace

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import set_langfuse_io
from oneops.observability.metrics import increment as _metric_inc
from oneops.policy.composer import Profile, compose
from oneops.use_cases.uc08_fulfillment.catalog_search import (
    CatalogMatch,
    CatalogSearchError,
)

_log = structlog.get_logger("oneops.uc08.catalog_reranker")
_tracer = trace.get_tracer("oneops.uc08.catalog_reranker")


# ── Tunable thresholds ──────────────────────────────────────────────────


# Below this top-1 cosine, skip rerank — there's nothing worth re-ranking.
RERANK_FLOOR = float(os.environ.get("UC08_RERANK_FLOOR", "0.35"))

# Above this top-1 cosine, skip rerank — embedding is confident enough.
RERANK_CEILING = float(os.environ.get("UC08_RERANK_CEILING", "0.65"))

# How many candidates the reranker sees. K is small for a small catalog;
# more candidates = more LLM context = more cost.
RERANK_TOP_K = int(os.environ.get("UC08_RERANK_TOP_K", "5"))

# Minimum confidence the LLM must report for an auto-confirm. Below this,
# the reranker still recommends a candidate, but the chat layer SHOULD
# surface it as a suggestion (not auto-fulfil).
RERANK_CONFIDENCE_FLOOR = float(
    os.environ.get("UC08_RERANK_CONFIDENCE_FLOOR", "0.70"))

# Hard timeout on the rerank LLM call. Caller gets CatalogSearchError on
# timeout — never a silent hang.
RERANK_TIMEOUT_S = float(os.environ.get("UC08_RERANK_TIMEOUT_S", "60.0"))

# Model. Default to a fast JSON-capable model. Overridable per-deployment.
RERANK_MODEL = os.environ.get("UC08_RERANK_MODEL", "gpt-4o-mini")

# Cache TTL (24h). Same query → same answer for a day.
RERANK_CACHE_TTL_S = int(os.environ.get("UC08_RERANK_CACHE_TTL_S", "86400"))


# ── Result types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RerankResult:
    """The reranker's verdict on one query.

    `chosen` is the catalog_item_id picked by the LLM. None when the LLM
    decided NO_MATCH (no candidate fits) or WRONG_INTENT (query is
    incident/KB/off-topic and shouldn't have reached UC-8).
    """
    chosen: str | None              # catalog_item_id or None
    chosen_match: CatalogMatch | None  # the full row, if chosen
    confidence: float               # LLM-reported confidence 0.0–1.0
    reasoning: str                  # one-sentence why
    verdict: str                    # 'CHOSEN' | 'NO_MATCH' | 'WRONG_INTENT'
    from_cache: bool                # was this a cache hit?
    skipped: bool = False           # True when reranker was bypassed
    skip_reason: str = ""           # why bypassed (above ceiling / below floor)


# ── Routing decision ────────────────────────────────────────────────────


def should_rerank(top1_cosine: float) -> tuple[bool, str]:
    """Decides whether the reranker fires.

    Returns (should_run, skip_reason). When `should_run=False`, the caller
    uses the embedding-only verdict (auto-pick or reject) directly.

    Floor/ceiling are re-read from the env per-call: the module-level
    RERANK_FLOOR / RERANK_CEILING are import-time defaults read BEFORE the app
    loads .env, so a per-call read is what makes UC08_RERANK_* changes take
    effect (parity with uc03's per-call gate).
    """
    floor = float(os.environ.get("UC08_RERANK_FLOOR", str(RERANK_FLOOR)))
    ceiling = float(os.environ.get("UC08_RERANK_CEILING", str(RERANK_CEILING)))
    if top1_cosine >= ceiling:
        return False, "above_ceiling_auto_pick"
    if top1_cosine < floor:
        return False, "below_floor_no_match"
    return True, ""


# ── Cache key ───────────────────────────────────────────────────────────


def _cache_key(
    *, tenant_id: str, sr_text: str, candidates: tuple[CatalogMatch, ...],
) -> str:
    """SHA256 of (tenant, query, candidate_ids). Tenant-isolated; same
    query+candidate set yields same cache hit."""
    h = hashlib.sha256()
    h.update(f"{tenant_id}|".encode())
    h.update(sr_text.encode())
    for m in candidates:
        h.update(f"|{m.catalog_item_id}".encode())
    return f"uc08.rerank.v1:{h.hexdigest()[:24]}"


# ── Prompt builder ──────────────────────────────────────────────────────


# Web-search-validated prompt construction (May 2026):
#
#   1. Reasoning field BEFORE answer field — chain-of-thought enforcer.
#      LLMs generate left-to-right; reasoning tokens must be emitted
#      before the answer token can commit. Sources: Dylan Castillo
#      "Don't put the cart before the horse" (8pp accuracy gain);
#      Yoav Goldberg "Structured-CoT breaks basic language principles";
#      OpenAI structured outputs cookbook.
#
#   2. Explicit two-step decision: intent classification BEFORE candidate
#      selection. The model first commits to "is this a fulfilment
#      request?" (a binary), THEN picks a candidate (only if yes).
#
#   3. Few-shot examples MIRROR the output schema exactly (same field
#      order). Reduces format drift.
#
#   4. Closed enumeration: intent_class ∈ {fulfilment, problem_report,
#      how_to, off_topic}. Closed sets ≤ 50 values minimise hallucination
#      (Branch8 / SureBlocks guidance).
#
#   5. Negative reinforcement: explicit rules of what NOT to count as
#      match evidence (keyword overlap alone is forbidden).
_RERANK_INSTRUCTION = """
You are a Service-Catalog matching engine for an ITSM platform. You
receive a user's natural-language request plus a list of catalog
candidates. Your job: make ONE decision per request. You will be wrong
sometimes — be HONEST about uncertainty rather than confidently wrong.

═════════════════════════════════════════════════════════════════════
HOW TO THINK — in this order. Do not skip steps. Do not reorder.
═════════════════════════════════════════════════════════════════════

STEP 1. INTENT CLASSIFICATION (binary commitment, no candidate yet)

  Read the user's message. Classify into exactly ONE of four classes:

    "fulfilment"     The user wants the SYSTEM to DO something for
                     them: provision, create, order, grant, set up,
                     onboard, assign. Action-oriented, future-tense.

    "problem_report" The user reports something is BROKEN or NOT
                     WORKING. Verbs of dysfunction: drops, fails,
                     crashed, stuck, jammed, can't access, won't
                     load, slow, error, broken, dead. Present-tense
                     pain.

    "how_to"         The user is asking a QUESTION about procedure,
                     policy, or knowledge. Opens with: how, what,
                     where, when, why, can you explain, show me,
                     teach me, walk me through, what's the.

    "off_topic"      The user's message is not ITSM/ITOM at all.
                     Weather, travel, food, jokes, personal life,
                     greetings, chit-chat.

  ANTI-PATTERNS — beware these mistakes:

    • Keyword overlap is NOT intent. "How do I set up VPN" contains
      "VPN" but is "how_to", not "fulfilment".
    • Brand mentions are NOT intent. "Outlook is slow" is
      "problem_report", not a request to provision Outlook.
    • Politeness ≠ ambiguity. "Hi team, hope you're well, could
      we get Lisa a laptop please?" is "fulfilment" — the politeness
      is wrapping the actual ask.
    • Foreign words / typos / slang do not change intent. Read
      THROUGH them to the underlying ask.

STEP 2. CANDIDATE SELECTION (only when intent_class == "fulfilment")

  If intent_class is anything other than "fulfilment", set chosen to
  null and skip candidate scoring. The user message will be routed
  elsewhere by the system. Be confident: confidence 0.85–0.95 for
  clear non-fulfilment intents.

  If intent_class == "fulfilment", evaluate every candidate against
  the user's UNDERLYING intent (not their words). Pick the candidate
  whose NAME + DESCRIPTION best matches what the user actually wants.

  Reasoning aids for fulfilment matching:

    • Negation. "NOT X, only Y" → pick Y, never X.
    • Multi-intent. "X AND Y AND Z" bundled → prefer the catalog
      item that bundles them (often onboarding/full-setup items)
      over any individual item.
    • Indirect language. "she works from cafes" → VPN/remote access.
      "needs to take calls in stand-ups" → headset. "ML training
      jobs" → GPU workstation.
    • Niche jargon. SSO/IdP/SAML → identity/account provisioning.
      "ephemeral container" / "playground" → cloud sandbox.
      "read replica" → database access.
    • Cosine is a HINT, not a floor. A 0.42 cosine match with a
      clear conceptual fit is better than a 0.55 cosine match that
      is semantically wrong. Override the embedding when you have
      reason to.

  If NO candidate is a reasonable fit even after considering the
  above, return chosen = null and intent_class = "fulfilment" with
  confidence 0.30–0.50 (low confidence is the correct signal here).

STEP 3. CONFIDENCE CALIBRATION (be honest)

  Anchor your number to one of these patterns:

    0.95  Canonical, unambiguous case. "I need VPN access" → VPN.
    0.85  Strong match through one layer of interpretation.
          (Paraphrase, typo, simple jargon.)
    0.75  Solid match through multiple layers of interpretation.
          (Negation, niche jargon, indirect language.)
    0.60  Best of several reasonable options. User should confirm.
    0.40  Tentative. Probably the right candidate but you're not sure.
    0.20  No clear winner. Return chosen = null.

═════════════════════════════════════════════════════════════════════
OUTPUT SCHEMA — fields are emitted in this order BY DESIGN.
═════════════════════════════════════════════════════════════════════

The JSON MUST contain exactly these four fields in this order:

  1. "reasoning"     — Your chain-of-thought. 1–3 sentences. Cite the
                       specific signal in the user message that drove
                       your decision. Do not be flowery. Be specific.

  2. "intent_class"  — One of: "fulfilment" | "problem_report" |
                       "how_to" | "off_topic"

  3. "confidence"    — Float 0.0 to 1.0, calibrated per Step 3.

  4. "chosen"        — When intent_class == "fulfilment" AND a candidate
                       fits: the bracketed [CAT_ID] of the chosen
                       candidate (no brackets in the value — just the id).
                     — Otherwise: null.

Output MUST be valid JSON. No markdown fences. No prose outside the
JSON. No commentary. Just the object.

═════════════════════════════════════════════════════════════════════
EXAMPLES — production-realistic real-world phrasings.
═════════════════════════════════════════════════════════════════════

User: "I need VPN access for a new contractor"
→ {"reasoning": "Direct provisioning request — 'I need VPN access' is canonical fulfilment phrasing for a named beneficiary.",
   "intent_class": "fulfilment", "confidence": 0.95,
   "chosen": "CAT_VPN_ACCESS"}

User: "how do I install the VPN client"
→ {"reasoning": "Opens with 'how do I' — this is a procedural question seeking documentation, not a request to provision anything.",
   "intent_class": "how_to", "confidence": 0.95,
   "chosen": null}

User: "VPN drops every 5 minutes"
→ {"reasoning": "Present-tense dysfunction ('drops every 5 minutes') reporting a broken existing service — this is an incident, not a fulfilment.",
   "intent_class": "problem_report", "confidence": 0.95,
   "chosen": null}

User: "do NOT need a laptop, only mailbox"
→ {"reasoning": "Negation — user explicitly excludes laptop and requests mailbox. Honor the affirmative ask, ignore the negated one.",
   "intent_class": "fulfilment", "confidence": 0.88,
   "chosen": "CAT_MAILBOX"}

User: "spin up a dev environment for prototyping"
→ {"reasoning": "Niche jargon — 'spin up a dev environment' is shorthand for ephemeral cloud sandbox provisioning.",
   "intent_class": "fulfilment", "confidence": 0.80,
   "chosen": "CAT_CLOUD_SANDBOX"}

User: "Hi team, hope you're well — could we get Lisa setup before she starts on the 15th? Thanks!"
→ {"reasoning": "Polite wrapping around a clear ask — 'get Lisa setup before she starts' is canonical new-hire onboarding.",
   "intent_class": "fulfilment", "confidence": 0.90,
   "chosen": "CAT_ONBOARDING"}

User: "she will be working from cafes mostly"
→ {"reasoning": "Indirect language — 'works from cafes' implies remote/untrusted-network access requirement, mapping to VPN.",
   "intent_class": "fulfilment", "confidence": 0.72,
   "chosen": "CAT_VPN_ACCESS"}

User: "what's the weather"
→ {"reasoning": "Weather inquiry — outside ITSM/ITOM scope entirely.",
   "intent_class": "off_topic", "confidence": 0.95,
   "chosen": null}

User: "give them a laptop AND VPN AND email setup"
→ {"reasoning": "Multi-intent bundle — laptop + VPN + email is the canonical onboarding package; prefer the bundled item over any single component.",
   "intent_class": "fulfilment", "confidence": 0.88,
   "chosen": "CAT_ONBOARDING"}

User: "any tickets about email outage we've seen recently"
→ {"reasoning": "Asks to find past similar tickets — that's similar-ticket search, not catalog fulfilment.",
   "intent_class": "how_to", "confidence": 0.85,
   "chosen": null}

User: "headset"
→ {"reasoning": "One-word fulfilment request — minimal but unambiguous, maps directly to the headset catalog item.",
   "intent_class": "fulfilment", "confidence": 0.85,
   "chosen": "CAT_HEADSET"}

═════════════════════════════════════════════════════════════════════
REAL-WORLD RESILIENCE — anticipated input distribution
═════════════════════════════════════════════════════════════════════

Real users write messy. Expect and handle:
  • Typos: "VNP acces fro contracter" → still VPN-access-request.
  • Mixed case / no punctuation: "ineed a laptop asap" → still fulfilment.
  • Slang / abbreviations: "spin up", "stand up", "kick off", "wfh",
    "byod", "mfa", "ad", "sso" — interpret literally.
  • Foreign words mixed in: "Set up VPN für die neue Mitarbeiterin"
    → still fulfilment for VPN.
  • Sarcasm / frustration: "REALLY need this VPN to work" — the
    capital letters are emphasis, not a problem report unless paired
    with a dysfunction verb.
  • Empty implication: "for John" with no other context → low
    confidence, ask for more (chosen = null, confidence ≤ 0.40).
""".strip()


def _build_user_prompt(
    *, sr_text: str, candidates: tuple[CatalogMatch, ...],
) -> str:
    lines = [
        "USER REQUEST:",
        f"  {sr_text}",
        "",
        f"CANDIDATES ({len(candidates)} from semantic search, ranked by cosine):",
    ]
    for m in candidates:
        desc = (m.description or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:197] + "..."
        lines.append(
            f"  [{m.catalog_item_id}] {m.name}  (cosine={m.cosine_score:.2f})"
        )
        lines.append(f"     Description: {desc or '(no description)'}")
        lines.append(f"     Category: {m.category}  Owner: {m.owner_group}")
        lines.append("")
    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────


async def _rerank_from_cache(cache, key: str, by_id: dict) -> RerankResult | None:
    """Return a cached rerank verdict (validated against the live candidate
    set), or None on miss / cache error / corrupted entry (caller falls
    through to the LLM)."""
    try:
        cached_raw = await cache.get(key)
    except Exception:                       # noqa: BLE001
        return None
    if cached_raw is None:
        return None
    try:
        cached = (
            json.loads(cached_raw)
            if isinstance(cached_raw, (str, bytes))
            else cached_raw
        )
        chosen_id = cached.get("chosen")
        return RerankResult(
            chosen=chosen_id if chosen_id in by_id else None,
            chosen_match=by_id.get(chosen_id) if chosen_id in by_id else None,
            confidence=float(cached.get("confidence", 0.5)),
            reasoning=str(cached.get("reasoning", ""))[:200],
            verdict=cached.get("verdict", "NO_MATCH"),
            from_cache=True,
        )
    except Exception:                    # noqa: BLE001
        return None  # corrupted cache entry — fall through to LLM


def _classify_verdict(
    parsed: dict[str, Any], by_id: dict, tenant_id: str,
) -> tuple[str, str | None, Any, float]:
    """Closed-taxonomy intent gate → (verdict, chosen, chosen_match,
    confidence). Non-fulfilment intent → WRONG_INTENT; fulfilment with a valid
    candidate id → CHOSEN; a hallucinated/absent id or unknown intent →
    NO_MATCH (confidence clamped down)."""
    intent_class = str(parsed.get("intent_class", "")).strip().lower()
    confidence = float(parsed.get("confidence", 0.0) or 0.0)
    chosen_raw = parsed.get("chosen")
    chosen_raw = str(chosen_raw).strip() if chosen_raw is not None else ""

    if intent_class in ("problem_report", "how_to") or intent_class == "off_topic":
        return "WRONG_INTENT", None, None, confidence
    if intent_class == "fulfilment":
        if chosen_raw and chosen_raw in by_id:
            return "CHOSEN", chosen_raw, by_id[chosen_raw], confidence
        if chosen_raw and chosen_raw.upper() not in ("NULL", "NONE", "NO_MATCH", ""):
            # Hallucinated id — refuse.
            _log.warning("uc08.rerank.hallucinated_id",
                         tenant_id=tenant_id,
                         hallucinated=chosen_raw[:60],
                         valid_ids=list(by_id.keys())[:5])
            return "NO_MATCH", None, None, min(confidence, 0.4)
        return "NO_MATCH", None, None, confidence
    # Missing or unknown intent_class — refuse loudly.
    _log.warning("uc08.rerank.unknown_intent_class",
                 tenant_id=tenant_id, intent_class=intent_class[:40])
    return "NO_MATCH", None, None, min(confidence, 0.3)


async def rerank(
    *, tenant_id: str, sr_text: str,
    candidates: tuple[CatalogMatch, ...],
    gateway: LlmGateway,
    cache=None,
    user_id: str = "",
) -> RerankResult:
    """LLM listwise rerank over top-K embedding candidates.

    Cache-first, gateway-bounded. Closed taxonomy: chosen must be one of
    the candidate ids, or one of the two refusal verdicts.
    """
    if not candidates:
        return RerankResult(
            chosen=None, chosen_match=None,
            confidence=0.0, reasoning="no candidates supplied",
            verdict="NO_MATCH", from_cache=False,
        )

    candidates = tuple(candidates[:RERANK_TOP_K])
    by_id = {m.catalog_item_id: m for m in candidates}

    with _tracer.start_as_current_span(
        "uc08.rerank.call",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.candidate_count": len(candidates),
            "uc08.top1_cosine": candidates[0].cosine_score,
        },
    ) as span:
        # 1. Cache lookup
        key = None
        if cache is not None:
            key = _cache_key(
                tenant_id=tenant_id, sr_text=sr_text, candidates=candidates,
            )
            hit = await _rerank_from_cache(cache, key, by_id)
            if hit is not None:
                return hit

        # 2. Build prompt
        sys_prompt = compose(
            Profile.FEATURE_AGENT_JSON,
            extra_sections=[_RERANK_INSTRUCTION],
        )
        user_prompt = _build_user_prompt(
            sr_text=sr_text, candidates=candidates,
        )

        # 3. LLM call (bounded by timeout via the gateway/transport layer;
        # the gateway exposes its own retry budget).
        try:
            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage(role="system", content=sys_prompt),
                    LlmMessage(role="user", content=user_prompt),
                ),
                model=RERANK_MODEL,
                tenant_id=tenant_id,
                user_id=user_id,
                temperature=0.0,
                max_tokens=200,
                response_format=ResponseFormat.JSON,
            ))
        except Exception as exc:                     # noqa: BLE001
            raise CatalogSearchError(
                f"rerank gateway failure: {type(exc).__name__}: {exc}",
            ) from exc

        # 4. Parse + validate against closed taxonomy
        text = (resp.content or "").strip().lstrip("`").rstrip("`")
        if text.startswith("json"):
            text = text[4:].strip()
        try:
            parsed = json.loads(text)
        except Exception as exc:                     # noqa: BLE001
            _log.warning("uc08.rerank.parse_failed",
                         tenant_id=tenant_id,
                         content_head=text[:120], error=str(exc)[:80])
            return RerankResult(
                chosen=None, chosen_match=None,
                confidence=0.0,
                reasoning="reranker output unparseable",
                verdict="NO_MATCH", from_cache=False,
            )

        # New schema (reasoning first, answer last — CoT chain-of-thought).
        reasoning = str(parsed.get("reasoning", ""))[:280]
        verdict, chosen, chosen_match, confidence = _classify_verdict(
            parsed, by_id, tenant_id)

        span.set_attribute("uc08.rerank.verdict", verdict)
        span.set_attribute("uc08.rerank.confidence", confidence)
        span.set_attribute("uc08.rerank.chosen",
                           chosen or "")

        # 5. Cache result (validated, not raw LLM output)
        if cache is not None:
            try:
                await cache.set(
                    key,
                    json.dumps({
                        "chosen": chosen,
                        "confidence": confidence,
                        "reasoning": reasoning,
                        "verdict": verdict,
                    }),
                    ttl_seconds=RERANK_CACHE_TTL_S,
                )
            except Exception:                        # noqa: BLE001
                pass  # cache failures are non-fatal

        # Production metrics for Grafana panels.
        _metric_inc("ai.uc08.rerank.total", 1,
                    tenant_id=tenant_id,
                    verdict=verdict,
                    auto_pick="true" if confidence >= 0.85 else "false")
        _metric_inc("ai.agent.runs.total", 1,
                    agent_id="uc08_fulfillment",
                    tenant_id=tenant_id,
                    source="catalog_rerank",
                    status="success" if verdict in ("CHOSEN", "WRONG_INTENT") else "no_match")

        _log.info("uc08.rerank.completed",
                  tenant_id=tenant_id,
                  verdict=verdict, confidence=confidence,
                  chosen=chosen, top1_cosine=candidates[0].cosine_score,
                  reasoning=reasoning[:120])

        # Langfuse: the listwise rerank decision (input = request + candidates,
        # output = verdict/chosen/why) as a visible stage; the LLM call itself
        # nests as a generation under this span via the gateway.
        set_langfuse_io(
            span,
            input={"request": sr_text,
                   "candidates": [m.catalog_item_id for m in candidates]},
            output={"verdict": verdict, "chosen": chosen,
                    "confidence": confidence, "reasoning": reasoning})
        return RerankResult(
            chosen=chosen, chosen_match=chosen_match,
            confidence=confidence, reasoning=reasoning,
            verdict=verdict, from_cache=False,
        )


__all__ = [
    "RerankResult",
    "rerank",
    "should_rerank",
    "RERANK_FLOOR",
    "RERANK_CEILING",
    "RERANK_TOP_K",
    "RERANK_CONFIDENCE_FLOOR",
]
