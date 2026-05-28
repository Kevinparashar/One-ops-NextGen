# ISS-004: Positional ordinals not in regex catalog fall through to no_referent

**Trigger:** User messages containing positional / referential ordinal phrases
that are productive language patterns but not in the principle file's
closed-lexeme list. Confirmed cases (20-case adversarial eval re-run after
(B)+(C), 2026-05-17):

- `"the one before that"` — positional, references a preceding entity.
- `"two ago"` — relative-position phrase.

Both also unchanged from the pre-(B)+(C) run, so this is independent of
the priority-order / type_filter fixes — it's a separate failure mode.

**Wrong behavior:** Both cases classify as `expression_type=no_referent`
(3/3 consistent in the pre-fix run; same behavior post-fix). The classifier
treats them as having no extractable referring expression. Downstream rule
engine sees `no_referent` → `valid_candidates=[]` → `is_unambiguous=False`
→ routes to clarification.

**Right behavior:** Classify as `expression_type=ordinal`. The rule engine's
`_resolve_ordinal_position()` already handles "unknown ordinal phrases"
defensively — when the regex catalog doesn't match, it returns `None` and
`_resolve_valid_candidates` passes all candidates through (so
`is_unambiguous` becomes False if multiple, routing to clarification). The
end-user experience is the same (clarify), BUT the classification carries
the user's actual intent forward, which matters for:
- Audit-log usefulness (debugging "why did the user not get an answer")
- Future ordinal-catalog expansion (knowing which phrases were tried)
- Component 3's clarification text (an "I think you meant by position but
  I'm not sure which" question is better than "I didn't understand").

**Root cause:** **Principle-level — closed-class lexicon under-covers
productive language patterns.** The principle's `ordinal` category lists:
`"first"`, `"previous"`, `"earlier"`, `"original"`, `"last"`, `"most recent"`,
`"latest"`, `"second"`, `"third"`, `"prior"`, `"current"`. It does NOT list:
- relative-position phrases like `"the one before that"`, `"the one after"`
- numerical-relative phrases like `"two ago"`, `"three back"`
- contextual ordinals like `"the earlier one in this conversation"`

The LLM, given a finite list, treats anything outside the list as not
matching the category — even when the linguistic intent is clearly ordinal.

**Fix:** **Deferred — accept v1 behavior; expand catalog reactively.** Both
cases fail safely (route to clarification). Two paths to address later:

1. Expand the principle's ordinal description to be more open-ended:
   "Any phrase that selects an entity by mention-position counts as
   ordinal, including positional/relative phrases (e.g. 'the one before
   that', 'two ago')." Risks: ordinal becomes a catch-all and over-applies.
2. Add specific positional phrases to the catalog incrementally as
   production traffic reveals real user phrasings.

Path 2 is more conservative and aligns with the project's
descriptions-are-principles-not-phrase-catalogs rule applied carefully:
closed-class grammatical features (numerical ordinals) get explicit
listing, productive patterns (positional references) require principle
generalization which is harder to get right.

**Test pinning:**
- `/tmp/eval_adversarial_20cases.py` cases P3.1 (`"the one before that"`)
  and P3.2 (`"two ago"`) — currently 3/3 `no_referent`, target `ordinal`.

**Status:** deferred (2026-05-17). Both cases route to clarification — safe
failure mode. Re-prioritize if production traffic shows positional
ordinals are common, OR if Component 3's clarification quality is hurt by
misclassifying intent.

**Related issues:** ISS-005 (similar pattern: principle's closed-list
coverage misses real language usage), ISS-002 (Generalization at the
bottom of that file applies here too: "closed-class regex catalogs catch
obvious lexemes but miss productive variants").

**Generalization:** Closed-class regex catalogs catch the obvious lexemes
but miss productive language patterns. For grammatical categories that
have a finite core AND productive variants, the catalog covers ~80% and
the rest fall through to safe defaults. **Acceptable for v1; expand
catalog incrementally based on production miss patterns.** Don't pre-emptively
add open-ended principle text — risks the same over-weighting problem
ISS-002 surfaced (long category description → LLM over-defaults to that
category).
