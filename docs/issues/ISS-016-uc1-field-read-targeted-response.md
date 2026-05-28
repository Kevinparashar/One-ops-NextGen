# ISS-016: UC-1 returns full summary on targeted field-read questions

**Trigger:** Frontend run 2026-05-26. User asks "what is the priority of
it" / "who is the assignee" / "when was it created" as follow-ups to
`summarize INC0001001`. Focus carries correctly (every turn resolves to
INC0001001 — visible via `cache hit · age Ns`). However, the reply for
each targeted question is the full Summary + Key Details card, not the
asked field.

**Wrong behavior:** A field-read question against the active focus
("what is the priority of it") returns the entire summarised record.
The asked value IS present inside Key Details, but the user has to
scan for it. With list-valued fields (Linked CIs, Related Incidents,
Approved By) this scan is worse — the answer is buried.

**Right behavior:**
- A single-field question returns one line: `Priority: P2`.
- A list-valued field returns one line, comma-joined: `Linked CIs:
  CI0000001, CI0000007`.
- A multi-field question ("priority and status") returns a short
  bullet block (`- Priority: P2`, `- Status: open`).
- A non-targeted question ("summarize INC0001001", "tell me about it")
  continues to return the full Summary + Key Details card.

**Root cause:** UC-1's `summarize_entity` handler always emits its
canonical Summary block regardless of the inbound question. There is
no `field_read` branch — the handler does not see the user's text, only
the structured `{ticket_id, service_id}` arguments. Routing classifies
the turn as UC-1 (correct) but cannot distinguish "summarize the whole
thing" from "answer this one field on the same thing".

**Generalization:** Any UC whose canonical response is a wide card
(summary, full record, full list) needs an intent split — *which subset
of my output does the user want this turn?*. The split lives inside
the UC handler, not in the router, because:

1. Field-name vocabulary is **structural to the UC** (the Key Details
   keys of UC-1). It does not belong in a global glossary or in the
   router.
2. The router only needs to know "this turn is for UC-1 against focus
   X". UC-1 then decides whether to return the full card or a subset.
3. Synonyms (priority/importance, assignee/owner, state/status,
   group/team) must resolve **semantically** at the field-extraction
   step, not via a phrase catalog in code (see
   `feedback_descriptions_principle_not_phrases.md`).

**Fix:** UC-1 internal field-read branch.

1. New module `src/oneops/use_cases/uc01_summarization/field_read.py`:
   - `extract_requested_fields(user_message, available_labels, gateway,
     model) -> list[str]` returns the canonical labels the user is
     asking for. Driven by a single LLM call with a stable, cacheable
     system prompt (policy-composed). The LLM resolves synonyms from
     the user message against the structural label set; the prompt
     does NOT enumerate user phrasings. Returns `[]` when the message
     is a full-summary request or unrelated.
   - Pluggable through `set_field_read_llm(fn)` (mirrors
     `set_summarize_llm`). Boot wiring lives in `app.py` so the
     handler stays test-friendly.

2. Router passes the rewritten sub-query text into step parameters as
   `user_message`. Fast-path (button) leaves it empty — the button is
   a structured invocation with no chat question, so it cannot be a
   field-read.

3. `summarize_entity` reads `arguments["user_message"]`. If present:
   - Build `humanise_record(record)` to get the canonical label set.
   - Call `extract_requested_fields`. If it returns ≥1 label, format
     just those fields and emit outcome `"field_read"` with a
     `"message"` ready for `friendly_step_response`.
   - Else: fall through to the existing summarise path.

4. `friendly_step_response` already surfaces a success step's `message`
   verbatim when no `summary.summary` paragraph is present — so the
   field-read outcome renders cleanly without aggregator changes.

**Single-value vs list-value handling:**
- Single value (`Priority: "P2"`) → `Priority: P2`.
- List value (`Linked CIs: ["CI0000001", "CI0000007"]`) → `Linked CIs:
  CI0000001, CI0000007`. The pre-existing `humanise_record` already
  joins lists for these fields; field-read just propagates the formatted
  string.
- Datetime fields render via `humanise_record`'s formatter (e.g.
  "April 1, 2026 09:10 UTC").

**Devil's-advocate test surface (must pass):**

- Pronoun + single field: "what is the priority of it" → `Priority: P2`.
- Synonym (`importance` → `priority`): "what's the importance?" → `Priority: P2`.
- Synonym (`owner` → `assigned to`): "who is the owner?" → `Assigned To: USR00003`.
- Synonym (`state` → `status`), (`group` → `assignment group`).
- Two fields in one message: "priority and status" → two-line response.
- List-valued: "what are the linked CIs?" → `Linked CIs: CI0000001, CI0000007`.
- Datetime: "when was it created?" → `Created At: April 1, 2026 09:10 UTC`.
- Empty list field on a record that has none → `No <label> on this <service>.`
- No-focus pronoun ("who is it assigned to?" with no active subject) →
  clarify, NOT a field-read.
- Full-summary ask: "summarize it" / "tell me about it" → returns the
  full Summary + Key Details (field extractor returns []).
- RBAC-denied focus: still denied; field_read does not bypass authz_recheck.
- LLM gateway down: field extractor returns `[]` (no fabricated split),
  handler falls through to summarise — full card.
- Multi-sub-query: "priority of INC0001001, status of REQ0002001" →
  two plan steps, each returns its targeted field (cross-entity).

**Status:** in progress. Wires through router (pass `user_message`),
handler (intent split), and app boot (LLM injection). Aggregator and
`friendly_step_response` need no changes.

**Related:**
- [[feedback_descriptions_principle_not_phrases]] — field extractor
  prompt is principle + structural label list, not user-phrase catalog.
- [[ISS-015]] — same wiring discipline (LLM-backed seam + Passthrough
  fallback) extended to a fourth seam.
- [[feedback_llm_is_decision_maker]] — field detection is an LLM
  decision; deterministic post-processing only formats.
