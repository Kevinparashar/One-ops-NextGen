"""Stage 0a — decomposition: split a compound message into sub-queries.

A single user message can carry several independent jobs ("show my incidents,
my approvals, and my requests") or a dependent chain ("find my oldest P3 *and
escalate it*"). Each sub-query is routed on its own through the funnel; the
plan stitches them back together.

Splitting is a **semantic judgment**, never a keyword cut. "Summarize the
incident *and* its timeline" is ONE job; "summarize the incident *and* find
related KB" is TWO. A split on the word "and" would get both wrong — so the
real decomposer is an LLM call (`LlmDecomposer`). The deterministic
`PassthroughDecomposer` treats the whole message as one sub-query; it backs
unit tests and local dev (a single-intent message is the common case and needs
no LLM).

`SubQuery.depends_on` carries decomposer-detected ordering hints (sub-query B
needs sub-query A's result first); the router turns them into plan-DAG edges.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from oneops.observability import get_logger, get_tracer, set_langfuse_io

_log = get_logger("oneops.router.decompose")
_tracer = get_tracer("oneops.router.decompose")


def planner_emit_bindings_enabled() -> bool:
    """Feature flag (default ON as of D4 enable). Data-flow binding was validated
    end-to-end (PBM root_cause → KB) and the full devils + unit + integration
    cycle came back clean against baseline with it on, so it now ships enabled.
    Set ONEOPS_PLANNER_EMIT_BINDINGS to 0/false/no/off to disable (kill-switch).
    When disabled, the decomposer prompt + parsing are byte-identical to before —
    bindings are pure enrichment, so off is a safe, zero-impact rollback."""
    return os.getenv("ONEOPS_PLANNER_EMIT_BINDINGS", "1").strip().lower() in (
        "1", "true", "yes", "on")


@dataclass(frozen=True)
class SubQuery:
    """One atomic job extracted from the user message."""

    id: str
    text: str
    depends_on: tuple[str, ...] = ()        # other SubQuery ids that must run first
    # Optional data-flow bindings: (from_sq, from_field, to_param) — feed a
    # specific value an upstream sub-query produced into this one's input.
    # Empty unless the planner-emit-bindings flag is on AND the LLM declared a
    # value dependency. Mapped to step-level ParameterBindings in assemble_plan.
    bindings: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("SubQuery.id is mandatory")
        if self.id in self.depends_on:
            raise ValueError(f"sub-query {self.id} cannot depend on itself")


class Decomposer(Protocol):
    async def decompose(self, message: str, *, request_ctx: dict) -> list[SubQuery]:
        """Split `message` into one or more sub-queries, in author order."""
        ...


class PassthroughDecomposer:
    """Deterministic decomposer — the whole message is a single sub-query.

    Correct for every single-intent message (the common case) and needs no
    LLM. A genuine implementation of the Protocol, not a mock — it simply
    never splits."""

    async def decompose(self, message: str, *, request_ctx: dict) -> list[SubQuery]:
        return [SubQuery(id="sq1", text=message)]


_DECOMPOSE_PROMPT = """You split a user's message into atomic sub-queries for an \
ITSM assistant.

## Your job (and what is NOT your job)

YOUR JOB — splitting and reference resolution only:
  • Decide whether the message contains ONE independent ask or MULTIPLE.
  • Resolve implicit references ("for it", "on this") by inlining the
    canonical entity id.
  • Preserve every other word VERBATIM, including the user's verbs.

NOT YOUR JOB — routing or intent classification:
  • Do NOT rewrite the user's verbs. "what do we know about X" stays
    "what do we know about X"; do NOT emit "summarize X". "any docs for X"
    stays "any docs for X"; do NOT emit "find KB for X". "details about X"
    stays "details about X"; do NOT emit "summarize X".
  • Do NOT collapse different phrasings into a single canonical form.
  • Verb choice IS the routing signal the user gave us. Destroying it
    forces the router to guess and produces wrong routes. Treat verbs as
    inviolable.

## Decision principle for splitting

A sub-query is ONE independent ask that produces ONE independent answer. A \
connective word ("and", a comma) is NOT a split signal on its own — judge by \
whether the two parts are independent asks or two facets of the same ask. \
Default to ONE sub-query; split only when the message contains two truly \
distinct asks.

## Reasoning steps (think silently, then emit)

1. **List entity ids mentioned** (INC..., REQ..., PBM..., CHG..., AST..., \
CI..., KB...). Note "none" if no explicit ids.
2. **Identify whether the message contains ONE ask or MULTIPLE** — judge \
semantically: are the parts independent (would each deserve its own answer) \
or facets of the same ask (multiple fields of one record = ONE ask).
3. **Write each sub-query as a self-contained text** — VERBATIM from the \
user, with reference-resolution applied (replace "it" / "this" / "the \
incident" with the canonical id when context makes it unambiguous).
4. **Coverage check** — every entity-id and noun-phrase the user mentioned \
appears in some sub-query. No silent drops, no invented sub-queries, no \
verb rewrites.

## Few-shot examples (diverse verbs — DO NOT canonicalize)

INPUT: "summarize INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask",
 "subqueries":[{"id":"sq1","text":"summarize INC0001001","depends_on":[]}]}

INPUT: "what do we know about INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'what do we know about' preserved",
 "subqueries":[{"id":"sq1","text":"what do we know about INC0001001","depends_on":[]}]}

INPUT: "any docs for INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'any docs for' preserved",
 "subqueries":[{"id":"sq1","text":"any docs for INC0001001","depends_on":[]}]}

INPUT: "details of INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'details of' preserved",
 "subqueries":[{"id":"sq1","text":"details of INC0001001","depends_on":[]}]}

INPUT: "priority of INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'priority of' preserved",
 "subqueries":[{"id":"sq1","text":"priority of INC0001001","depends_on":[]}]}

INPUT: "tell me about CI0000001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'tell me about' preserved",
 "subqueries":[{"id":"sq1","text":"tell me about CI0000001","depends_on":[]}]}

INPUT: "walk me through INC0001001"
OUTPUT:
{"reasoning":"1 entity, 1 ask — verb 'walk me through' preserved",
 "subqueries":[{"id":"sq1","text":"walk me through INC0001001","depends_on":[]}]}

INPUT: "how do I fix VPN"
OUTPUT:
{"reasoning":"no entity, topic ask — preserved",
 "subqueries":[{"id":"sq1","text":"how do I fix VPN","depends_on":[]}]}

INPUT: "is there a playbook for this"
OUTPUT:
{"reasoning":"no entity, knowledge ask — preserved (router will use focus context)",
 "subqueries":[{"id":"sq1","text":"is there a playbook for this","depends_on":[]}]}

INPUT: "what is the priority and status of INC0001001"
OUTPUT:
{"reasoning":"1 entity, multi-field facets of one ask → 1 sub (verbatim)",
 "subqueries":[{"id":"sq1","text":"what is the priority and status of INC0001001","depends_on":[]}]}

INPUT: "summarize INC0001001 and any docs for it"
OUTPUT:
{"reasoning":"2 independent asks on the same entity — split; sq2 resolves 'it' to INC0001001; both verbs preserved",
 "subqueries":[
   {"id":"sq1","text":"summarize INC0001001","depends_on":[]},
   {"id":"sq2","text":"any docs for INC0001001","depends_on":["sq1"]}]}

INPUT: "details of INC0001001 and do we have any data regarding this"
OUTPUT:
{"reasoning":"2 independent asks on the same entity — split; sq2 resolves 'this' to INC0001001; both verbs preserved verbatim",
 "subqueries":[
   {"id":"sq1","text":"details of INC0001001","depends_on":[]},
   {"id":"sq2","text":"do we have any data regarding INC0001001","depends_on":["sq1"]}]}

INPUT: "priority of INC0001001 and risk level of CHG0004001"
OUTPUT:
{"reasoning":"2 entities, 2 independent asks → 2 subs",
 "subqueries":[
   {"id":"sq1","text":"priority of INC0001001","depends_on":[]},
   {"id":"sq2","text":"risk level of CHG0004001","depends_on":[]}]}

INPUT: "summarize CHG0004001 and search KB for outlook sync issues"
OUTPUT:
{"reasoning":"2 independent asks; sq2 names its own topic so no entity inlining",
 "subqueries":[
   {"id":"sq1","text":"summarize CHG0004001","depends_on":[]},
   {"id":"sq2","text":"search KB for outlook sync issues","depends_on":[]}]}

INPUT: "compare INC0001001 and INC0001002"
OUTPUT:
{"reasoning":"single analytical job over 2 entities → 1 sub",
 "subqueries":[{"id":"sq1","text":"compare INC0001001 and INC0001002","depends_on":[]}]}

## Inlining rule (when to add the entity id to a sub-query, when NOT to)

When a sub-query implicitly refers to another sub-query's entity ("for it",
"on this", "any docs on this", "find related articles", "find guidance",
"any runbook") — REWRITE that sub-query's text to inline the referenced
canonical id, and set depends_on. This lets downstream tools dispatch a
linked-to-ticket search instead of a literal text search.

When a sub-query EXPLICITLY names its own topic — VPN disconnects, MFA,
outlook sync, password reset, etc. — its subject is already concrete.
DO NOT inline a sibling sub-query's entity id into it. That would route
the KB search through the wrong tool (ticket-linked vs text search) and
drag the second sub-query onto the first sub-query's UC. Keep the text
verbatim; depends_on is empty.

INPUT: "summarize the incident and its timeline"
OUTPUT:
{"reasoning":"entities=[(implicit focus)]; goals=[summarize]; timeline is part of the summary → 1 sub",
 "subqueries":[{"id":"sq1","text":"summarize the incident and its timeline","depends_on":[]}]}

INPUT: "priority and status"
OUTPUT:
{"reasoning":"entities=[(implicit focus)]; goals=[field-read(priority),field-read(status)]; same implicit entity multi-field → 1 sub",
 "subqueries":[{"id":"sq1","text":"priority and status","depends_on":[]}]}

INPUT: "hello"
OUTPUT:
{"reasoning":"no entity, no ITSM goal; greeting → 1 sub passthrough",
 "subqueries":[{"id":"sq1","text":"hello","depends_on":[]}]}

## Hard rules

- Never invent entity ids that are not in the user's message.
- Never drop content the user mentioned. Coverage must be complete.
- Never add a rollup sub-query that merely combines earlier outputs.
- Sub-query ids are sq1, sq2, sq3, … in author order.

Return STRICT JSON ONLY in the shape shown above:
{"reasoning":"...","subqueries":[{"id":"sq1","text":"...","depends_on":[]}]}"""


# Appended to the prompt ONLY when the planner-emit-bindings flag is on, so the
# default prompt is byte-identical (cached, zero behaviour change). Teaches the
# LLM to declare a data-flow edge when a later ask consumes an earlier result.
_BINDINGS_PROMPT = """\

## Data-flow bindings — ADDITIVE enrichment, never a replacement

Resolve every back-reference the normal way FIRST: follow the Inlining rule —
INLINE the canonical entity id into the dependent sub-query and set depends_on.
That keeps each sub-query self-contained and routable on its own. This never
changes.

THEN, and only additionally, when the later ask consumes a VALUE the earlier
sub-query PRODUCES (not the record itself) — "the root cause it surfaces", "the
error it returns", "whatever CIs it finds", "that risk score", "the workaround
it gives" — add a `bindings` entry as an ENRICHMENT hint that carries that value
from the earlier sub-query:
  {"from":"sq1","from_field":"<field sq1 produces>","to_param":"<the later input>"}

The binding is a HINT, not a replacement:
  - ALWAYS keep the inlined entity in the text (never strip it). If the produced
    value is available at runtime the step is enriched with it; if not, the
    inlined text alone still gives a correct, self-contained query.
  - This is why a binding can never make a query worse than plain inlining.

Examples (note: BOTH keep the entity inlined; the second ALSO adds a hint):

INPUT: "details of INC0001001 and any docs for it"
OUTPUT:
{"reasoning":"'it' = entity INC0001001 → inline; pure record reference, no produced value",
 "subqueries":[
   {"id":"sq1","text":"details of INC0001001","depends_on":[]},
   {"id":"sq2","text":"any docs for INC0001001","depends_on":["sq1"]}]}

INPUT: "summarize INC0001001 and find KB for the root cause it surfaces"
OUTPUT:
{"reasoning":"sq2 consumes the root cause sq1 produces → inline the entity AND add an enrichment binding",
 "subqueries":[
   {"id":"sq1","text":"summarize INC0001001","depends_on":[]},
   {"id":"sq2","text":"find KB for the root cause of INC0001001","depends_on":["sq1"],
    "bindings":[{"from":"sq1","from_field":"root_cause","to_param":"query"}]}]}

Rules:
  - Most splits need NO bindings — omit the field entirely.
  - NEVER strip the entity to add a binding; the binding is always extra.
  - `from` MUST be an earlier sub-query id; never bind a sub-query to itself
    and never form a cycle (the dependency graph stays acyclic).
  - `from_field` and `to_param` are NAMES, not values — never invent a value."""


class LlmDecomposer:
    """Production decomposer — one gateway call returning structured sub-queries.

    Splitting is the LLM's semantic judgment (the prompt teaches the "and
    within one intent" trap). A call that fails or returns unparseable JSON
    falls back to a single passthrough sub-query — a decomposition fault never
    drops the user's message.
    """

    def __init__(self, gateway, *, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model

    async def decompose(self, message: str, *, request_ctx: dict) -> list[SubQuery]:
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.policy import Profile, compose

        with _tracer.start_as_current_span(
            "router.stage0a.decompose",
            attributes={"oneops.router.stage": "0a",
                        "oneops.router.model": self._model,
                        "oneops.message.char_len": len(message or "")},
        ) as span:
            # Every LLM call carries the policy layer (Component Spec C15). The
            # task instruction rides as an extra section; the profile is static so
            # the composed prefix is cached (latency) and byte-stable (token cost).
            emit_bindings = planner_emit_bindings_enabled()
            extra = ([_DECOMPOSE_PROMPT, _BINDINGS_PROMPT] if emit_bindings
                     else [_DECOMPOSE_PROMPT])
            system_prompt = compose(Profile.INTERNAL_AGENT, extra_sections=extra)
            try:
                response = await self._gateway.call(LlmRequest(
                    # System prefix = policy-composed prefix + decompose rules.
                    # Stable across every turn — mark for prompt cache.
                    messages=(LlmMessage("system", system_prompt,
                                         cache_control=True),
                              LlmMessage("user", message)),
                    model=self._model,
                    tenant_id=request_ctx.get("tenant_id") or "_unknown",
                    user_id=request_ctx.get("user_id", "") or "",
                    response_format=ResponseFormat.JSON,
                    request_id=request_ctx.get("request_id", "")))
                doc = json.loads(response.content)
                subs = [
                    SubQuery(id=str(s["id"]), text=str(s["text"]),
                             depends_on=tuple(s.get("depends_on") or ()),
                             bindings=_parse_bindings(s) if emit_bindings else ())
                    for s in doc.get("subqueries", []) if s.get("text")
                ]
                subs = _sanitize_subqueries(subs)
                if subs:
                    span.set_attribute("oneops.router.subquery_count", len(subs))
                    set_langfuse_io(span, input=message,
                                    output=[s.text for s in subs])
                    return subs
                span.set_attribute("oneops.router.fallback", "no_subs")
            except (LLMGatewayError, ValueError, KeyError, TypeError) as exc:
                span.set_attribute("oneops.router.fallback", "llm_failed")
                _log.warning("decomposer.llm_failed_falling_back", error=str(exc))
        # Fallback — the message is still handled, as one sub-query.
        return [SubQuery(id="sq1", text=message)]


def _parse_bindings(s: dict) -> tuple[tuple[str, str, str], ...]:
    """Pull a sub-query's optional `bindings` array into normalised triples,
    dropping any malformed or self-referential entry (defensive — the LLM
    output is untrusted)."""
    out: list[tuple[str, str, str]] = []
    for bd in (s.get("bindings") or []):
        if not isinstance(bd, dict):
            continue
        fr = str(bd.get("from") or "").strip()
        ff = str(bd.get("from_field") or "").strip()
        tp = str(bd.get("to_param") or "").strip()
        if fr and ff and tp and fr != str(s.get("id") or ""):
            out.append((fr, ff, tp))
    return tuple(out)


def _sanitize_subqueries(subs: list[SubQuery]) -> list[SubQuery]:
    """Deterministic clean-up of LLM decomposer output.

    The LLM sometimes:
      * duplicates sub-queries verbatim (same text twice in the list);
      * appends a trailing "rollup" sub-query that depends on every prior
        sub-query and merely re-states the message — the aggregator
        already stitches per-step results, so a rollup is double work.

    Both shapes inflate plan-step counts and confuse routing. This
    routine collapses them by text-equality (case + whitespace
    normalized) and drops any tail sub-query whose depends_on covers
    *every other* sub-query id. Order is preserved otherwise; sub-query
    ids are re-issued sq1..sqN so depends_on edges remain consistent.
    """
    if len(subs) <= 1:
        return subs

    # 1. Dedupe by normalized text. Keep the first occurrence's edges.
    seen: dict[str, SubQuery] = {}
    deduped: list[SubQuery] = []
    for s in subs:
        key = " ".join(s.text.lower().split())
        if key in seen:
            continue
        seen[key] = s
        deduped.append(s)

    # 2. Drop trailing rollup sub-query: depends on EVERY prior sub-query
    # AND there are at least 3 priors. With only one prior, a dependency
    # is legitimate sequencing ("summarize X and find KB for it"); a
    # rollup is only meaningful when it consolidates several independent
    # outputs.
    if len(deduped) >= 4:
        tail = deduped[-1]
        other_ids = {s.id for s in deduped[:-1]}
        if other_ids and set(tail.depends_on) >= other_ids:
            deduped = deduped[:-1]

    # 3. Re-id sq1..sqN so depends_on edges stay valid after dedup.
    id_remap = {s.id: f"sq{i+1}" for i, s in enumerate(deduped)}
    return [
        SubQuery(
            id=id_remap[s.id],
            text=s.text,
            depends_on=tuple(id_remap[d] for d in s.depends_on
                             if d in id_remap),
            # Remap each binding's source sub-query id; drop bindings whose
            # source was deduped away (no longer a valid upstream).
            bindings=tuple((id_remap[fr], ff, tp) for (fr, ff, tp) in s.bindings
                           if fr in id_remap),
        )
        for s in deduped
    ]


__all__ = ["SubQuery", "Decomposer", "PassthroughDecomposer", "LlmDecomposer"]
