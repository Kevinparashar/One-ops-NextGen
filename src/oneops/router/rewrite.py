"""Stage 0b — rewriting: resolve implicit references against conversation history.

A sub-query is often not self-contained: "close it", "same as last time",
"what about the network one", "not that one — the earlier one". These only
mean something in the context of earlier turns. The rewriter resolves the
reference so the rest of the funnel sees a self-contained query — e.g.
"close it" → "close INC0048213".

Minimum-action principle: the rewriter passes the text through **unchanged**
unless there is a genuine reference to resolve. It never invents an entity.

Resolving a reference is a semantic judgment over the conversation — so the
real rewriter is an LLM call (`LlmRewriter`). The deterministic
`PassthroughRewriter` returns the text untouched; it backs unit tests and
local dev, and is correct for every already-self-contained sub-query (the
common case).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from oneops.observability import get_logger, get_tracer, set_langfuse_io

_log = get_logger("oneops.router.rewrite")
_tracer = get_tracer("oneops.router.rewrite")


@dataclass(frozen=True)
class ConversationTurn:
    """One prior turn the rewriter may resolve references against."""

    role: str           # user | assistant
    content: str


@dataclass(frozen=True)
class RewriteResult:
    """The rewriter's output — the resolved text plus an audit trail."""

    text: str
    changed: bool = False
    rationale: str = ""

    @staticmethod
    def unchanged(text: str) -> RewriteResult:
        return RewriteResult(text=text, changed=False, rationale="no reference to resolve")


class Rewriter(Protocol):
    async def rewrite(
        self, text: str, *, history: list[ConversationTurn], request_ctx: dict
    ) -> RewriteResult:
        """Resolve implicit references in `text` against `history`."""
        ...


class PassthroughRewriter:
    """Deterministic rewriter — returns the text unchanged.

    Correct for every self-contained query; a real Protocol implementation,
    not a mock. The router treats its `RewriteResult` exactly as it treats the
    LLM rewriter's, so funnel logic is identical either way."""

    async def rewrite(
        self, text: str, *, history: list[ConversationTurn], request_ctx: dict
    ) -> RewriteResult:
        return RewriteResult.unchanged(text)


_REWRITE_PROMPT = """## Implicit-Reference Resolution (strict)

Your job: when the user's message references an ITSM/ITOM entity
implicitly, rewrite the message so it explicitly names that entity. An
implicit reference is ANY of:

  * a pronoun or back-reference ("it", "that", "this", "the same",
    "the earlier one", "its", "their", …);
  * an elided / missing entity that the rest of the message would
    naturally need to be a complete query (e.g. "what is the priority"
    asks ABOUT something — if the conversation just discussed a record,
    that record is the implicit subject);
  * a **bare attribute name** standing alone, with no verb or
    question-word ("approved by", "affected CIs", "warranty", "owner",
    "priority", "state"). When focus is active, treat these as
    field-reads against focus and append the entity id to make the
    message routable;
  * a partial reference ("the incident", "the ticket", "the change",
    "the article" — meant generically rather than naming a specific id);
  * a **bare digit string** with no service prefix (e.g. "0001001",
    "1234", "1015"). These are ID completions:
      - When a focus record is in conversation history, infer the
        service from the focus's prefix and emit the canonical 7-digit
        id (e.g. focus is INC0001001, user says "0001015" → rewrite
        to "INC0001015"). The digit count must be 3-7; pad with
        leading zeros to width 7 ("1015" → "INC0001015").
      - When NO focus exists, return the message unchanged — the
        boundary classifier will ask for a complete id.

Use the conversation so far to identify the referent. The referent is the
most recently mentioned canonical entity id (e.g. `INC0001001`,
`PBM0003003`, `KB0005010`, `CHG0004001`, `AST0001001`, `CI0000001`).

**STRICT recency rule** (critical for multi-hop correctness):
- "Most recent" means the LAST assistant or user turn that named or
  served an entity of the asked-about type. Older entities are stale
  and MUST NOT be used.
- When the user asks about "the related problem", "the linked change",
  "the affected CI", etc. of the CURRENT focus, do NOT substitute a
  specific id from earlier in the conversation. The current focus's
  linked-record id is the user's intent, NOT some older id that happens
  to match the type. Either leave the phrase intact ("the linked
  problem") or attach the CURRENT focus id ("the linked problem of
  INC0001004"). Never paste in a PBM/CHG/AST id from a prior turn that
  was about a different focus.
- If you cannot identify the most-recent focus unambiguously, return
  the message unchanged.

Hard constraints:

  * **Complete, do not answer.** If the message is a question, the
    rewritten message MUST remain a question of the same shape. Never
    convert "what is the priority" into "the priority is P2" or any
    declarative statement; never include the answer that the prior
    assistant turn revealed.
  * **Complete, do not paraphrase.** Keep the original sentence
    structure and wording. The ONLY change permitted is inserting or
    replacing the implicit reference with the canonical entity id.
  * **Never change the user's verb or noun choice.** "what do we know
    about X" must NOT become "summarize X"; "any docs for X" must NOT
    become "find KB for X"; "details about X" must NOT become "summarize
    X"; "info available for X" must NOT become "info about X". The
    verb/noun the user chose is the routing signal — destroying it
    routes the query to the wrong agent. You are NOT classifying intent;
    you are only resolving references.
  * **One entity per completion.** Identify the most recent
    unambiguous entity in the conversation and use that.
  * **Minimum action.** If the message is already self-contained (it
    names a specific entity, or it is a greeting / out-of-scope /
    pleasantry that has no implicit subject), return it unchanged with
    `changed: false`.
  * **Never invent.** If the conversation does not unambiguously support
    a referent, return the message unchanged.
  * **Self-contained subject — DO NOT inject focus.** When the message
    states its OWN concrete subject — a KB search with an explicit
    topic ("find KB about VPN issues", "search articles for outlook
    sync", "any docs on salesforce lag", "how do I reset MFA"), a
    troubleshooting question with a named symptom ("VPN keeps
    dropping"), or any other clause whose subject is already named in
    the message — return it unchanged. Do NOT append the current focus
    record id. The focus would route the query to the wrong UC (a KB
    search bound to an incident id routes to UC-1 summarisation and
    drops the user's actual question). This is the second most common
    rewriter mistake after over-paraphrasing.

Examples (illustrative — apply the PRINCIPLE, not these literal phrases):

  user: "what is the priority of it"
  prior turns named INC0001001
  → "what is the priority of INC0001001"   (changed)

  user: "what is the priority"
  prior turns named INC0001001
  → "what is the priority of INC0001001"   (changed — elided subject filled)

  user: "show me work notes"
  prior turns named INC0001001
  → "show me work notes for INC0001001"    (changed)

  user: "any related changes?"
  prior turns named PBM0003001
  → "any related changes for PBM0003001?"  (changed)

  user: "the incident — who owns it?"
  prior turns named INC0001014
  → "INC0001014 — who owns it?"            (changed — generic noun resolved)

  user: "approved by"
  prior turns named CHG0004001
  → "approved by for CHG0004001"           (changed — bare attribute, no verb,
                                            still a field-read against focus)

  user: "affected CIs"
  prior turns named CHG0004001
  → "affected CIs for CHG0004001"          (changed — bare attribute)

  user: "warranty"
  prior turns named AST0001005
  → "warranty for AST0001005"              (changed — bare attribute)

  user: "owner"
  prior turns named INC0001001
  → "owner of INC0001001"                  (changed — single-word attribute)

  user: "summarize INC0001001"
  → unchanged (already self-contained)

  user: "hi" / "thanks" / "what is the weather"
  → unchanged (no implicit subject)

  *** Bare-digit ID completion ***

  history: summarize INC0001001
  user: "0001015"
  → "summarize INC0001015"                  (bare digits, focus is INC →
                                              prefix inferred, full id emitted)

  history: summarize PBM0003001
  user: "0003007"
  → "summarize PBM0003007"                  (focus is PBM → prefix inferred)

  history: summarize CHG0004001
  user: "1015"
  → "summarize CHG0001015"                  (4 digits → pad to 7 with leading zeros)

  history: (no prior turns)
  user: "0001015"
  → unchanged (no focus to infer service from)

  *** HARD RULE — linked-record phrasing ***

  When the user says "the linked X / the related X / its X / the parent X
  / the affected X" (where X is a record type — problem, change, incident,
  CI, asset, KB), this is a LINKED-RECORD reference, NOT a request to
  substitute a specific id. You MUST:
    1. Keep the phrase "the linked X / the related X / etc." literally
       intact in the output.
    2. Attach the CURRENT focus id (the most recent canonical entity
       you can identify) at the END, like
       "<original> of <CURRENT_FOCUS_ID>".
    3. NEVER substitute a specific X id (e.g. don't replace "the linked
       problem" with "PBM0003003"). The downstream resolver follows the
       focus's actual link field; it must see the literal phrase to do so.

  Examples:
  history: summarize INC0001001 (named PBM0003001 inside its summary)
  user: "root cause of the linked problem"
  → "root cause of the linked problem of INC0001001"  (CORRECT —
                                                        keep phrase,
                                                        attach focus)
  → "root cause of PBM0003001"                        (WRONG — never
                                                        substitute)

  history: summarize INC0001001; summarize PBM0003003
  user: "owner of the related problem"   (focus is now PBM0003003)
  → "owner of PBM0003003"                             (CORRECT — there
                                                        IS no "related
                                                        problem of a
                                                        problem"; the
                                                        user means the
                                                        focused PBM)
  Note: when the current focus IS already the target type (asking about
  "the related problem" when the focus is itself a problem), substitute
  the focus id directly.

  *** STRICT RECENCY — multi-hop hazard ***

  history:
    user:      "summarize INC0001001"
    assistant: (summary that names PBM0003001 in its Related Problem field)
    user:      "who owns the related problem?"
    assistant: (returns the Related Problem field)
    user:      "summarize INC0001004"
    assistant: (summary that names PBM0003002 in its Related Problem field)
  current user: "priority of the linked problem"
  → "priority of the linked problem of INC0001004"   (attach the CURRENT
                                                       focus id; do NOT
                                                       paste PBM0003001
                                                       — that's STALE,
                                                       belongs to an
                                                       older focus)

  history:
    user:      "summarize INC0001004"
    assistant: (summary mentions PBM0003002)
  current user: "priority of PBM0003002"
  → unchanged (user named the id explicitly; nothing to resolve)

Return STRICT JSON only (no markdown fences):
  {"rewritten": "<the message, with implicit references filled>",
   "changed":   true | false,
   "rationale": "<short why>"}
"""


class LlmRewriter:
    """Production rewriter — one gateway call that resolves references against
    the recent conversation. Returns the text unchanged when nothing needs
    resolving (minimum action). A call/parse failure falls back to
    `RewriteResult.unchanged` — a rewrite fault never corrupts the request.
    """

    def __init__(self, gateway, *, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model

    async def rewrite(
        self, text: str, *, history: list[ConversationTurn], request_ctx: dict
    ) -> RewriteResult:

        with _tracer.start_as_current_span(
            "router.stage0b.rewrite",
            attributes={
                "oneops.router.stage": "0b",
                "oneops.router.model": self._model,
                "oneops.router.history_turn_count": len(history or []),
            },
        ) as span:
            result = await self._rewrite_inner(
                text, history=history, request_ctx=request_ctx, _span=span)
            set_langfuse_io(
                span, input=text,
                output={"rewritten": result.text, "changed": result.changed})
            return result

    async def _rewrite_inner(
        self, text: str, *, history: list[ConversationTurn],
        request_ctx: dict, _span,
    ) -> RewriteResult:
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.policy import Profile, compose

        convo = "\n".join(f"{t.role}: {t.content}" for t in history) or "(no prior turns)"
        # Stage 2 (2026-05-28): the authoritative focus comes from the
        # LangGraph state channel (computed deterministically by the
        # executor's update_focus node), not from the LLM scanning
        # history. The LLM rewriter's job is ONLY pronoun/reference
        # resolution against this anchor. Surface focus as an explicit
        # block so the LLM can't drift to an older or assistant-mentioned
        # id.
        focus_id = (request_ctx.get("focus_entity_id") or "").strip()
        focus_service = (request_ctx.get("focus_service_id") or "").strip()
        focus_block = ""
        if focus_id:
            focus_block = (
                f"\n\nCURRENT FOCUS (authoritative — computed deterministically):\n"
                f"  entity_id: {focus_id}\n"
                f"  service:   {focus_service or 'unknown'}\n"
                f"When the message uses a pronoun ('it', 'this', 'that'), "
                f"a bare attribute name ('priority'), or a linked-record "
                f"phrase ('the linked X', 'the related X', 'the affected X'), "
                f"attach the CURRENT FOCUS id above — never any other id "
                f"from history. The current focus is the most recent record "
                f"the user named; any other id mentioned by the assistant "
                f"in summaries is NOT focus.\n"
            )
        user_block = (
            f"Conversation so far:\n{convo}{focus_block}\n\nMessage to resolve:\n{text}"
        )
        # Policy layer on every LLM call (Component Spec C15); static profile →
        # composed prefix is cached and byte-stable.
        system_prompt = compose(Profile.INTERNAL_AGENT,
                                extra_sections=[_REWRITE_PROMPT])
        try:
            response = await self._gateway.call(LlmRequest(
                # System prefix is the policy-composed static portion +
                # rewrite rules — large + stable. Mark for prompt cache.
                messages=(LlmMessage("system", system_prompt,
                                     cache_control=True),
                          LlmMessage("user", user_block)),
                model=self._model,
                tenant_id=request_ctx.get("tenant_id") or "_unknown",
                user_id=request_ctx.get("user_id", "") or "",
                response_format=ResponseFormat.JSON,
                request_id=request_ctx.get("request_id", "")))
            doc = json.loads(response.content)
            rewritten = str(doc.get("rewritten") or text)
            changed = bool(doc.get("changed")) and rewritten != text
            rewritten, changed = self._apply_rewrite_guards(
                text, rewritten, changed, history, request_ctx)
            _span.set_attribute("oneops.router.rewrite.changed", changed)
            return RewriteResult(text=rewritten, changed=changed,
                                 rationale=str(doc.get("rationale", "")))
        except (LLMGatewayError, ValueError, KeyError, TypeError) as exc:
            _span.set_attribute("oneops.router.rewrite.changed", False)
            _span.set_attribute("oneops.router.error", str(exc)[:120])
            _log.warning("rewriter.llm_failed_falling_back", error=str(exc))
            return RewriteResult.unchanged(text)

    def _apply_rewrite_guards(
        self, text: str, rewritten: str, changed: bool,
        history: list[ConversationTurn], request_ctx: dict,
    ) -> tuple[str, bool]:
        """Deterministic post-LLM guard chain — defence-in-depth for prompt
        rules the LLM leaks ~5-10% of the time. Every guard may only RESOLVE
        a reference (a pronoun / bare-digit / linked-phrase becoming a
        concrete id) or REVERT to the user's exact words; none may replace
        the topic. Returns the final `(rewritten, changed)`.

        Order matters:
          1. Self-routable — original already has a canonical id ⇒ nothing
             to resolve; a rewrite would only corrupt the routing verb.
          2. Bare-digit completion when the LLM left it unchanged.
          3. Revert-chain guards (hallucinated id / linked-phrase
             substitution / focus-injection into a self-contained subject /
             topic replacement) — each reverts when it detects a violation.
          4. Pure-rephrase revert — a "change" adding NO new canonical id is
             just a non-deterministic rephrase; revert for stable routing.
          5. Authoritative focus — enforce the LangGraph state focus (or the
             history-scan fallback) over whatever id the LLM chose.
        """
        if changed and _CANONICAL_ID_RE.search(text):
            rewritten, changed = text, False
        if not changed:
            completed = _complete_bare_digit_id(text, history)
            if completed and completed != text:
                rewritten, changed = completed, True
        # Each revert-guard takes the current rewrite and returns a possibly-
        # reverted one; `changed` is recomputed against the original.
        revert_guards = (
            lambda r: _reject_hallucinated_ids(
                original=text, rewritten=r, history=history),
            lambda r: _enforce_linked_phrase_guard(
                original=text, rewritten=r, history=history),
            lambda r: _reject_focus_injection_into_self_contained(
                original=text, rewritten=r),
            lambda r: _reject_topic_replacement(original=text, rewritten=r),
        )
        for guard in revert_guards:
            if changed:
                rewritten = guard(rewritten)
                changed = rewritten != text
        if changed and not (set(_extract_record_ids(rewritten))
                            - set(_extract_record_ids(text))):
            rewritten, changed = text, False
        rewritten = self._enforce_focus(text, rewritten, history, request_ctx)
        changed = rewritten != text
        return rewritten, changed

    def _enforce_focus(
        self, text: str, rewritten: str,
        history: list[ConversationTurn], request_ctx: dict,
    ) -> str:
        """Stage-2 authoritative focus (2026-05-28): for focus-bound messages
        (pronouns / bare-attribute reads / linked-record refs) enforce the
        LangGraph state's `focus_entity_id` — the single source of truth,
        computed deterministically by `executor.update_focus` — over the
        LLM's choice. Fresh session / off-domain (no state focus) falls back
        to the history-scan guard so the safety net stays in place."""
        authoritative_focus = (
            request_ctx.get("focus_entity_id") or "").strip()
        if authoritative_focus:
            return _enforce_authoritative_focus(
                original=text, rewritten=rewritten,
                authoritative_focus=authoritative_focus)
        return _enforce_most_recent_focus_for_focus_bound(
            original=text, rewritten=rewritten, history=history)


# Canonical record-id token (e.g. INC0001003, REQ0000042). Its presence in
# the user's own message means the user is being EXPLICIT about the record —
# the rewriter must not substitute a different focus id (self-routable guard).
_CANONICAL_ID_RE = re.compile(r"\b([A-Z]{2,4}\d{6,})\b")


# Pronouns + back-references that LEGITIMATELY require focus injection.
# Only a message containing one of these (or being a bare attribute name
# / short field-read phrase) should get the focus id appended.
_PRONOUN_RE = re.compile(
    r"\b(?:it|its|this|that|the same|the earlier|the previous|the (?:incident|ticket|change|problem|request|asset|article|record))\b",
    re.IGNORECASE,
)

# Linked-record reference: a relation word + a record-type word.
# Matches "the linked problem", "the related change", "the affected CI",
# "any related changes", "its parent problem", "related problem". These
# REQUIRE the most-recent focus id (the LLM occasionally picks an older
# id from history).
_LINKED_REF_RE = re.compile(
    r"\b(?:the\s+|its\s+|any\s+)?"
    r"(?:linked|related|affected|parent|child)\s+"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)s?\b",
    re.IGNORECASE,
)
# Short bare-attribute messages still get focus injection (their intent is
# clearly "field on the focus"). Width gate: ≤4 content words (matches
# "priority", "what is the priority", "who is the assignee").
_WORD_RE = re.compile(r"\b\w+\b")


# Anything matching `<2-5 letters><any digits>` — a near-canonical id
# the user typed (correctly cased OR lowercase, correct OR wrong digit
# count). The presence of this in the user's message means the user is
# being EXPLICIT about which record they want, so the rewriter must
# NOT substitute a different focus id. If the typed id is malformed
# (wrong digit count), the entity_id normalizer will reject it
# downstream and the handler will return the designed "not found"
# reply — that's the right behaviour, not silent focus rebind.
_NEAR_CANONICAL_RE = re.compile(r"\b[A-Za-z]{2,5}\s*\d+\b")


def is_followup_reference(text: str) -> bool:
    """True only when the message is an EXPLICIT reference to the conversation's
    focused record — a pronoun ('it', 'this', 'that', 'the incident') or a
    linked-record phrase ('the related problem', 'its parent change'). Such a
    message has no subject of its own and needs the focus to supply one.

    Everything else carries its OWN intent and must route on its own merits.
    This is the topic-switch gate (CHIQ / the runbook's "classify each message
    fresh; a user can start a new request mid-conversation"): an independent
    request ('I need a second monitor') is NOT a follow-up, so the focus entity
    must NOT be bound to it — otherwise it inherits the focused record's agent.

    Deliberately card-agnostic and keyword-catalog-free (rule §2.1): it tests
    sentence STRUCTURE (does it point back at something?), never a vocabulary of
    intents/services. Adding the 101st UC needs no change here."""
    if not text:
        return False
    return bool(_PRONOUN_RE.search(text) or _LINKED_REF_RE.search(text))


def _enforce_authoritative_focus(
    *, original: str, rewritten: str, authoritative_focus: str,
) -> str:
    """Stage 2 guard: when the LangGraph state carries a deterministic
    focus_entity_id, force any LLM rewrite that injected a DIFFERENT
    canonical id to use the authoritative one.

    The state focus is the single source of truth (computed by
    `executor.update_focus` from current-message extraction + carried
    state). Any id the LLM picked that disagrees with state focus is
    drift — by definition, since state focus IS the most recent
    user-named record.

    The only id allowed to differ is one the user explicitly typed in
    the CURRENT ORIGINAL message — that is the user being explicit and
    overriding the carried focus for this turn.
    """
    if not rewritten or rewritten == original or not authoritative_focus:
        return rewritten
    rewritten_ids = _extract_record_ids(rewritten)
    if not rewritten_ids:
        return rewritten
    if authoritative_focus in rewritten_ids:
        return rewritten                              # already correct
    original_ids = set(_extract_record_ids(original or ""))
    for stale_id in rewritten_ids:
        if stale_id != authoritative_focus and stale_id not in original_ids:
            corrected = rewritten.replace(stale_id, authoritative_focus)
            _log.info(
                "rewriter.focus_corrected_to_state_authoritative",
                original=original, stale=stale_id,
                corrected=authoritative_focus,
            )
            return corrected
    return rewritten


def _enforce_most_recent_focus_for_focus_bound(
    *, original: str, rewritten: str, history: list[ConversationTurn],
) -> str:
    """When the user's ORIGINAL message is focus-bound (a linked-record
    reference, a pronoun, or a bare-attribute query) and the rewriter
    has injected a focus id, validate that the injected id is the
    MOST RECENT user-named focus. If not, replace it with the
    most-recent one.

    Why: the rewriter's prompt instructs it to use the most recent focus,
    but the LLM's attention drifts on long histories — for sessions with
    multiple entities mentioned, it sometimes anchors on the
    most-discussed id rather than the most-recent one. Example:
      history: ['summarize INC0001004', ..., 'summarize CHG0004003', ...]
      user:    'related problem ?'
      LLM:     'related problem of INC0001004'          ← stale
      truth:   'related problem of CHG0004003'          ← most-recent

    This guard runs AFTER the hallucination + self-contained guards so
    we operate only on rewrites the prior guards accepted. It is a
    surgical fix: it only fires when (a) the original is focus-bound,
    (b) the rewriter chose an id that exists in history but is not the
    most recent user-named focus. Other cases pass through unchanged.

    Tested against the bug class:
      • 'related problem ?'      after CHG0004003 (history also has INC...)
      • 'the linked change ?'    after PBM0003001 (history also has INC...)
      • 'criticality of the affected CI ?' after CHG0004003 (history also has PBM...)
      • 'who owns it ?'          after second entity is now focus
      • bare 'priority' / 'status' / 'owner' after a switched focus
    """
    if not rewritten or rewritten == original:
        return rewritten
    is_focus_bound = (
        bool(_LINKED_REF_RE.search(original or ""))
        or bool(_PRONOUN_RE.search(original or ""))
        or len(_WORD_RE.findall(original or "")) <= 4
    )
    if not is_focus_bound:
        return rewritten
    user_focus_chain = _history_focus_ids_user_only(history)
    if not user_focus_chain:
        return rewritten
    most_recent = user_focus_chain[0]
    rewritten_ids = _extract_record_ids(rewritten)
    if not rewritten_ids:
        return rewritten
    if most_recent in rewritten_ids:
        return rewritten                              # already correct
    # The rewriter picked something other than the most-recent user
    # focus. Correct it UNLESS the chosen id is one the user explicitly
    # named in the CURRENT original message (in that case the user is
    # being explicit and we must not override).
    #
    # Two drift patterns we catch here:
    #   1. older user-named focus       — LLM anchored on most-discussed
    #      id rather than most-recent (e.g. older INC after a CHG switch)
    #   2. assistant-mentioned id       — LLM picked an id that appeared
    #      only in an assistant summary (e.g. a related_incident listed
    #      while summarising a problem). The user never adopted it as
    #      focus; the assistant just enumerated it.
    original_ids = set(_extract_record_ids(original or ""))
    for stale_id in rewritten_ids:
        if stale_id != most_recent and stale_id not in original_ids:
            corrected = rewritten.replace(stale_id, most_recent)
            _log.info("rewriter.focus_corrected_to_most_recent",
                      original=original, stale=stale_id,
                      corrected=most_recent)
            return corrected
    return rewritten


def _history_focus_ids_user_only(
    history: list[ConversationTurn],
) -> list[str]:
    """Most-recent-first list of canonical record ids the USER explicitly
    typed in conversation history. Strictly user-named — assistant
    summaries that mention linked-record ids don't count. This is the
    authoritative signal for 'current focus' when correcting LLM
    rewriter drift."""
    out: list[str] = []
    for turn in reversed(history):
        if (getattr(turn, "role", "") or "").lower() != "user":
            continue
        content = turn.content or ""
        if not content:
            continue
        for hid in _extract_record_ids(content):
            if hid not in out:
                out.append(hid)
    return out


def _reject_topic_replacement(*, original: str, rewritten: str) -> str:
    """Deterministic guard: the rewriter resolves references, it never
    replaces the topic.

    If the original carries a real phrase (>=2 content words, len>=4) and
    the rewrite preserves NONE of them, the LLM swapped in a different
    subject (a prior turn's topic) rather than resolving a reference —
    revert to the original. Short messages (<2 content words, e.g. a bare
    id-completion "1003" -> "INC0001003" or "summarise it") are left to the
    other guards; this one only fires on a clear multi-word topic swap.
    """
    orig_content = {w for w in _WORD_RE.findall(original.lower()) if len(w) >= 4}
    if len(orig_content) < 2:
        return rewritten
    rw_content = {w for w in _WORD_RE.findall(rewritten.lower()) if len(w) >= 4}
    if orig_content & rw_content:
        return rewritten                               # topic preserved
    _log.warning("rewriter.topic_replacement_rejected",
                 original=original, rewritten=rewritten)
    return original


def _reject_focus_injection_into_self_contained(
    *, original: str, rewritten: str
) -> str:
    """Deterministic guard: if the rewriter appended a canonical record
    id to a message that is ALREADY self-contained (has its own concrete
    subject — multi-word, no pronoun, no bare-attribute shape), revert
    to the original.

    Detection heuristic:
      * `new_ids` = canonical record ids in `rewritten` but NOT in
        `original`. If empty, the rewriter didn't inject — return as-is.
      * If `original` contains an implicit-reference pronoun
        (`it`, `this`, `the incident`, etc.) → injection is LEGITIMATE
        (the rewriter resolved the pronoun). Return rewrite.
      * If `original` is short (≤4 content words) → likely a bare
        attribute or short field-read. Injection is LEGITIMATE.
      * Otherwise the message is self-contained; the LLM over-eagerly
        appended focus. Reject — return the original.
    """
    orig_ids = set(_extract_record_ids(original))
    new_ids = set(_extract_record_ids(rewritten)) - orig_ids
    if not new_ids:
        return rewritten
    # Near-canonical guard: the user typed something that LOOKS like a
    # canonical id (e.g. "kb999999", "Inc12345") — even if malformed.
    # That is the user being EXPLICIT about which record they want.
    # The rewriter MUST NOT substitute a different focus id. The
    # entity_id normalizer will reject the malformed token downstream
    # and the handler will emit the designed "not found" reply.
    if _NEAR_CANONICAL_RE.search(original):
        _log.warning(
            "rewriter.focus_injection_rejected_near_canonical",
            original=original, rewritten=rewritten,
            injected_ids=list(new_ids),
        )
        return original
    if _PRONOUN_RE.search(original):
        return rewritten                                # legitimate pronoun resolution
    word_count = len(_WORD_RE.findall(original))
    if word_count <= 4:
        return rewritten                                # bare attribute / short field-read
    _log.warning(
        "rewriter.focus_injection_rejected_self_contained",
        original=original, rewritten=rewritten,
        injected_ids=list(new_ids), word_count=word_count,
    )
    return original


_LINKED_PHRASE_RE = re.compile(
    r"\b(?:the|its|the related|the linked|the parent|the affected)\s+"
    r"(?:linked|related|parent|affected\s+)?"
    r"(?:problem|change|incident|ticket|ci|kb|article|asset|request)\b",
    re.IGNORECASE,
)


def _enforce_linked_phrase_guard(
    *, original: str, rewritten: str, history: list[ConversationTurn],
) -> str:
    """If `original` contains a linked-record phrase ("the linked
    problem", "its related change", …) and `rewritten` introduces a
    canonical entity id that was NOT in `original`, the LLM violated
    the hard rule. Reject the substitution and produce a safe fallback:

      * Identify the MOST RECENT canonical id mentioned in history
        (assistant turns first, then user turns).
      * Return `<original> of <focus_id>` so the downstream resolver
        sees the literal linked-record phrase and can follow the focus's
        actual link field.
      * If no focus id can be identified, return `original` unchanged.

    Idempotent: when the rewriter already produced "...of <focus>"
    correctly, this guard returns the input unchanged.
    """
    if not _LINKED_PHRASE_RE.search(original):
        return rewritten

    orig_ids = set(_extract_record_ids(original))
    new_ids = set(_extract_record_ids(rewritten)) - orig_ids
    if not new_ids:
        # No RECORD-id substitution happened — accept as-is.
        return rewritten

    # Find the most-recent canonical RECORD entity in history. Critical:
    # we use the registry-driven EntityIdNormalizer here so non-record
    # tokens (USR…, GRP-…) are NOT mistaken for focus candidates — the
    # earlier permissive regex matched USR00003 and broke multi-hop.
    focus_candidates = _history_focus_ids(history)
    focus_id = focus_candidates[0] if focus_candidates else ""

    _log.warning(
        "rewriter.linked_phrase_substitution_rejected",
        original=original, rewritten=rewritten,
        substituted_ids=list(new_ids), focus_id=focus_id,
    )
    if focus_id:
        return f"{original} of {focus_id}"
    return original


_BARE_DIGIT_RE = re.compile(r"^\s*(\d{3,7})\s*$")


def _bare_digit_allowed_ids(
    original: str, new_ids: set[str], history: list[ConversationTurn],
) -> set[str]:
    """Phase N: a bare-digit original (e.g. '0001002') may legitimately produce
    a canonical id whose digit-body matches, IF history names a record of the
    same prefix (proving the service context was inferable). Returns the subset
    of `new_ids` allowed under this rule (empty when the original isn't bare
    digits or no prefix matches)."""
    bare_digit_match = _BARE_DIGIT_RE.match(original)
    if not bare_digit_match:
        return set()
    digit_body = bare_digit_match.group(1).zfill(7)
    history_prefixes: set[str] = set()
    for turn in history:
        for hid in _extract_record_ids(turn.content or ""):
            pm = re.match(r"^([A-Z]{2,5})", hid)
            if pm:
                history_prefixes.add(pm.group(1))
    allowed: set[str] = set()
    for nid in new_ids:
        m = re.match(r"^([A-Z]{2,5})(\d{4,})$", nid)
        if m and m.group(2).lstrip("0") == digit_body.lstrip("0") \
                and m.group(1) in history_prefixes:
            allowed.add(nid)
    return allowed


def _reject_hallucinated_ids(
    *, original: str, rewritten: str, history: list[ConversationTurn],
) -> str:
    """Reject ANY rewriter substitution that introduces a canonical
    record id (INC…, PBM…, CHG…, …) not present in conversation
    history or in the original message.

    Production-grade safety net: prevents the LLM rewriter from
    confidently filling in a default id (e.g. INC0001001) when there
    is no focus to bind to. Without this guard, "what is the priority"
    on a fresh session might be rewritten to "what is the priority of
    INC0001001" — and routing would happily serve INC0001001's
    priority instead of asking which ticket the user means.

    Allowed substitutions:
      * ids that already exist verbatim in the original message;
      * ids that exist in any history turn's content (the user or
        assistant previously named them).

    Disallowed:
      * any other canonical record id appearing only in the rewrite.
    """
    new_ids = set(_extract_record_ids(rewritten))
    if not new_ids:
        return rewritten
    allowed = set(_extract_record_ids(original))
    # Use the filtered focus set — assistant clarification messages that
    # contain example ids (e.g. "Please share its id like INC0001234")
    # must NOT seed the allowed set, otherwise the rewriter binds
    # subsequent attribute queries to a fake id.
    allowed.update(_history_focus_ids(history))

    # Phase N exemption: a bare-digit original may legitimately produce an id
    # whose digit-body matches (e.g. "0001002" → INC0001002), as long as
    # history names a record of the same prefix (proving service context was
    # inferable). Without history there is nothing to bind the prefix to anyway.
    allowed.update(_bare_digit_allowed_ids(original, new_ids, history))

    hallucinated = new_ids - allowed
    if not hallucinated:
        return rewritten
    _log.warning(
        "rewriter.hallucinated_id_rejected",
        original=original, rewritten=rewritten,
        hallucinated=sorted(hallucinated),
    )
    return original


def _history_focus_ids(history: list[ConversationTurn]) -> list[str]:
    """Most-recent-first list of canonical record ids the user is
    actively focused on. Discipline (2026-05-27):

      * **User-turn ids come first.** Ids the user EXPLICITLY typed
        (e.g. `summarize PBM0003001`) are the authoritative focus
        signal. An assistant summary may incidentally mention several
        linked ids (`PBM0003001` → "Related Change CHG0004001"); those
        secondary ids must NOT outrank the user-named subject.
      * **Assistant-turn ids come second.** They cover cases where the
        user used a pronoun ("summarize it") — the assistant's prior
        turn named the subject; we still want it as a fallback focus.
      * **Clarification templates are skipped.** Example ids inside
        "Please share its id, e.g. INC0001234" must not seed focus,
        otherwise the next bare-attribute turn binds to the example.

    Returns user ids (most-recent-first) followed by assistant ids
    (most-recent-first), de-duplicated.
    """
    user_ids: list[str] = []
    asst_ids: list[str] = []
    for turn in reversed(history):
        role, ids = _collect_turn_ids(turn)
        if role == "assistant":
            asst_ids.extend(h for h in ids
                            if h not in asst_ids and h not in user_ids)
        else:                                               # user turns
            user_ids.extend(h for h in ids if h not in user_ids)
    # Concat: user-named first (authoritative), assistant-named second
    # (pronoun-resolution fallback).
    return _dedup_preserve_order(user_ids + asst_ids)


# Assistant clarification templates — their EXAMPLE ids ("e.g. INC0001234")
# must not seed focus, or the next bare-attribute turn binds to the example.
_CLARIFICATION_MARKERS = (
    "which ticket", "which record", "please share its id",
    "please share the record id", "please send the record id",
    "share its id",
)


def _is_clarification_prompt(content_lower: str) -> bool:
    """True when an assistant turn is a clarification template (its example
    ids must not be treated as focus)."""
    return any(m in content_lower for m in _CLARIFICATION_MARKERS)


def _collect_turn_ids(turn: ConversationTurn) -> tuple[str, list[str]]:
    """`(role, record-ids)` for one turn — ids empty when the turn should be
    skipped (no content, or an assistant clarification template)."""
    content = turn.content or ""
    if not content:
        return "", []
    role = (getattr(turn, "role", "") or "").lower()
    if role == "assistant" and _is_clarification_prompt(content.lower()):
        return role, []
    return role, _extract_record_ids(content)


def _dedup_preserve_order(ids: list[str]) -> list[str]:
    """De-duplicate while preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for hid in ids:
        if hid not in seen:
            seen.add(hid)
            out.append(hid)
    return out


def _complete_bare_digit_id(
    original: str, history: list[ConversationTurn]
) -> str:
    """Phase N — bare-digit ID completion.

    When the user types just a digit string (3-7 digits, no prefix),
    infer the service from the most-recent canonical record id in
    history and emit the full canonical id (prefix + 7-digit-zero-
    padded number). Returns "" when:
      * the message is not a bare digit string, OR
      * no canonical record id appears in history (no focus to
        infer from — the boundary classifier asks for a full id).

    Production-grade: registry-driven (the prefix list comes from
    `EntityIdNormalizer.from_registry_file`); never a hard-coded
    prefix catalog in code.

    Examples:
      history=[…INC0001001…], text="0001015" → "INC0001015"
      history=[…CHG0004001…], text="1015"    → "CHG0001015"
      history=[],             text="0001015" → ""  (no focus)
      text="hello"                            → ""  (not bare digits)
    """
    if not original or not history:
        return ""
    m = _BARE_DIGIT_RE.match(original)
    if not m:
        return ""
    digits = m.group(1)
    focus_candidates = _history_focus_ids(history)
    focus_id = focus_candidates[0] if focus_candidates else ""
    if not focus_id:
        return ""
    # Extract the alphabetic prefix from the focus id (e.g. "INC", "PBM").
    prefix_match = re.match(r"^([A-Z]{2,5})", focus_id)
    if not prefix_match:
        return ""
    prefix = prefix_match.group(1)
    # Pad to canonical 7-digit width.
    padded = digits.zfill(7)
    return f"{prefix}{padded}"


def _extract_record_ids(text: str) -> list[str]:
    """Extract canonical work-record ids (INC0001234, PBM0003001,
    CHG0004001, …) via the registry's `EntityIdNormalizer`. This is
    the single source of truth for "what counts as a record id" —
    never a regex, because the regex shape `[A-Z]{2,5}\\d{4,}` also
    matches `USR00003`, `GRP00001`, etc., which would silently
    poison focus selection.

    Returns the ids in document order; empty list when no record id
    is present (or when the registry cannot be loaded — defensive
    fall-through, the guard becomes a no-op rather than crashing).
    """
    if not text:
        return []
    try:
        from oneops.router.entity_id import EntityIdNormalizer
        normalizer = EntityIdNormalizer.from_registry_file()
        return [e.entity_id for e in normalizer.extract(text).entities]
    except Exception:                                       # noqa: BLE001 — boundary
        return []


__all__ = [
    "ConversationTurn",
    "RewriteResult",
    "Rewriter",
    "PassthroughRewriter",
    "LlmRewriter",
]
