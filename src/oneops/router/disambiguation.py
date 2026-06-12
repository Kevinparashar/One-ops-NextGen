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

## How to decide — the cards are authoritative

The scope of each candidate is its CARD — its description, "Use when"
(positive scope), and "Do NOT use" (out-of-scope cases, each naming the agent
to pick instead). The cards are provided with the query below. For each thing
the user asks for:
  • Select the candidate whose "Use when" covers the ask AND whose "Do NOT
    use" does not exclude it.
  • "Do NOT use" is DECISIVE: if the ask matches a candidate's "Do NOT use"
    clause, do not select that candidate — select the agent it names instead,
    when that agent is in the candidate list.
  • A query may ask for several things — return every agent whose card covers
    a part of it (see Multi-intent below).
  • Do not invent agents that are not in the candidate list.

Reason over the cards, not over a fixed taxonomy. Every candidate is
first-class; none is a default.

## The job each ask wants done (a lens for reading the cards, not a checklist)

The clarifying question is: **what does the user want DONE with the object,
and where does the answer come from?** Common jobs — each owned by whichever
candidate's card claims it:
  • Read THIS record's own facts — its status, priority, owner, SLA,
    description, what happened, current state, or the VALUE of one of its
    stored / linked fields (including root_cause / RCA, affected_ci, the
    related or linked problem or change — these are fields ON the record). A
    bare id ("INC0001001") is this.
  • Find OTHER records like this one — duplicates, prior or recurring
    occurrences, whether we have seen this pattern before, how widespread or
    frequent it is, related cases for resolution reuse. Anything about the
    issue's PREVALENCE or HISTORY across many tickets, rather than this one
    record's own fields.
  • Retrieve authored KNOWLEDGE about the object — KB articles, runbooks,
    SOPs, how-to, documented fixes / workarounds, a documented "known-error"
    write-up. Topic-only questions with no record ("how do I fix VPN") are
    this too.
  • Classify or act on a record — triage, prioritise, categorise, assign, or
    route a single ticket.
  • Obtain something new — software, a license, hardware, access, an account,
    onboarding (OBTAIN / PROVISION / ORDER / REQUEST / SET UP).

These jobs are a lens for reading the cards. If a card's "Use when" plainly
covers the ask, select it even when the ask fits none of the jobs above.

## Record-scoped agents need a record in scope (admission rule)

The record-summary agent reads ONE specific record's own fields, so it is
meaningful ONLY when a record is identified — a record id in the query itself or
the ACTIVE FOCUS record shown above. With neither, it has nothing to read;
selecting it only forces a "which record do you mean?" dead-end, so do NOT.

The similar-tickets agent has TWO ways to be anchored, and needs EITHER:
  • a specific record (id or active focus) — find tickets like THAT record; or
  • the user's own intent is to be handed a COLLECTION OF EXISTING RECORDS that
    resemble a problem they describe — then the description is the anchor.

The distinction is the DELIVERABLE the user wants to walk away with, judged by
meaning, not by any trigger word. similar-tickets exists to hand back a set of
records the user can scan; the knowledge agent exists to hand back an answer or
fix. So the test is: does this person want a LIST OF RECORDS, or do they want
their problem understood and resolved? Naming or describing a problem expresses
the second — a person who HAS a problem wants help with it, so it is authored
guidance → the knowledge agent (the safe default), even when terse and even
when a record is in focus. similar-tickets applies only when the records
THEMSELVES are the thing being requested — the user wants to inspect what else
exists, not to be told what to do. When a query could be read either way,
prefer the knowledge agent. A request to OBTAIN something new → the fulfilment
agent.

Active focus never converts a new-topic problem into a similar-tickets
follow-up: when the query carries its own subject, route it on that subject's
intent — a stated problem is still a knowledge request, not "similar to the
focus record".

## The confusions users actually trigger (contrastive principles)

1. The record's own facts vs. authored knowledge — the classic trap.
   "what do we know about INC0001001" and "what info is available for
   INC0001001" look identical but differ:
   • "what do we know about X" / "details OF / ABOUT X" / "tell me about X" /
     "what happened in X" → the record's OWN fields → the record-summary agent.
   • "what info is available FOR X" / "anything written up ON X" / "docs or
     runbook FOR X" / "how was this solved" → AUTHORED material → the KB agent.
   The answer-source is what the user NEEDS, not the phrasing: a direct "how
   do I fix X" and a meta "is there a procedure for X" both want authored KB.
   • Field-reads disguised as knowledge — "root cause" / "RCA" / "the affected
     CI" / "the related or linked problem or change" / "its X". These are the
     VALUE of a field stored on the record → record-summary, regardless of the
     noun. Anything asking the value of a specific attribute or linked-record
     id of the focus is a field-read on the record.

2. THIS record vs. OTHER records like it — the prevalence trap (these are
   frequently mis-routed to record-summary):
   • "summarize / status / who owns INC0001001" → THIS record →
     record-summary.
   • "is this recurring" / "have we seen this before" / "is it trending" /
     "is this happening org-wide" / "any duplicates" / "is this a known
     recurring issue" / "is this a fresh issue or an old one" / "did we
     already fix this somewhere" → the issue's prevalence or history across
     MANY tickets → the similar-tickets agent. A "known RECURRING issue /
     pattern" → similar-tickets; a documented "known-error ARTICLE" → KB. The
     signal is whether the user wants past TICKETS or an authored DOCUMENT.

3. Same record, different job. "how do I resolve INC0001001" carries an id but
   wants authored guidance → KB; "triage INC0001001" wants classification →
   triage; "summarize INC0001001" wants its fields → record-summary. The id is
   shared; the JOB differs — let the cards' "Do NOT use" clauses arbitrate.

## "Learn it" vs "get it" — and the safe default (teach before you provision)

For an IT problem or need, the user is ultimately after ONE of two deliverables.
Decide by what they would walk away with — read from their words AND the
conversation so far, never from a single trigger verb:

  • KNOWING — they want to understand, fix, or do the thing themselves; what
    they walk away with is an explanation or steps they will act on.
    → the knowledge (KB) agent.
  • HAVING — they want to be put in possession of something they lack or cannot
    yet use (access, a license, hardware, an account, a provisioned resource);
    what they walk away with is work carried out for them.
    → the fulfilment (request) agent.

Three rules settle the hard cases:

  1. Lacking something — not having it, or being unable to access or use it — is
     a PROBLEM STATE, not a request. It is HAVING only when the user actually
     asks to be granted, given, provisioned, or set up with it. "I can't get
     into X" / "X isn't available to me" reports a state; it does not by itself
     ask to be granted X.

  2. When the message commits to NEITHER deliverable — it only reports that
     something is missing, blocked, broken, slow, or unavailable, with no sign
     of whether the user wants to be shown how or wants it done for them — it is
     AMBIGUOUS. DEFAULT to the knowledge (KB) agent. Never refuse; never guess
     fulfilment.

  3. Wanting to understand the PROCESS by which something is obtained — the
     procedure or steps one follows to get access to, set up, or request a thing
     — is itself a KNOWING ask: the deliverable the user wants FIRST is the
     procedure, not the thing. This holds even when the thing named is ordinarily
     provisioned — seeking the how-to defaults to the knowledge agent, and the
     follow-up then offers to raise the actual request. It is HAVING only when
     the user is asking to be given or granted the thing itself, with no interest
     in the procedure for getting it.

The guiding heuristic: **when in doubt, teach before you provision.** Defaulting
to KB is safe because a later step offers to raise a service request after the
KB answer, so the user is never stranded on the wrong path. This governs ONLY
the knowledge-vs-fulfilment choice — it never overrides another card whose "Use
when" plainly fits, and never overrides a request that clearly asks to be given
or provisioned something.

## Contrastive examples (apply the PRINCIPLE, do not match strings)

Read THIS record's own facts (→ record-summary agent):
  • "summarize INC0001001" / "describe INC0001001" / "details of INC0001001"
  • "what do we know about INC0001001" / "what happened in INC0001001"
  • "what is the priority / status / SLA / owner / category / severity of INC0001001"
  • "who is INC0001001 assigned to" / "INC0001001" (bare id) / "CI0000001"
  • Linked / RCA field-reads — also this agent (the values are STORED ON the
    record): "root cause" / "RCA" / "the affected CI" / "the related problem" /
    "the linked change" / "status of the linked problem" / "owner of the
    related problem" / "its X". Resolve the link and read the field; not a KB
    search.

Find OTHER records like it (→ similar-tickets agent):
  • "any duplicates of INC0001001" / "have we seen this before"
  • "is this recurring" / "is this trending" / "is it happening org-wide"
  • "other tickets with the same problem" / "past cases like this one"
  • "is this a fresh issue or an old one" / "did we already fix this somewhere"
  • "is this a known recurring issue"   (past TICKETS, not an article)

Retrieve authored knowledge (→ KB agent):
  • "any docs / runbooks / playbook for INC0001001" / "how was this solved"
  • "what should I follow for this issue" / "is there anything documented"
  • "how do I fix VPN" (topic only, no entity) / "MFA reset procedure"
  • "is there a known-error article for this"

Classify or route a ticket (→ triage agent):
  • "triage INC0001001" / "what priority should this be" / "which team owns this"

Obtain something new (→ fulfilment / catalog agent):
  • "I need a new laptop" / "request access to the finance folder"
  • "set me up with a VPN token" / "can I get Tableau installed"

Several at once (→ every agent that applies; record-summary first if present):
  • "summarize INC0001001 and any docs for it" → [record-summary, KB]
  • "summarize INC0001001 and find similar ones" → [record-summary, similar-tickets]

Off-domain (→ []):
  • "tell me a joke" / "what's the weather"

## Decision procedure

For each ask in the query, identify two things:

  • OBJECT — what the user is asking about: a specific record (or the current
    focused record on a follow-up), or a class of things (a topic, technology,
    service, symptom, operational concern).

  • WHAT THEY WANT DONE — the value of a field on that record (read it), other
    records like it (its prevalence / history), authored knowledge about it, a
    classification / routing action on it, or obtaining something new. This is
    determined by what the user NEEDS, not by phrasing — a direct "how do I fix
    X" and a meta "is there a procedure for X" both want authored knowledge.

Match that to the candidate whose card ("Use when" / "Do NOT use") covers it.
"Do NOT use" is DECISIVE: if the ask matches a candidate's "Do NOT use" clause,
do not pick that candidate — pick the agent it names, when present. This is how
same-id look-alikes are separated: "how do I resolve INC0001001" carries an id
but asks for authored guidance, so the summary / similar agents' "Do NOT use"
clauses hand it to the KB agent; "is INC0001001 recurring" carries the same id
but asks about other tickets, so it goes to the similar-tickets agent.

## Multi-intent

When the query asks for more than one of these, return every agent that
applies, ordered with the record-summary agent first when it is selected.

## Dispatch discipline

Returning no agents means the user is refused before any agent runs.
Reserve that outcome for queries with no IT/ITSM/ITOM object at all —
casual conversation, off-topic chat. When the query has any in-domain
object — a record, a technology, a service, a symptom, an operational
topic — at least one in-domain agent should be selected. Each agent
reports its own no-result when its lookup yields nothing; that is the
correct place to surface "no match", not the router.

When you are genuinely uncertain between in-domain candidates and the KB
(knowledge) agent is in the set, prefer it. Its no-result reply is honest and
recoverable; a router-level refusal is not.

## Output schema (STRICT JSON only)

{"selected_agent_ids":["..."],
 "intents":["..."],
 "confidence":0.0-1.0,
 "rationale":"<one short sentence: the job the ask wanted + the card 'Use when' that decided it>"}

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
    """The ACTIVE-FOCUS prompt section (supplies a missing subject for bare
    follow-up queries; carries no routing meaning — the cards decide). Empty
    string when the conversation has no focus entity, so the caller can
    concatenate it unconditionally."""
    if not focus_id:
        return ""
    return (
        f"\n\nACTIVE FOCUS (the user is mid-conversation about):\n"
        f"  entity_id: {focus_id}\n"
        f"  service:   {focus_service or 'unknown'}\n"
        f"How to use the focus — apply rigorously:\n"
        f"\n"
        f"  The focus is ONLY a way to fill in a missing subject for a bare "
        f"follow-up. It is NOT a sticky route and carries NO routing meaning of "
        f"its own. The candidate CARDS below (each agent's 'Use when' / 'Do NOT "
        f"use') always decide which agent — with or without a focus. A user can "
        f"start a completely new, unrelated request mid-conversation without "
        f"restating context, and you MUST honour that.\n"
        f"\n"
        f"  1. Does the query carry its OWN intent — a new request or action, a "
        f"     different service/topic, or anything that does not depend on the "
        f"     focused record's own stored data to be understood? Then the "
        f"     focus is IRRELEVANT: choose the agent whose card matches the "
        f"     query's intent, exactly as if there were no focus. Do NOT route "
        f"     it to the agent that handled the focused record just because a "
        f"     focus exists.\n"
        f"\n"
        f"  2. Only if the query is a BARE reference with no intent of its own "
        f"     — a pronoun ('it', 'this', 'that'), a bare attribute name "
        f"     ('priority', 'owner', 'status'), or a linked-record phrase ('the "
        f"     related problem') — treat it as a query ABOUT entity {focus_id}: "
        f"     substitute that id as the subject, then STILL choose the agent "
        f"     whose card matches what is being asked of that record. The focus "
        f"     supplies the missing id; the cards decide the agent.\n"
        f"\n"
        f"  The focused record's topical keywords appearing inside an "
        f"  independent-intent query do not make it a follow-up. When unsure "
        f"  whether a query is independent or a bare follow-up, prefer routing "
        f"  by the cards on the query's own words.\n"
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
    # Teach-before-provision on a hedge: when the LLM could not pin the query to
    # one card but a viable in-domain set survived, prefer the knowledge (KB)
    # agent if it is a candidate, rather than whatever embedded closest. A hedge
    # over a vague problem statement ("issue with <system>") otherwise fell
    # through to the top retrieval score — often similar-tickets/summary — which
    # contradicts the self-service-first default. KB is the safe, recoverable
    # choice (it reports its own no-result, and a downstream step offers a
    # service request). KB identified by the id-suffix convention already used
    # for the intents tag below; it must still clear the same narrow floor.
    kb = next((c for c in candidates
               if c.agent_id.endswith("_kb_lookup") and c.score >= 0.10), None)
    chosen = kb if kb is not None else top
    span.set_attribute(_ONEOPS_ROUTER_SELECTED, chosen.agent_id)
    span.set_attribute(
        "oneops.router.dispatch_reason",
        "retriever_floor_llm_hedge_kb_default" if kb is not None
        else "retriever_floor_llm_hedge")
    span.set_attribute("oneops.router.llm_hedge_rationale",
                       str(doc.get("rationale") or "")[:160])
    return Disambiguation.select(
        [chosen.agent_id], confidence=float(chosen.score),
        rationale=(
            "supervisor dispatch-by-default: stage-3 retriever surfaced a viable "
            "in-domain set; stage-4 LLM hedged (no_match). Defaulted to the "
            "knowledge agent (teach-before-provision) — it reports its own "
            "no-result if nothing is found." if kb is not None else
            "supervisor dispatch-by-default: stage-3 retriever surfaced this "
            "agent above the signal floor; stage-4 LLM hedged (no_match). The "
            "agent reports its own no-result if its lookup yields nothing."),
        intents=["kb_search"] if chosen.agent_id.endswith("_kb_lookup") else [])


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
        # (it reads the full card and judges intent + off-domain). So this floor is
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
        # record is overwhelmingly a record-field read on that record, not a KB
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
