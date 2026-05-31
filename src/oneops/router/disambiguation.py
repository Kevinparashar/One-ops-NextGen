"""Stage 4 — disambiguation over the surviving candidates.

After stages 1–3 the candidate set is small (typically 1–5) and every member
is tenant/role-eligible and condition-viable. Stage 4 chooses. It is a
`Disambiguator` Protocol with two real implementations:

  * `LlmDisambiguator` — production: one small LLM call over only the
    survivors, returning **structured, schema-validated** output
    `{selected_agent_ids, parameters, intents, confidence}` (Moveworks
    "structured outputs as enforcement"). The LLM only disambiguates an
    already-narrowed set — it never sees the full catalogue.
  * `ThresholdDisambiguator` — deterministic: selects the top retrieval
    candidate when its score clears a confidence floor, else returns
    `no_confident_match`. Needs no LLM; backs the unit suite and local dev.

Either way the output is the same `Disambiguation` value, so the router's
funnel logic is identical regardless of which is wired.
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass
from typing import Any, Protocol

from oneops.observability import get_tracer
from oneops.router.retrieval import Candidate

_tracer = get_tracer("oneops.router.disambiguation")

# Deterministic preroute vocabulary. Narrowed to ONLY the two patterns
# every routing spec agrees on:
#   P1: a bare entity id alone → entity-summary agent
#   P2: an explicit documentation noun + entity id → KB agent
# Ambiguous words ("information", "data", "details", "available", "know",
# "what do we know") are DELIBERATELY excluded — they go to the semantic
# router because their meaning depends on phrasing context.
_DOC_NOUNS = frozenset({
    "docs", "doc", "document", "documents",
    "kb", "article", "articles",
    "runbook", "runbooks",
    "playbook", "playbooks",
    "sop", "sops",
})
# Multi-word doc phrases that don't survive simple word tokenisation
_DOC_PHRASES = (
    "knowledge base", "troubleshooting guide", "troubleshooting guides",
)
_ENTITY_ID_RE = _re.compile(r"\b([A-Za-z]{2,4}\d{6,})\b")
_BARE_ID_RE = _re.compile(r"^\s*[A-Za-z]{2,4}\d{6,}\s*$")


@dataclass(frozen=True)
class Disambiguation:
    """Stage-4 result. `selected_agent_ids` empty ⇒ no confident match."""

    selected_agent_ids: tuple[str, ...] = ()
    parameters_by_agent: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    intents: tuple[str, ...] = ()           # classified intent tokens (audit + re-eval)
    confidence: float = 0.0
    rationale: str = ""

    @property
    def is_confident_match(self) -> bool:
        return len(self.selected_agent_ids) > 0

    def params_for(self, agent_id: str) -> dict[str, str]:
        for aid, params in self.parameters_by_agent:
            if aid == agent_id:
                return dict(params)
        return {}

    @staticmethod
    def no_match(rationale: str) -> Disambiguation:
        return Disambiguation(rationale=rationale)

    @staticmethod
    def select(agent_ids: list[str], *, confidence: float, rationale: str = "",
               intents: list[str] | None = None,
               parameters: dict[str, dict[str, str]] | None = None) -> Disambiguation:
        params = parameters or {}
        return Disambiguation(
            selected_agent_ids=tuple(agent_ids),
            parameters_by_agent=tuple(
                (aid, tuple(sorted(params.get(aid, {}).items())))
                for aid in agent_ids
            ),
            intents=tuple(intents or ()),
            confidence=confidence,
            rationale=rationale,
        )


class Disambiguator(Protocol):
    async def disambiguate(
        self, query_text: str, candidates: list[Candidate], *, request_ctx: dict
    ) -> Disambiguation:
        ...


class ThresholdDisambiguator:
    """Deterministic disambiguator — picks the top candidate when its score
    clears `confidence_floor`, else `no_confident_match`. Real logic, no LLM."""

    def __init__(self, *, confidence_floor: float = 0.34) -> None:
        self._floor = confidence_floor

    async def disambiguate(
        self, query_text: str, candidates: list[Candidate], *, request_ctx: dict
    ) -> Disambiguation:
        if not candidates:
            return Disambiguation.no_match("no candidates survived the funnel")
        top = candidates[0]
        if top.score < self._floor:
            return Disambiguation.no_match(
                f"top candidate '{top.agent_id}' score {top.score:.2f} "
                f"is below the confidence floor {self._floor}"
            )
        return Disambiguation.select(
            [top.agent_id], confidence=top.score,
            rationale=f"top-scoring candidate above the {self._floor} floor",
        )


_DISAMBIGUATE_PROMPT = """You route an ITSM/ITOM query to the right agent(s) \
from a short candidate list. Output is strict JSON only.

## The ONE semantic test

Ask yourself: **what is the user trying to achieve with this query?**

  AXIS A — **Understand the entity / record ITSELF.**
  The user wants facts ABOUT the record: what it is, its summary,
  description, status, priority, severity, owner, assignee, SLA, impact,
  urgency, related changes, current known state, what happened, what is
  going on, what we know about it, its background, context, history. A
  bare entity id alone ("INC0001001", "CI0000001") falls here — the user
  is referencing the record itself.
  → route to the entity-summary agent (renders the record's own fields).

  AXIS B — **Find supporting knowledge OUTSIDE the entity.**
  The user wants material that HELPS understand, resolve, or troubleshoot:
  documents, KB articles, runbooks, playbooks, SOPs, procedures,
  troubleshooting guides, fixes, workarounds, known issues, previous
  resolutions, internal write-ups, reference material, "how do I fix",
  "how was this solved", "what should I follow", "any guidance",
  "anything documented", "what info is available for". Topic-only
  questions with no entity ("how do I fix VPN", "MFA reset procedure")
  also fall here.
  → route to the KB / knowledge-content agent.

  AXIS C — **Both.** The query contains BOTH an axis-A ask AND an axis-B
  ask. Return both agents in the order [entity-summary, KB].

  AXIS D — **Off-domain.** The query is not about an ITSM/ITOM record,
  service, or operational task at all (jokes, weather, chit-chat).
  Return no agents.

## The hard distinction (the one users get wrong)

The trap: "what do we know about INC0001001" and "what info is available
for INC0001001" look almost identical, but they ask different things.

  • "what do we know about X" — asks for facts ABOUT the entity itself.
    This is axis A (entity-summary). The user is asking US to summarise
    what we have on the record.
  • "what info is available for X" — asks what KNOWLEDGE material exists
    for X. This is axis B (KB). The user is asking what supporting
    documentation has been written about / linked to X.

Mental model: "details OF X" / "details ABOUT X" / "what do we know
about X" → entity itself (axis A). "info available FOR X" / "anything
written up ON X" / "docs FOR X" / "any data regarding X" → external
material (axis B).

Second trap — "root cause" / "RCA" / "the affected CI". These look
like knowledge phrases but they refer to FIELDS stored on the record
(a problem record has its own root_cause field; a change record has
its own affected_ci field). They are axis A field-reads, not axis B
KB lookups. Anything asking for the VALUE of a specific attribute or
linked-record-id of the focus is axis A, regardless of the noun used.

The distinction is in what the user wants RETURNED:
  - axis A returns the record's own fields
  - axis B returns KB articles linked to / about the record

## Contrastive examples (apply the PRINCIPLE, do not match strings)

Axis A — entity itself (→ entity-summary agent):
  • "summarize INC0001001"
  • "describe INC0001001"
  • "details of INC0001001"
  • "details about INC0001001"
  • "what do we know about INC0001001"
  • "tell me about CI0000001"
  • "walk me through INC0001001"
  • "explain this incident"
  • "what happened in INC0001001"
  • "what is going on with INC0001001"
  • "give me the ticket context"
  • "why was INC0001001 raised"
  • "what is the priority / status / SLA / owner / category / impact / urgency / severity / state of INC0001001"
  • "who is INC0001001 assigned to"
  • "INC0001001"                            (bare id)
  • "CI0000001"                             (bare id)

  Chained linked-record field-reads — also axis A:
  The record's own linked-record-id fields (related_problem,
  related_changes, affected_ci, parent_incident, linked_kb) are
  STORED ON THE RECORD itself. When the user asks for the VALUE
  of a linked field — even with paraphrases like "the linked X",
  "the related X", "the affected X", "its X" — that is a chained
  field-read on the record. It is axis A, NOT a KB search. The
  entity-summary agent resolves the link and reads the linked
  record's field. KB content lives elsewhere; these queries want
  the record's own linkage value.
    • "any related changes for INC0001001"
    • "the related problem"  / "what is the related problem"
    • "the linked change" / "the linked problem"
    • "the affected CI" / "criticality of the affected CI"
    • "status of the linked problem"
    • "priority of the linked problem"
    • "owner of the related problem" / "who owns the linked X"
    • "risk level of the linked change"
    • "root cause" / "RCA" / "what is the root cause"   (a PBM's
      own root_cause field is part of the entity record)

Axis B — supporting material (→ KB agent):
  • "any docs for INC0001001"
  • "any runbooks for INC0001001"
  • "any guidance for CI0000001"
  • "anything written up on CI0000001"
  • "available documents linked to INC0001001"
  • "known issues for VPN handoff"
  • "info available for INC0001001"
  • "what data do we have for CI0000001"
  • "is there anything in our database for INC0001001"
  • "do we have any information related to INC0001001"
  • "has anyone documented this before"
  • "is there a playbook for this"
  • "what should I follow for this issue"
  • "how was this solved earlier"
  • "find supporting material for this incident"
  • "what can help me resolve VPN handoff"
  • "is there any internal knowledge on this"
  • "how do I fix VPN"   (topic only, no entity)
  • "MFA reset procedure"

Axis C — both (→ [entity-summary, KB]):
  • "summarize INC0001001 and any docs for it"
  • "details of INC0001001 and do we have any data regarding this"

Axis D — off-domain (→ []):
  • "tell me a joke"
  • "what's the weather"

## Decision procedure — capability-driven dispatch

For each ask in the query, identify two things explicitly:

  • OBJECT — what the user is asking about. Either a specific instance
    (an identified record, or the current focused record when the query
    is a follow-up), or a class of things (a topic, technology, service,
    symptom, operational concern).

  • ANSWER-SOURCE — where the answer must come from. Either:
        ─ STORED-ATTRIBUTE — the answer is the value of a field held on
          the specific record itself. The agent reads the record's own
          data.
        ─ AUTHORED-MATERIAL — the answer is a separately authored
          resource about the object: a write-up, procedure, guideline,
          troubleshooting steps, documented fix, known-issue note. The
          agent retrieves authored content distinct from any single
          record's stored fields.

The answer-source is determined by what the user NEEDS, not by phrasing.
A direct question ("how do I fix X") and a meta question ("any material
on X", "is there a procedure documented for X", "what do we have on X",
"where is the write-up for X") both resolve to AUTHORED-MATERIAL — the
user needs authored content, regardless of how they asked for it.

Route by matching (OBJECT × ANSWER-SOURCE) to the candidate whose
capability description fits that pair. The agent catalog above the
routing block describes each candidate's capability. Select the candidate
whose description aligns; do not invent agents not listed.

When the query contains BOTH a stored-attribute ask AND an
authored-material ask, return both agents, ordered with the stored-
attribute reader first.

## Dispatch discipline

Returning no agents means the user is refused before any agent runs.
Reserve that outcome for queries with no IT/ITSM/ITOM object at all —
casual conversation, off-topic chat. When the query has any in-domain
object — a record, a technology, a service, a symptom, an operational
topic — at least one in-domain agent should be selected. Each agent
reports its own no-result when its lookup yields nothing; that is the
correct place to surface "no match", not the router.

When the candidate set includes the authored-material agent and you are
uncertain whether the user needs stored-attribute or authored-material,
prefer the authored-material agent. Its no-result reply is honest and
recoverable; a router-level refusal is not.

## Output schema (STRICT JSON only)

{"selected_agent_ids":["..."],
 "intents":["..."],
 "confidence":0.0-1.0,
 "rationale":"<one short sentence: axis (A/B/C/D) + the ask that decided it>"}

The `intents` field uses tokens from: summary, field_read, kb_search, \
kb_article_fetch, similar_search, action, off_domain."""


def _deterministic_preroute(
    query_text: str, valid_ids: set[str]
) -> tuple[str, str, str] | None:
    """Narrow deterministic preroute — only patterns ALL routing specs agree
    on. Case-insensitive. Returns (agent_id, intent_token, rationale) on a
    hit, else None (fall through to the semantic router).

    Rules — first match wins:
      P1. Bare entity id alone ('INC0001001', 'ci0000001') → uc01_summarization
      P2. Entity present + explicit DOC NOUN (docs / KB / runbook / playbook /
          SOP / article / knowledge base / troubleshooting guide)
          → uc03_kb_lookup

    Deliberately NOT prerouted (LLM semantic router decides):
      - "what do we know about X" — ambiguous, asks about entity
      - "information / data / details / available" — context-dependent
      - field-read attributes (priority / status / owner / SLA)
      - linked-field reads ("the related X", "any related X")
      - summary verbs (summarize / describe / overview)
      - off-domain queries
    """
    q = (query_text or "").strip()
    if not q:
        return None
    if _BARE_ID_RE.match(q) and "uc01_summarization" in valid_ids:
        return ("uc01_summarization", "summary",
                "P1: bare entity id → entity-summary agent")
    has_entity = bool(_ENTITY_ID_RE.search(q))
    if not has_entity:
        return None
    q_lower = q.lower()
    words = set(_re.findall(r"[a-z][a-z\-]*", q_lower))
    if words & _DOC_NOUNS and "uc03_kb_lookup" in valid_ids:
        return ("uc03_kb_lookup", "kb_search",
                "P2: explicit doc-noun + entity id → KB agent")
    if any(p in q_lower for p in _DOC_PHRASES) \
            and "uc03_kb_lookup" in valid_ids:
        return ("uc03_kb_lookup", "kb_search",
                "P2: explicit doc-phrase + entity id → KB agent")
    return None


class LlmDisambiguator:
    """Production disambiguator — one gateway call over the surviving
    candidates only. Returns schema-validated structured output. A call/parse
    failure yields `no_confident_match` — never a guessed route.

    The LLM is given each candidate's registry description so it can decide
    based on what the agent actually does, not the opaque agent_id. Without
    the description the LLM was reduced to guessing from id semantics
    (`uc03_kb_lookup` sounds like "topic lookup"), which misrouted linked-
    record phrases like "any related changes". The descriptions are pulled
    from the same RegistryService the router uses, so they stay in lock-step
    with what agents are actually active — no separate catalogue to maintain.
    """

    def __init__(self, gateway, *, model: str = "gpt-4o-mini",
                 registry: Any = None) -> None:
        self._gateway = gateway
        self._model = model
        # Optional; when None the disambiguator falls back to id-only listings
        # (preserves the pre-2026-05-28 behaviour for callers that haven't
        # been wired through yet).
        self._registry = registry
        # Catalog block — built lazily once and embedded in the cached system
        # prompt so Anthropic's prompt cache reuses descriptions across every
        # routing call. Dragonfly FLUSHALL never touches it; only a process
        # restart (which is also when the registry would reload) rebuilds it.
        self._catalog_block: str | None = None

    def _describe(self, agent_id: str) -> str:
        """Look up the agent's registry description. Returns '' on miss so
        listings degrade gracefully — never raises into the router."""
        if self._registry is None:
            return ""
        try:
            agent = self._registry.agents.get_optional(agent_id)
        except Exception:                                       # noqa: BLE001
            return ""
        return (agent.description or "").strip() if agent is not None else ""

    def _build_catalog(self) -> str:
        """One-time build of the full agent catalog: every active agent's
        id + description, formatted for inclusion in the cached system
        prompt. None when registry is unwired (legacy callers)."""
        if self._registry is None:
            return ""
        try:
            ids = sorted(self._registry.agents.list_ids())
        except Exception:                                       # noqa: BLE001
            return ""
        lines: list[str] = ["## Agent catalog (stable; cached in system prompt)"]
        for aid in ids:
            desc = self._describe(aid)
            if not desc:
                continue
            lines.append(f"\n### {aid}\n{desc}")
        return "\n".join(lines)

    async def disambiguate(
        self, query_text: str, candidates: list[Candidate], *, request_ctx: dict
    ) -> Disambiguation:

        with _tracer.start_as_current_span(
            "router.stage4.disambiguate",
            attributes={
                "oneops.router.stage": "4",
                "oneops.router.candidate_count": len(candidates),
                "oneops.router.candidate_ids": ",".join(
                    sorted(c.agent_id for c in candidates)),
                "oneops.router.model": self._model,
            },
        ) as span:
            return await self._disambiguate_inner(
                query_text, candidates, request_ctx=request_ctx, _span=span)

    async def _disambiguate_inner(
        self, query_text: str, candidates: list[Candidate], *,
        request_ctx: dict, _span,
    ) -> Disambiguation:
        import json

        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.observability import get_logger
        from oneops.policy import Profile, compose

        if not candidates:
            return Disambiguation.no_match("no candidates survived the funnel")
        valid_ids = {c.agent_id for c in candidates}
        # Deterministic preroute — high-confidence patterns the LLM was found
        # to mishandle. Field-read attributes intentionally NOT short-circuited
        # (they fall through so the original LLM rule-4 logic runs unchanged).
        # We pre-check against the FULL active-agent set (not just stage-3
        # survivors), because the preroute patterns are themselves the routing
        # contract for these cases; the lexical retriever can drop a target
        # agent for short queries (e.g. bare CI id), but the contract still
        # applies. We never invent an agent — registry must list it active.
        all_active_ids: set[str] = set()
        if self._registry is not None:
            try:
                all_active_ids = set(self._registry.agents.list_ids())
            except Exception:                                   # noqa: BLE001
                all_active_ids = set()
        preroute_pool = all_active_ids or valid_ids
        preroute = _deterministic_preroute(query_text, preroute_pool)
        if preroute is not None:
            agent_id, intent, rationale = preroute
            _span.set_attribute("oneops.router.preroute.fired", True)
            _span.set_attribute("oneops.router.preroute.target", agent_id)
            _span.set_attribute("oneops.router.preroute.rationale", rationale)
            _span.set_attribute("oneops.router.selected", agent_id)
            return Disambiguation.select(
                [agent_id], confidence=0.95,
                rationale=rationale, intents=[intent])
        _span.set_attribute("oneops.router.preroute.fired", False)
        # User block carries only what changes per call: the query and the
        # surviving candidate ids. Descriptions live in the cached system
        # prompt (catalog block) — they are stable across every call and
        # benefit from Anthropic prompt-cache reuse.
        listing = "\n".join(
            f"- {c.agent_id} (retrieval score {c.score:.2f})" for c in candidates)
        # Stage 2 (2026-05-28): when the conversation has an active focus
        # entity (carried in the LangGraph state via request_ctx), surface
        # it to the disambiguator so it has the correct prior. A
        # follow-up query like "what is the root cause" anchored on a PBM
        # record is overwhelmingly a record-field read (axis A), not a KB
        # search — without this signal the LLM looks at the words alone
        # and can probabilistically drift to UC-3.
        focus_id = (request_ctx.get("focus_entity_id") or "").strip()
        focus_service = (request_ctx.get("focus_service_id") or "").strip()
        focus_block = ""
        if focus_id:
            focus_block = (
                f"\n\nACTIVE FOCUS (the user is mid-conversation about):\n"
                f"  entity_id: {focus_id}\n"
                f"  service:   {focus_service or 'unknown'}\n"
                f"Routing principle with active focus — apply rigorously:\n"
                f"\n"
                f"  The focus is a PRIOR for ambiguous follow-ups, NOT a "
                f"sticky override of explicit intent. A user can switch "
                f"intent mid-conversation without restating context.\n"
                f"\n"
                f"  Test the query in this order:\n"
                f"\n"
                f"  1. Does the query ask for the VALUE of a field, "
                f"     attribute, or linked-record-id that is STORED on "
                f"     the focused entity itself? The answer must come "
                f"     from the focused record's own data.\n"
                f"     YES → axis A. The focused record supplies the value.\n"
                f"\n"
                f"  2. Does the query ask for external knowledge material — "
                f"     content authored as a separate written-up resource "
                f"     about a topic, technology, service, or problem area? "
                f"     The answer must come from documents written outside "
                f"     the focused record. The query references a topic "
                f"     scope, not a stored attribute of the focused record, "
                f"     even when that topic overlaps with what the focused "
                f"     record concerns.\n"
                f"     YES → axis B. Explicit knowledge intent overrides "
                f"     the focus prior. Do not re-render the focused record "
                f"     when the user is asking for documented knowledge.\n"
                f"\n"
                f"  3. If neither applies — the query is genuinely "
                f"     ambiguous and has no explicit knowledge cue — fall "
                f"     back to the focused record's type (axis A).\n"
                f"\n"
                f"  Apply the principle. The focused entity's topical "
                f"  keywords appearing inside a knowledge-content query "
                f"  do NOT convert it to axis A.\n"
            )
        user_block = (
            f"Query:\n{query_text}{focus_block}\n\n"
            f"Candidate agents (look up each id in the Agent catalog "
            f"section of the system prompt for its description):\n{listing}"
        )
        # Lazy-build the catalog once (process lifetime) and stitch it into the
        # cached system prompt. Order: policy prefix → routing rules →
        # full agent catalog.
        if self._catalog_block is None:
            self._catalog_block = self._build_catalog()
        extra_sections = [_DISAMBIGUATE_PROMPT]
        if self._catalog_block:
            extra_sections.append(self._catalog_block)
        system_prompt = compose(Profile.INTERNAL_AGENT,
                                extra_sections=extra_sections)
        try:
            response = await self._gateway.call(LlmRequest(
                # System block is the policy-composed prefix + disambiguation
                # rules — large + stable across every routing decision.
                # Prompt-cache for ~50-90% input-token savings per turn.
                messages=(LlmMessage("system", system_prompt,
                                     cache_control=True),
                          LlmMessage("user", user_block)),
                model=self._model,
                tenant_id=request_ctx.get("tenant_id") or "_unknown",
                user_id=request_ctx.get("user_id", "") or "",
                response_format=ResponseFormat.JSON,
                request_id=request_ctx.get("request_id", "")))
            doc = json.loads(response.content)
            # Closed-class guard — only agents that were actually offered may
            # be selected; an LLM-invented id is dropped (ISS-003 discipline).
            selected = [a for a in (doc.get("selected_agent_ids") or [])
                        if a in valid_ids]
            if not selected:
                # Retriever-as-floor + LLM-as-refiner (canonical production
                # router pattern; cf. Aurelio Semantic Router, LangGraph
                # Supervisor). The stage-3 retriever is authoritative: when
                # it produced a non-empty survivor set, the stage-4 LLM is
                # allowed to refine that set or re-rank it — never to empty
                # it. An LLM hedge on telegraphic / under-specified queries
                # is exactly the failure mode this rule mitigates: the user
                # asked an in-domain question, the retrieval already
                # surfaced a relevant agent, the LLM only failed to commit
                # — let the agent run and report its own no-result, do not
                # silently refuse at the supervisor.
                #
                # Fires only when survivors exist and the top has signal
                # above floor; for genuinely off-domain queries the
                # retriever returns no survivors → boundary handles them.
                if candidates:
                    top = max(candidates, key=lambda c: c.score)
                    if top.score >= 0.10:        # narrow floor, matches MIN_FUSED_SCORE on retriever side
                        _span.set_attribute("oneops.router.selected", top.agent_id)
                        _span.set_attribute("oneops.router.dispatch_reason",
                                            "retriever_floor_llm_hedge")
                        _span.set_attribute("oneops.router.llm_hedge_rationale",
                                            str(doc.get("rationale") or "")[:160])
                        return Disambiguation.select(
                            [top.agent_id], confidence=float(top.score),
                            rationale=(
                                "supervisor dispatch-by-default: stage-3 "
                                "retriever surfaced this agent above the "
                                "signal floor; stage-4 LLM hedged "
                                "(no_match). The agent reports its own "
                                "no-result if its lookup yields nothing."),
                            intents=["kb_search"] if top.agent_id.endswith("_kb_lookup") else [])
                _span.set_attribute("oneops.router.selected", "")
                return Disambiguation.no_match(
                    str(doc.get("rationale") or "no candidate matched the intent"))
            _span.set_attribute("oneops.router.selected", ",".join(selected))
            _span.set_attribute("oneops.router.confidence",
                                float(doc.get("confidence") or 0.0))
            return Disambiguation.select(
                selected, confidence=float(doc.get("confidence") or 0.0),
                rationale=str(doc.get("rationale", "")),
                intents=list(doc.get("intents") or []))
        except (LLMGatewayError, ValueError, KeyError, TypeError) as exc:
            _span.set_attribute("oneops.router.error", str(exc)[:120])
            get_logger("oneops.router.disambiguation").warning(
                "disambiguator.llm_failed", error=str(exc))
            return Disambiguation.no_match(f"disambiguation failed: {exc}")


__all__ = [
    "Disambiguation", "Disambiguator", "ThresholdDisambiguator", "LlmDisambiguator",
]
