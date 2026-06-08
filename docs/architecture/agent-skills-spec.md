# Agent Skills — Spec (v0)

> Status: 2026-06-04. **Additive, NOT-yet-wired.** Declaring `skills` changes no
> routing behavior today; the dynamic router consumes them only once enabled
> (flag-gated + eval-gated). This is the "skill-declaration" piece of the
> zero-risk routing foundation (see `docs/planning/scheduler-refactor-scope.md` sibling).

## Why
Routing's final disambiguation today runs on a **hand-tuned taxonomy baked into a
prompt** (`router/disambiguation.py` — the AXIS A/B entity-vs-KB framing). That is
tuned to the *current* kinds of UC and does not generalize to a genuinely new
*kind* of UC at 1000-UC scale. The fix the industry converges on: each agent
**declares structured, self-describing skill cards**, and the router **matches the
query against those cards** instead of a prompt taxonomy. Adding a UC = adding its
skill card; the router needs no code/prompt change (agents-as-data).

## The schema (`oneops.registry.models.Skill`)
A frozen Pydantic model; an agent carries `skills: tuple[Skill, ...] = ()`.

| Field | Type | Meaning |
|---|---|---|
| `id` | str (`_ID_PATTERN`) | stable snake_case skill id |
| `name` | str ≤120 | short human label ("Summarize a ticket") |
| `description` | str ≤600 | **what it does AND when to use it** (Anthropic rule) — the routing key |
| `use_when` | tuple[str] | positive routing signals (intents/phrasings this skill should win) |
| `not_when` | tuple[str] | **disambiguation-as-data** — intents this skill must NOT win (the KB-vs-summary trap, moved off the prompt) |
| `tags` | tuple[str] | structured retrieval/filter boosts |
| `examples` | tuple[str] | illustrative queries — applied as PRINCIPLE, never a string-match list |

### Design rules
1. **`description` must state both *what* and *when*** (Anthropic Agent Skills).
2. **Disambiguation knowledge goes in `use_when`/`not_when`**, not a router prompt —
   this is what makes routing generalize.
3. **`examples` are principle illustrations, not a match list** (mirror the existing
   "apply the PRINCIPLE, do not match strings" rule).
4. **One skill = one coherent capability.** An agent may declare several.

## Industry basis (sources)
Converged pattern across: **Anthropic Agent Skills** (`SKILL.md`: `name`+`description`
= what+when; progressive disclosure = match metadata, then load detail); **A2A
AgentCard `skills[]`** (id/name/description/tags/examples as the machine-readable
discovery contract); **Salesforce Agentforce** (Reasoning Engine routes by matching
the query to a subagent's name+description; Actions=tools, Instructions=behavior);
**CrewAI** (Skills = how/when, distinct from Tools = what). All separate **discovery
(match on lightweight skill metadata)** from **execution (load tools/instructions
after selection)** — exactly the repo's retrieve → narrow → disambiguate funnel.

## Fully-worked example (UC-1)
```json
"skills": [
  {
    "id": "summarize_ticket",
    "name": "Summarize a ticket",
    "description": "Produce a natural-language summary of an incident/request/problem/change's OWN fields, status, and timeline. Use when the user wants facts ABOUT the record itself.",
    "use_when": ["summarize / describe / explain a ticket", "what do we know about / what happened in / current state of a record", "a bare entity id (INC..., CI...) with no other intent"],
    "not_when": ["user wants external KB / how-to / docs (that is the knowledge agent)", "user wants to change the record (that is an action agent)"],
    "tags": ["summarization", "read", "incident", "request", "problem", "change"],
    "examples": ["summarize INC0001001", "what's going on with this ticket", "walk me through PRB0002"]
  }
]
```

## How it plugs in (when wired — NOT today)
- **Retrieval** embeds the query against skill `name`+`description`+`tags` (richer,
  more precise candidate selection than agent-level description).
- **Disambiguation** injects the surviving skills' `use_when`/`not_when` and picks by
  match — replacing the hardcoded A/B taxonomy with data.
- **Adding UC #1001** = ship its skill card; router code/prompt unchanged.

## Status / rollout
- v0 schema: **shipped** (`models.Skill`, `AgentRecord.skills`, default empty).
- Skill cards: **declared on the 5 active UCs** (uc01/02/03/05/08) — declarative only.
- Wiring into retrieval/disambiguation: **deferred**, behind a flag, gated on a
  routing-eval harness proving no regression vs the current router.
