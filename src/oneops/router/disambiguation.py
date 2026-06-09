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

from oneops.observability import get_tracer, set_langfuse_io
from oneops.router.retrieval import Candidate

# Telemetry literals → constants (sonar S1192).
_ONEOPS_ROUTER_SELECTED = "oneops.router.selected"

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

## Card-first selection (AUTHORITATIVE — read this before the axes)

The axes above are a COARSE guide for the single most common confusion
(understand a record vs. find knowledge). They are NOT the full set of things
a user can ask for, and a query is NOT off-domain just because it fits none of
A/B/C/D. The AUTHORITATIVE scope of each candidate is its CARD — its
description + "Use when" + "Do NOT use" — provided with the query below.

Decide from the cards:
  • Select the candidate whose "Use when" covers the ask AND whose "Do NOT
    use" does not exclude it.
  • A query can match a candidate's "Use when" even when it fits NONE of axes
    A/B/C/D. Example: a request to OBTAIN, PROVISION, ORDER, REQUEST, or SET UP
    something new (software, a license, hardware, access, an account,
    onboarding) matches the fulfilment/catalog agent's card — though it is
    neither "understand a record" (A) nor "find knowledge" (B). Select it.
  • When the cards and the axes disagree, THE CARDS WIN. The axes never
    override a candidate whose "Use when" plainly covers the ask.
This is how new capabilities are routed without new axes: the card carries the
per-agent truth; you reason over the cards in the candidate list.

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
capability description fits that pair. Each candidate agent's card is
provided with the query (its description, Use when, and Do NOT use). Select
the candidate whose card fits; do not invent agents not listed.

Each candidate card also lists "Use when" (the agent's positive scope) and
"Do NOT use" (out-of-scope cases, each naming the agent to pick instead).
Treat these as DECISIVE: if the query matches a candidate's "Do NOT use"
clause, do NOT select that candidate — select the agent named in that clause
when it is in the candidate list. These boundaries are how same-entity
look-alikes are told apart — e.g. "how do I resolve INC0001001" carries a
record id but asks for authored guidance, so the summary/similar agents'
"Do NOT use" clauses send it to the KB agent.

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

# Appended ONLY in strict_fit mode (team member-selection). Overrides the
# always-route dispatch discipline above: the candidates are a fixed set, so a
# query whose intent matches none of them must be refused, not force-routed.
_STRICT_FIT_PROMPT = """## Fixed candidate set — fit test (overrides dispatch discipline)

The candidate agents above are a FIXED, externally-chosen set (a team's
members) — NOT a relevance-ranked shortlist. So you must judge FIT, not just
pick the closest:

- Select an agent ONLY if its capability genuinely matches the query's intent
  (its "Use when" covers this ask, and the ask is not in its "Do NOT use").
- If NONE of these specific candidates fit the query's intent, return an EMPTY
  selected_agent_ids — even when the query is a perfectly valid in-domain
  request. "This team has no member for this" is a correct, expected outcome;
  do NOT force-pick the least-bad candidate.
- This supersedes any earlier instruction to always route an in-domain query.

Example: a "summarize this incident" query offered only a triage agent and a
fulfilment agent → no fit → return []. Refusing lets the caller route it to the
right team instead of mis-handling it."""


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


def _build_focus_block(focus_id: str, focus_service: str) -> str:
    """The ACTIVE-FOCUS prompt section (the axis-A/B routing prior for
    follow-up queries). Empty string when the conversation has no focus
    entity, so the caller can concatenate it unconditionally."""
    if not focus_id:
        return ""
    return (
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


def _floor_dispatch(
    candidates: list[Candidate], doc: dict, span,
) -> Disambiguation | None:
    """Retriever-as-floor + LLM-as-refiner (canonical production router
    pattern; cf. Aurelio Semantic Router, LangGraph Supervisor). The stage-3
    retriever is authoritative: when it produced a non-empty survivor set, the
    stage-4 LLM may refine/re-rank it — never empty it. An LLM hedge on
    telegraphic / under-specified queries is exactly the failure mode this
    mitigates: the user asked an in-domain question, retrieval surfaced a
    relevant agent, the LLM only failed to commit — let the agent run and
    report its own no-result rather than refusing at the supervisor.

    Returns the dispatch decision, or None when no survivor clears the signal
    floor (the caller then emits no_match — off-domain queries land here).
    """
    if not candidates:
        return None
    top = max(candidates, key=lambda c: c.score)
    if top.score < 0.10:        # narrow floor, matches MIN_FUSED_SCORE on retriever side
        return None
    span.set_attribute(_ONEOPS_ROUTER_SELECTED, top.agent_id)
    span.set_attribute("oneops.router.dispatch_reason", "retriever_floor_llm_hedge")
    span.set_attribute("oneops.router.llm_hedge_rationale",
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
                 registry: Any = None,
                 abstain_min_score: float | None = None,
                 abstain_min_margin: float = 0.0,
                 strict_fit: bool = False) -> None:
        self._gateway = gateway
        self._model = model
        # strict_fit: the candidate set is FIXED and externally constrained
        # (e.g. a team's members), NOT a relevance-ranked shortlist from
        # retrieval. In that mode the reranker must judge whether any candidate's
        # capability genuinely FITS the query and refuse (empty selection) when
        # none do — instead of the default global bias toward always routing an
        # in-domain query to the closest agent. Off by default (the global router
        # keeps its always-route discipline). Used by the team member-selector.
        self._strict_fit = strict_fit
        # Optional; when None the disambiguator falls back to id-only listings
        # (preserves the pre-2026-05-28 behaviour for callers that haven't
        # been wired through yet).
        self._registry = registry
        # Abstain = a JUNK-SKIP floor, NOT a decision threshold (2026-06-07 v2).
        # A retrieval SCORE must never *refuse* a query — only the reranker may
        # (it reads the full card and judges intent + axis-D). So this floor is
        # set LOW (~0.25): below it, retrieval is clearly junk → skip the LLM to
        # save cost; ABOVE it, the borderline band falls THROUGH to the reranker,
        # which is the refuse authority. This is the canonical "retrieve → if
        # confident route, else hand to the LLM" pattern — setting it high (a
        # decision threshold) over-refused weak-but-valid queries (e.g. KB topic
        # queries ~0.31) before the corrector could run. None = gate OFF.
        # AMBIGUOUS (top-two within abstain_min_margin) likewise only fires as a
        # junk-skip, not a verdict. Preroute is exempt. Tuned per-domain at scale.
        self._abstain_min_score = abstain_min_score
        self._abstain_min_margin = abstain_min_margin
        # Catalog block — built lazily once and embedded in the cached system
        # prompt so Anthropic's prompt cache reuses descriptions across every
        # routing call. Dragonfly FLUSHALL never touches it; only a process
        # restart (which is also when the registry would reload) rebuilds it.
        self._catalog_block: str | None = None

    def _active_agent_ids(self) -> set[str]:
        """All active agent ids from the registry (empty set on any failure).
        Used as the preroute pool so the routing contract applies even when the
        lexical retriever drops a target agent for a short query."""
        if self._registry is None:
            return set()
        try:
            return set(self._registry.agents.list_ids())
        except Exception:                                   # noqa: BLE001
            return set()

    def _abstain_check(self, candidates: list[Candidate], span) -> Disambiguation | None:
        """Wrong-agent guard. Returns a refuse-and-clarify Disambiguation when
        retrieval is too weak or too ambiguous to commit; None to proceed to
        the LLM. OFF when abstain_min_score is None (no regression)."""
        if self._abstain_min_score is None or not candidates:
            return None
        ranked = sorted(candidates, key=lambda c: -c.score)
        top = ranked[0]
        if top.score < self._abstain_min_score:
            span.set_attribute("oneops.router.abstain", "weak")
            span.set_attribute("oneops.router.abstain.top_score", top.score)
            return Disambiguation.no_match(
                f"abstain (weak match): top candidate '{top.agent_id}' score "
                f"{top.score:.2f} < floor {self._abstain_min_score:.2f} — "
                f"ask the user to clarify rather than route")
        if len(ranked) >= 2 and self._abstain_min_margin > 0.0:
            margin = top.score - ranked[1].score
            if margin < self._abstain_min_margin:
                span.set_attribute("oneops.router.abstain", "ambiguous")
                span.set_attribute("oneops.router.abstain.margin", margin)
                return Disambiguation.no_match(
                    f"abstain (ambiguous): top two ('{ranked[0].agent_id}', "
                    f"'{ranked[1].agent_id}') within {margin:.2f} < margin "
                    f"{self._abstain_min_margin:.2f} — ask the user to clarify")
        return None

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

    def _scope(self, agent_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (use_when, not_when) for an agent, flattened+deduped across its
        skills. These are the reranker's POSITIVE scope and CONTRASTIVE
        boundaries (the latter carry 'route to <id>' hints). Empty on miss —
        never raises into the router."""
        if self._registry is None:
            return (), ()
        try:
            agent = self._registry.agents.get_optional(agent_id)
        except Exception:                                       # noqa: BLE001
            return (), ()
        if agent is None or not getattr(agent, "skills", None):
            return (), ()
        uw: list[str] = []
        nw: list[str] = []
        for s in agent.skills:
            uw.extend(s.use_when or ())
            nw.extend(s.not_when or ())
        return tuple(dict.fromkeys(uw)), tuple(dict.fromkeys(nw))

    def _build_catalog(self) -> str:
        """One-time build of the full agent catalog for the cached system prompt:
        every active agent's id + description + use_when (positive scope) +
        not_when (contrastive boundaries with 'route to <id>' hints). The
        not_when lines are what let the reranker tell same-entity look-alikes
        apart (summarize vs similar vs how-to); they are NOT embedded, so this
        catalog is the ONLY place they reach the routing decision. Stable across
        calls → benefits from prompt-cache. None when registry is unwired."""
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
            block = [f"\n### {aid}\n{desc}"]
            use_when, not_when = self._scope(aid)
            if use_when:
                block.append("Use when:\n" + "\n".join(f"  - {x}" for x in use_when))
            if not_when:
                block.append(
                    "Do NOT use — out of scope (pick the agent named in the clause):\n"
                    + "\n".join(f"  - {x}" for x in not_when))
            lines.append("\n".join(block))
        return "\n".join(lines)

    def _candidate_catalog(self, candidates: list[Candidate]) -> str:
        """Shortlist catalog: the cards (description + use_when + not_when) for
        ONLY the retrieved candidates, each with its retrieval score. Built per
        request and carried in the user block — so the reranker sees the top-K,
        never the full registry, and the prompt stays bounded as the agent count
        grows. This is the inject-all fix: the all-agents `_build_catalog` is no
        longer stitched into every prompt."""
        blocks: list[str] = []
        for c in candidates:
            desc = self._describe(c.agent_id) or "(no description)"
            block = [f"\n### {c.agent_id} (retrieval score {c.score:.2f})\n{desc}"]
            use_when, not_when = self._scope(c.agent_id)
            if use_when:
                block.append("Use when:\n" + "\n".join(f"  - {x}" for x in use_when))
            if not_when:
                block.append(
                    "Do NOT use — out of scope (pick the agent named in the clause):\n"
                    + "\n".join(f"  - {x}" for x in not_when))
            blocks.append("\n".join(block))
        return "\n".join(blocks)

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
            result = await self._disambiguate_inner(
                query_text, candidates, request_ctx=request_ctx, _span=span)
            # Langfuse: the routing "WHY" — candidates in, chosen agent(s) +
            # confidence + rationale out. chosen/confidence are safe routing
            # signals (always on span); the query + rationale are content
            # (redacted + content-gated via set_langfuse_io).
            span.set_attribute(
                "oneops.router.chosen",
                ",".join(result.selected_agent_ids) or "none")
            span.set_attribute("oneops.router.confidence", result.confidence)
            set_langfuse_io(
                span,
                input={"query": query_text,
                       "candidates": sorted(c.agent_id for c in candidates)},
                output={"chosen": list(result.selected_agent_ids),
                        "confidence": result.confidence,
                        "rationale": result.rationale},
                observation_type="span")
            return result

    async def _disambiguate_inner(
        self, query_text: str, candidates: list[Candidate], *,
        request_ctx: dict, _span,
    ) -> Disambiguation:
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
        preroute_pool = self._active_agent_ids() or valid_ids
        preroute = _deterministic_preroute(query_text, preroute_pool)
        if preroute is not None:
            agent_id, intent, rationale = preroute
            _span.set_attribute("oneops.router.preroute.fired", True)
            _span.set_attribute("oneops.router.preroute.target", agent_id)
            _span.set_attribute("oneops.router.preroute.rationale", rationale)
            _span.set_attribute(_ONEOPS_ROUTER_SELECTED, agent_id)
            return Disambiguation.select(
                [agent_id], confidence=0.95,
                rationale=rationale, intents=[intent])
        _span.set_attribute("oneops.router.preroute.fired", False)
        # Abstain gate — refuse-and-clarify before spending an LLM call when
        # retrieval is too weak/ambiguous to commit (wrong-agent guard, off by
        # default). Preroute above is exempt (deterministic high-confidence).
        abstain = self._abstain_check(candidates, _span)
        if abstain is not None:
            return abstain
        # Shortlist-only catalog (this is what scales to 100s of agents): the
        # per-call user block carries the cards (description + use_when +
        # not_when) of ONLY the retrieved top-K candidates — never the full
        # registry. The stable rules stay in the cached system prompt; this small
        # top-K catalog varies per call. Avoids the inject-all collapse past ~50
        # agents (a full-registry catalog in every prompt).
        listing = self._candidate_catalog(candidates)
        # Stage 2 (2026-05-28): when the conversation has an active focus
        # entity (carried in the LangGraph state via request_ctx), surface
        # it to the disambiguator so it has the correct prior. A
        # follow-up query like "what is the root cause" anchored on a PBM
        # record is overwhelmingly a record-field read (axis A), not a KB
        # search — without this signal the LLM looks at the words alone
        # and can probabilistically drift to UC-3.
        focus_block = _build_focus_block(
            (request_ctx.get("focus_entity_id") or "").strip(),
            (request_ctx.get("focus_service_id") or "").strip())
        user_block = (
            f"Query:\n{query_text}{focus_block}\n\n"
            f"Candidate agents — choose ONLY from these. Each card has the "
            f"agent's description, Use when, and Do NOT use:\n{listing}"
        )
        # System prompt = the STABLE rules only (prompt-cache friendly). The
        # agent cards are NOT here — they ride per-request in the user block,
        # scoped to the retrieved candidates (shortlist-only catalog).
        extra_sections = [_DISAMBIGUATE_PROMPT]
        if self._strict_fit:
            extra_sections.append(_STRICT_FIT_PROMPT)
        system_prompt = compose(Profile.INTERNAL_AGENT,
                                extra_sections=extra_sections)
        return await self._call_and_select(
            system_prompt, user_block, valid_ids, candidates, request_ctx, _span)

    async def _call_and_select(
        self, system_prompt: str, user_block: str, valid_ids: set[str],
        candidates: list[Candidate], request_ctx: dict, _span,
    ) -> Disambiguation:
        """Stage-4 LLM call + closed-class parse. The system block is the stable
        policy+rules prefix (prompt-cached for ~50-90% input-token savings); the
        per-call user block carries the shortlist catalog. On an LLM hedge (no
        valid selection) defer to the retriever floor; on call/parse error
        return no_match — never a guessed route."""
        import json

        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.observability import get_logger
        try:
            response = await self._gateway.call(LlmRequest(
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
                floor = _floor_dispatch(candidates, doc, _span)
                if floor is not None:
                    return floor
                _span.set_attribute(_ONEOPS_ROUTER_SELECTED, "")
                return Disambiguation.no_match(
                    str(doc.get("rationale") or "no candidate matched the intent"))
            _span.set_attribute(_ONEOPS_ROUTER_SELECTED, ",".join(selected))
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
