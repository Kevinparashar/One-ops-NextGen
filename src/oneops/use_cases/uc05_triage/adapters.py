"""Phase 1 — gateway-backed adapters for UC-5 tools.

Replaces the raw httpx callables (embed_fn, tiebreak_fn, tag_fn, infer_fn)
used during demo/test with production-grade ones that go through:

  • oneops.llm.gateway.LlmGateway — single egress, per-tenant cost,
    retries, fallback model, replay-cache, redaction, llm.embed/llm.call spans
  • oneops.policy.composer.compose(Profile.X, ...) — every prompt carries
    safety + tenant + RBAC + JSON-output policy blocks

The tool functions (check_duplicate_candidates, recommend_assignment,
prioritize_entity) keep their existing pluggable callable signatures —
this module just provides production-grade factories that build those
callables from a gateway + tenant context.

Profile mapping (locked 2026-05-29):
  tiebreak_fn  → Profile.FEATURE_AGENT          (returns plain string)
  tag_fn       → Profile.FEATURE_AGENT_JSON     (returns JSON list)
  infer_fn     → Profile.FEATURE_AGENT_JSON     (returns JSON object)
  embed_fn     → no policy (embeddings are not generative)
"""
from __future__ import annotations

import json
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import get_tracer
from oneops.policy.composer import Profile, compose

_tracer = get_tracer("oneops.uc05_triage.adapters")

# Repeated literals → constants (sonar S1192).
_ONEOPS_TENANT_ID = "oneops.tenant_id"
_UC05_PROPOSE_OUTCOME = "uc05.propose.outcome"

# Default models — match what UC-3 already uses against LiteLLM
DEFAULT_EMBED_MODEL = "text-embedding-3-large"
DEFAULT_EMBED_DIMENSIONS = 1536
DEFAULT_CHAT_MODEL = "gpt-4o-mini"


# ── Embed factory ────────────────────────────────────────────────────────────

def make_embed_fn(
    gateway: LlmGateway, *, tenant_id: str, user_id: str = "",
    model: str = DEFAULT_EMBED_MODEL, dimensions: int = DEFAULT_EMBED_DIMENSIONS,
):
    """Returns an async embed_fn(text, *, tenant_id, user_id) -> list[float]."""

    async def _embed(text: str, *, tenant_id: str = tenant_id,
                     user_id: str = user_id) -> list[float]:
        vecs = await gateway.embed(
            [text], model=model, tenant_id=tenant_id, user_id=user_id,
            dimensions=dimensions,
        )
        return vecs[0]

    return _embed


# ── Tiebreak factory (LLM chooses among kNN-split candidates) ────────────────

_TIEBREAK_INSTRUCTION = (
    "TASK: pick the most semantically appropriate value for a field on an IT "
    "ticket. You are given the ticket title + description, the candidate "
    "values that similar past tickets used, and 1-2 example titles per "
    "candidate. Choose the ONE value whose examples best match the new "
    "ticket. Reply with ONLY the chosen value — no prose, no quotes."
)


_PROPOSE_INSTRUCTION = (
    "TASK: propose a value for a field on an IT ticket when neighbour voting "
    "produced no clear winner. You are given the ticket title + description, "
    "the field name, and (optionally) the historical value pool for guidance.\n\n"
    "Rules:\n"
    "1. If a HISTORICAL POOL is provided, strongly prefer a value from it — "
    "those are the categories/groups already in use in this tenant.\n"
    "2. If the pool is empty OR no value fits the ticket, you may propose a "
    "new short value following the same naming convention (lowercase for "
    "categories, GRP-* for assignment groups).\n"
    "3. Use the ticket's *primary topic* (the failure mode / request type), "
    "not surface words.\n"
    "4. Report a confidence in {high, medium, low}:\n"
    "   • high   — ticket is unambiguous and the value is clearly correct\n"
    "   • medium — value fits but there's some interpretation\n"
    "   • low    — best guess; UI should escalate to human\n\n"
    "Return STRICT JSON: {\"value\":\"<short value>\","
    "\"confidence\":\"high|medium|low\",\"rationale\":\"<one short sentence>\"}"
)


def make_tiebreak_fn(
    gateway: LlmGateway, *, tenant_id: str, user_id: str = "",
    model: str = DEFAULT_CHAT_MODEL,
):
    """Returns an async tiebreak_fn matching tools.check_duplicates.TiebreakFn."""

    async def _tiebreak(*, probe_text: str, field: str,
                        candidates: list[dict[str, Any]],
                        ticket_row: dict[str, Any]) -> str | None:
        with _tracer.start_as_current_span(
            "uc05.adapter.tiebreak",
            attributes={_ONEOPS_TENANT_ID: tenant_id, "uc05.field": field,
                        "uc05.candidate_count": len(candidates)},
        ):
            cand_block = "\n".join(
                f"  • {c['value']}  (vote_count={c['vote_count']})"
                + (f"\n     example: \"{c['example_titles'][0]}\""
                   if c.get('example_titles') else "")
                for c in candidates
            )
            user = (
                f"NEW TICKET:\n  Title: {ticket_row.get('title','')}\n"
                f"  Description: {ticket_row.get('description','')}\n\n"
                f"FIELD: {field}\n\nCANDIDATES:\n{cand_block}"
            )
            sys_prompt = compose(
                Profile.FEATURE_AGENT,
                extra_sections=[_TIEBREAK_INSTRUCTION],
            )
            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage(role="system", content=sys_prompt),
                    LlmMessage(role="user", content=user),
                ),
                model=model,
                tenant_id=tenant_id, user_id=user_id,
                temperature=0.0, max_tokens=30,
            ))
            choice = (resp.content or "").strip().strip('"').strip("'")
            return choice or None

    return _tiebreak


# ── Propose factory (LLM proposes a field value when kNN can't) ──────────────
#
# Fallback path for Tool 1 when neighbour voting yields no value or a
# below-floor majority. Returns `{value, confidence, rationale}` or None.
#
# Confidence mapping (str → float):
#   high   = 0.80
#   medium = 0.55
#   low    = 0.30
#
# The two intentional choices:
#   • Discrete buckets, not raw floats — calibrates to LLM self-reports
#     consistently; raw float self-reports tend to skew optimistic.
#   • Cap at 0.80 — when only the LLM proposes (no neighbour support),
#     the overall confidence shouldn't reach auto-apply territory (0.90).
_CONFIDENCE_STR_TO_FLOAT: dict[str, float] = {
    "high": 0.80,
    "medium": 0.55,
    "low": 0.30,
}


def make_propose_fn(
    gateway: LlmGateway, *, tenant_id: str, user_id: str = "",
    model: str = DEFAULT_CHAT_MODEL,
):
    """Returns an async propose_fn matching tools.check_duplicates.ProposeFn.

    Signature: `(probe_text, field, ticket_row, pool) -> dict|None`
    where the dict is `{"value": str, "confidence": float, "rationale": str}`.
    Returns None on parse/model failure — caller falls back to the kNN
    result with confidence=0 (or empty if no votes).
    """

    async def _propose(*, probe_text: str, field: str,
                        ticket_row: dict[str, Any],
                        pool: list[str]) -> dict[str, Any] | None:
        with _tracer.start_as_current_span(
            "uc05.adapter.propose",
            attributes={
                _ONEOPS_TENANT_ID: tenant_id,
                "uc05.field": field,
                "uc05.pool_size": len(pool),
            },
        ) as span:
            pool_block = (
                ", ".join(sorted(set(pool))[:30]) if pool else "(empty — propose a new value)"
            )
            user = (
                f"NEW TICKET:\n  Title: {ticket_row.get('title','')}\n"
                f"  Description: {ticket_row.get('description','')}\n\n"
                f"FIELD: {field}\n"
                f"HISTORICAL POOL: {pool_block}"
            )
            sys_prompt = compose(
                Profile.FEATURE_AGENT,
                extra_sections=[_PROPOSE_INSTRUCTION],
            )
            try:
                resp = await gateway.call(LlmRequest(
                    messages=(
                        LlmMessage(role="system", content=sys_prompt),
                        LlmMessage(role="user", content=user),
                    ),
                    model=model,
                    tenant_id=tenant_id, user_id=user_id,
                    response_format=ResponseFormat.JSON,
                    temperature=0.0, max_tokens=120,
                ))
                doc = json.loads(resp.content or "{}")
                value = (doc.get("value") or "").strip()
                conf_str = (doc.get("confidence") or "low").strip().lower()
                rationale = (doc.get("rationale") or "").strip()
                if not value:
                    span.set_attribute(_UC05_PROPOSE_OUTCOME, "empty_value")
                    return None
                confidence = _CONFIDENCE_STR_TO_FLOAT.get(conf_str, 0.30)
                span.set_attribute(_UC05_PROPOSE_OUTCOME, "ok")
                span.set_attribute("uc05.propose.value", value[:60])
                span.set_attribute("uc05.propose.confidence", confidence)
                return {
                    "value": value,
                    "confidence": confidence,
                    "rationale": rationale[:180],
                }
            except Exception as exc:                                # noqa: BLE001
                span.set_attribute(_UC05_PROPOSE_OUTCOME, "error")
                span.set_attribute("uc05.propose.error", str(exc)[:160])
                return None

    return _propose


# ── Tag factory (LLM extracts ITSM/ITOM domain tags) ─────────────────────────

_TAG_INSTRUCTION = (
    "TASK: extract 1-3 short ITSM/ITOM domain tags for an IT ticket. "
    "Tags are lowercase, distinct, single words or hyphenated terms.\n\n"
    "GOOD tags: vpn, tunnel, gateway, mailbox, exchange, outlook, kerberos, "
    "sso, mfa, printer, wi-fi, dhcp, dns, certificate, ssl, firewall, "
    "load-balancer, database, sql, deadlock, replication, kubernetes, pod, "
    "patch, license, onboarding, payroll, erp, salesforce, jira, slack, "
    "storage, latency, disk-full, cpu, memory, snmp, syslog, vmware.\n\n"
    "BAD tags (NEVER return): users, issue, ticket, please, new, affecting, "
    "working, problem, error, failure, cannot, doing, getting, started, "
    "multiple, business, team, request, help, support, finance, hr, floor, "
    "building, office, morning, today, yesterday, minutes, hours.\n\n"
    "RESPONSE FORMAT: reply with ONLY a JSON list like [\"vpn\", \"tunnel\", "
    "\"wi-fi\"]. No prose."
)


def make_tag_fn(
    gateway: LlmGateway, *, tenant_id: str, user_id: str = "",
    model: str = DEFAULT_CHAT_MODEL,
):
    """Returns an async tag_fn matching tools.check_duplicates.TagFn."""

    async def _tag(*, probe_title: str, probe_description: str,
                   neighbour_titles: list[str],
                   neighbour_descriptions: list[str],
                   candidate_pool: list[str]) -> list[str]:
        with _tracer.start_as_current_span(
            "uc05.adapter.tag",
            attributes={_ONEOPS_TENANT_ID: tenant_id,
                        "uc05.pool_size": len(candidate_pool)},
        ):
            nb = "\n".join(f"  • {t}" for t in neighbour_titles[:5])
            pool = ", ".join(candidate_pool[:15]) if candidate_pool else "(none)"
            user = (
                f"NEW TICKET:\n  Title: {probe_title}\n  Description: {probe_description}\n\n"
                f"SIMILAR PAST TICKETS:\n{nb}\n\n"
                f"PRE-FILTERED CANDIDATE WORDS (hint): {pool}"
            )
            sys_prompt = compose(
                Profile.FEATURE_AGENT_JSON,
                extra_sections=[_TAG_INSTRUCTION],
            )
            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage(role="system", content=sys_prompt),
                    LlmMessage(role="user", content=user),
                ),
                model=model,
                tenant_id=tenant_id, user_id=user_id,
                temperature=0.0, max_tokens=80,
                response_format=ResponseFormat.JSON,
            ))
            text = (resp.content or "").strip().lstrip("`").rstrip("`")
            if text.startswith("json"):
                text = text[4:].strip()
            try:
                parsed = json.loads(text)
            except Exception:
                return []
            return parsed if isinstance(parsed, list) else []

    return _tag


# ── Prioritize factory (LLM infers Motadata impact + urgency) ────────────────

_PRIORITIZE_INSTRUCTION = (
    "TASK: pick Motadata impact + urgency for an IT incident.\n\n"
    "IMPACT values (pick exactly one):\n"
    "  Low            (nuisance, no users blocked)\n"
    "  On Users       (a few users impacted, workaround exists)\n"
    "  On Department  (a whole department blocked, no workaround)\n"
    "  On Business    (mission-critical service down for the company)\n\n"
    "URGENCY values (pick exactly one):\n"
    "  Low            (can wait until next business day)\n"
    "  Medium         (needs attention within business hours)\n"
    "  High           (within 4 hours)\n"
    "  Urgent         (immediate — within 1 hour)\n\n"
    "RESPONSE FORMAT: reply with ONLY a JSON object like "
    "{\"impact\": \"On Department\", \"urgency\": \"High\"}. No prose."
)


def make_infer_fn(
    gateway: LlmGateway, *, tenant_id: str, user_id: str = "",
    model: str = DEFAULT_CHAT_MODEL,
):
    """Returns an async infer_fn matching tools.prioritize.InferFn."""

    async def _infer(*, service_id: str, ticket_row: dict[str, Any],
                     suggested_category: str | None = None,
                     suggested_subcategory: str | None = None,
                     suggested_service_name: str | None = None,
                     vip_flag: bool = False) -> dict[str, str]:
        with _tracer.start_as_current_span(
            "uc05.adapter.prioritize",
            attributes={_ONEOPS_TENANT_ID: tenant_id,
                        "uc05.service_id": service_id,
                        "uc05.vip": vip_flag},
        ):
            user = (
                f"TICKET:\n"
                f"  Title:       {ticket_row.get('title','')}\n"
                f"  Description: {ticket_row.get('description','')}\n"
                f"  Category:    {suggested_category or 'unknown'}\n"
                f"  Subcategory: {suggested_subcategory or 'unknown'}\n"
                f"  Service:     {suggested_service_name or 'unknown'}\n"
                f"  CI:          {ticket_row.get('ci_id','-')}\n"
                f"  VIP reporter: {vip_flag}"
            )
            sys_prompt = compose(
                Profile.FEATURE_AGENT_JSON,
                extra_sections=[_PRIORITIZE_INSTRUCTION],
            )
            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage(role="system", content=sys_prompt),
                    LlmMessage(role="user", content=user),
                ),
                model=model,
                tenant_id=tenant_id, user_id=user_id,
                temperature=0.0, max_tokens=60,
                response_format=ResponseFormat.JSON,
            ))
            text = (resp.content or "").strip().lstrip("`").rstrip("`")
            if text.startswith("json"):
                text = text[4:].strip()
            return json.loads(text)

    return _infer


__all__ = [
    "make_embed_fn", "make_tiebreak_fn", "make_tag_fn", "make_infer_fn",
    "DEFAULT_EMBED_MODEL", "DEFAULT_CHAT_MODEL",
]
