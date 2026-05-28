"""UC-1 field-read branch — extract the subset of canonical Key Detail
labels the user asked for in this turn, given the active record's
humanised label set.

The extractor is one LLM call with a stable, cacheable system prompt
composed through the policy layer. The principle: **no user-phrase
catalog**. The prompt names the rule ("return the labels the user is
asking for") and lets the LLM resolve synonyms semantically against the
structural label list. This survives novel phrasings without code edits.

Output is a typed `FieldReadIntent` carrying:
  * `labels`: ordered list of canonical labels (subset of available_labels).
  * `via_link`: when non-empty, the focus-record label that holds the id
    of a linked record the user actually wants. The handler then fetches
    that linked record and reads `labels` on IT (2-hop traversal).

Pluggability: the public seam is `extract_requested_fields(...)`. The
LLM-backed implementation is injected at app boot via
`set_field_read_llm(fn)`; tests inject a stub.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from oneops.observability import get_logger

_log = get_logger("oneops.use_cases.uc01.field_read")


# ── Deterministic fast-path (Stage 1, 2026-05-28) ─────────────────────
# Schema-aware synonym layer. Production-grade industry pattern (Moveworks
# typed Resolvers, hybrid NLU+LLM): map common attribute aliases to an
# ORDERED candidate list; the actual schema (the focus record's humanised
# label set) decides which candidate wins. The LLM call is preserved as
# the long-tail fallback so unseen paraphrases (e.g. "give me the
# rundown on its current state") still work.
#
# Design rules:
#   * Patterns map to a CANDIDATE LIST. The first candidate that exists
#     in the focus's available_labels wins. This is how "priority" → on
#     an incident gets "Priority", on a change gets "Risk Level" — no
#     per-service branching, the schema decides.
#   * Conservative bail-outs: linked-record references and whole-record
#     verbs route to the LLM path (they need richer judgment).
#   * Multiple matches accumulate (multi-field reads like "priority and
#     status" produce both labels in order).
#   * Patterns are ordered by specificity — multi-word phrases before
#     single tokens so "risk level" doesn't trigger both "risk" and
#     "level" rules.

# (regex, candidate canonical labels in priority order)
_SYNONYMS: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    # — multi-word phrases (specific) — must come before single tokens —
    (re.compile(r"\brisk\s+level\b", re.I),         ("Risk Level",)),
    (re.compile(r"\broot\s+cause\b", re.I),         ("Root Cause",)),
    (re.compile(r"\bassign(?:ed|ment)\s+(?:to|group)\b", re.I),
                                                    ("Assigned To", "Assignment Group")),
    (re.compile(r"\bassignment\s+group\b", re.I),   ("Assignment Group",)),
    (re.compile(r"\bsla\s+(?:due|deadline)\b", re.I), ("SLA Due",)),
    (re.compile(r"\bsla\s+breached\b", re.I),       ("SLA Breached",)),
    (re.compile(r"\bapproval\s+status\b", re.I),    ("Approval Status",)),
    (re.compile(r"\bapproved\s+by\b", re.I),        ("Approved By",)),
    (re.compile(r"\brequested\s+by\b", re.I),       ("Requested By",)),
    (re.compile(r"\breported\s+by\b", re.I),        ("Reported By",)),
    (re.compile(r"\bcreated\s+at\b", re.I),         ("Created At",)),
    (re.compile(r"\bupdated\s+at\b", re.I),         ("Updated At",)),
    (re.compile(r"\bresolved\s+at\b", re.I),        ("Resolved At",)),
    (re.compile(r"\bplanned\s+(?:start|end)\b", re.I),
                                                    ("Planned Start", "Planned End")),
    (re.compile(r"\bconfiguration\s+item\b", re.I), ("Configuration Item",)),
    (re.compile(r"\blinked\s+cis?\b", re.I),        ("Linked CIs", "Configuration Item")),
    (re.compile(r"\baffected\s+cis?\b", re.I),      ("Affected CIs",)),
    (re.compile(r"\bwork\s+notes?\b", re.I),        ("Work Notes",)),
    (re.compile(r"\bwhat\s+state\s+is\s+it\s+in\b", re.I),
                                                    ("Status", "State")),
    (re.compile(r"\bwhere\s+does\s+it\s+(?:currently\s+)?stand\b", re.I),
                                                    ("Status", "State")),
    (re.compile(r"\bwhen\s+was\s+it\s+(?:created|opened|raised)\b", re.I),
                                                    ("Created At",)),
    (re.compile(r"\bwhen\s+was\s+it\s+(?:updated|modified|last\s+modified)\b", re.I),
                                                    ("Updated At",)),
    (re.compile(r"\bwhen\s+was\s+it\s+resolved\b", re.I),
                                                    ("Resolved At",)),
    (re.compile(r"\bwho\s+(?:owns|owned|is\s+(?:assigned|handling|working\s+on)|handles)\b", re.I),
                                                    ("Owner", "Assigned To")),
    (re.compile(r"\bwho\s+(?:approved|signed\s+off)\b", re.I),
                                                    ("Approved By",)),
    (re.compile(r"\bwho\s+(?:reported|raised|opened|filed)\b", re.I),
                                                    ("Reported By",)),
    (re.compile(r"\bwho\s+requested\b", re.I),      ("Requested By", "Reported By")),
    # — single tokens (general) —
    (re.compile(r"\bpriority\b", re.I),             ("Priority", "Risk Level")),
    (re.compile(r"\bseverity\b", re.I),             ("Severity",)),
    (re.compile(r"\bimpact\b", re.I),               ("Impact",)),
    (re.compile(r"\burgency\b", re.I),              ("Urgency",)),
    (re.compile(r"\bcategory\b", re.I),             ("Category",)),
    (re.compile(r"\bsubcategory\b", re.I),          ("Subcategory",)),
    (re.compile(r"\bstatus\b", re.I),               ("Status", "State")),
    (re.compile(r"\bstate\b", re.I),                ("State", "Status")),
    (re.compile(r"\btype\b", re.I),                 ("Type",)),
    (re.compile(r"\bowner\b", re.I),                ("Owner", "Assigned To")),
    (re.compile(r"\bassignee\b", re.I),             ("Assigned To",)),
    (re.compile(r"\brisk\b", re.I),                 ("Risk Level",)),
    (re.compile(r"\bcriticality\b", re.I),          ("Criticality",)),
    (re.compile(r"\bimportance\b", re.I),           ("Priority", "Risk Level", "Criticality")),
    (re.compile(r"\brca\b", re.I),                  ("Root Cause",)),
    (re.compile(r"\bworkaround\b", re.I),           ("Workaround",)),
    (re.compile(r"\bservice\b", re.I),              ("Service",)),
    (re.compile(r"\bgroup\b", re.I),                ("Assignment Group",)),
    (re.compile(r"\bteam\b", re.I),                 ("Assignment Group",)),
    (re.compile(r"\bcaller\b", re.I),               ("Reported By",)),
    (re.compile(r"\brequester\b", re.I),            ("Requested By", "Reported By")),
    (re.compile(r"\breporter\b", re.I),             ("Reported By",)),
    (re.compile(r"\bsla\b", re.I),                  ("SLA Due", "SLA")),
    (re.compile(r"\bbreached\b", re.I),             ("SLA Breached",)),
    (re.compile(r"\boverdue\b", re.I),              ("SLA Breached",)),
    (re.compile(r"\bdue\b", re.I),                  ("SLA Due",)),
    (re.compile(r"\bopened\b", re.I),               ("Created At",)),
    (re.compile(r"\bcreated\b", re.I),              ("Created At",)),
    (re.compile(r"\bupdated\b", re.I),              ("Updated At",)),
    (re.compile(r"\bresolved\b", re.I),             ("Resolved At",)),
    (re.compile(r"\bwarranty\b", re.I),             ("Warranty Expiry", "Warranty")),
    (re.compile(r"\bvendor\b", re.I),               ("Vendor",)),
    (re.compile(r"\bmodel\b", re.I),                ("Model",)),
    (re.compile(r"\btitle\b", re.I),                ("Title", "Name")),
]

# Bail patterns: when present, defer to the LLM. The deterministic path
# is conservative — it never fires for queries that need richer judgment.
_LINKED_RECORD_BAIL = re.compile(
    # Two word orders — must mirror the version in tools.py:
    #   (a) "<relation> <record-type>"  — "linked problem", "related change"
    #   (b) "<record-type> linked to"   — "problem linked to INC0001001"
    r"\b(?:"
    r"(?:the\s+|its\s+|any\s+)?"
    r"(?:linked|related|affected|parent|child)\s+"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)s?"
    r"|"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)s?"
    r"\s+linked\s+to"
    r")\b",
    re.I,
)
_WHOLE_RECORD_BAIL = re.compile(
    r"\b(?:summari[sz]e|describe|explain|tell\s+me\s+about|walk\s+me\s+through|"
    r"what\s+happened|what\s+is\s+going\s+on|background|context|rundown|"
    r"give\s+me\s+the\s+(?:rundown|context|details)|"
    r"details?\s+(?:of|about)|what\s+do\s+we\s+know)\b",
    re.I,
)


def _try_deterministic_extract(
    user_message: str, available_labels: list[str],
) -> "FieldReadIntent | None":
    """Schema-aware deterministic extraction. Returns None to defer to
    the LLM fallback. Returns FieldReadIntent on a confident match.

    Algorithm:
      1. Bail if message has a linked-record reference (needs LLM via_link).
      2. Bail if message has a whole-record verb (return labels=[] via LLM
         to keep consistent full-summary path).
      3. For each synonym pattern in declaration order, if it matches the
         message, append the FIRST candidate that exists in
         available_labels (preserving order, no duplicates).
      4. If any labels matched → return single-hop FieldReadIntent.
         Otherwise → None (LLM fallback).
    """
    if not user_message or not available_labels:
        return None
    if _LINKED_RECORD_BAIL.search(user_message):
        return None
    if _WHOLE_RECORD_BAIL.search(user_message):
        return None
    available_set = {lbl: lbl for lbl in available_labels}
    matched: list[str] = []
    asked_aliases: list[str] = []           # synonym hits the user typed
    for pattern, candidates in _SYNONYMS:
        m = pattern.search(user_message)
        if not m:
            continue
        asked_aliases.append(m.group(0))
        for cand in candidates:
            if cand in available_set and cand not in matched:
                matched.append(cand)
                break
    if not matched:
        # The user clearly asked for a specific field (synonym hit) but
        # NO candidate label exists on the focus record's schema. Tell
        # the UC-1 handler so it can render a clean "this record type
        # doesn't expose <X>" response instead of falling through to a
        # full-summary dump.
        if asked_aliases:
            return FieldReadIntent(unavailable_field=asked_aliases[0])
        return None
    return FieldReadIntent(
        labels=tuple(matched), via_link="", via_link_known=False,
    )


@dataclass(frozen=True)
class FieldReadIntent:
    """Structured output of the field extractor.

    `labels` are the canonical labels to render. When `via_link` is set,
    those labels apply to the LINKED record, not the focus. When `labels`
    is empty AND `via_link` is set, the handler should produce a full
    summary of the linked record (the user said something like "tell me
    about the linked change"). When both are empty, the caller falls
    back to the full-summary path on the focus.

    `via_link_known` distinguishes two distinct linked-record scenarios:
      * True  → `via_link` IS a real label on the focus record. The
                handler should follow the link (2-hop traversal) or
                surface "no value" if the link field is empty on this
                particular record.
      * False → the LLM proposed a link (`via_link` is set) but the
                focus record has no such field. The handler MUST surface
                that mismatch ("INC0001030 has no Related Problem;
                this is a security/SSO incident") instead of silently
                falling through to a single-hop on the focus or a
                full summary. Required to honour the "no silent
                failure" thumb rule when the user explicitly named a
                link the focus doesn't expose.
    """

    labels: tuple[str, ...] = ()
    via_link: str = ""
    via_link_known: bool = False
    # When the user clearly asked for a SPECIFIC field by name (synonym
    # match in the deterministic extractor) but the focus record's
    # schema doesn't expose that field, the deterministic layer reports
    # the asked-for alias here. The UC-1 handler renders a clean
    # "this record type doesn't expose <X>" response instead of falling
    # through to a full summary. Empty when the requested field is
    # available or when the user asked for a whole-record read.
    unavailable_field: str = ""


# fn(user_message, available_labels, tenant_id, model, user_id="") -> FieldReadIntent
# `user_id` is keyword-only with default; older injectors without it still work.
FieldReadFn = Callable[..., Awaitable[FieldReadIntent]]


_fn: FieldReadFn | None = None


def set_field_read_llm(fn: FieldReadFn | None) -> None:
    """Inject (or clear) the LLM-backed field extractor."""
    global _fn
    _fn = fn


def _get_fn() -> FieldReadFn | None:
    return _fn


_SYSTEM_PROMPT = """You are the field-extractor for an ITSM record viewer. \
The user is in a multi-turn conversation with a record already in focus. \
Given the user's CURRENT message and the structural list of Key Detail \
labels actually present on that record, return EVERY label (verbatim from \
that list) the user is asking for in THIS turn.

## Output shape

Return STRICT JSON: {"labels": [...], "via_link": "<link label or null>"}

- "labels" is the list of CANONICAL labels (from available_labels) the \
user is asking about. Possibly empty when the message is a full-summary \
request or unrelated.
- "via_link" is non-null ONLY when the user wants a field of a LINKED \
record reached through the current focus (e.g. "priority of the linked \
problem of INC0001004" — the user wants the linked problem's priority, \
not the incident's). Set "via_link" to the EXACT label in the focus \
record that holds the link (e.g. "Related Problem", "Related Change", \
"Parent Problem", "Linked CI"). The handler will follow that link, fetch \
the linked record, and answer "labels" on THAT record.
- When "via_link" is set, "labels" names the target fields on the linked \
record, NOT on the focus.

## Reasoning steps (think before you answer)

1. **Identify the asked attributes** in the user message. Look for nouns \
that name a property of an ITSM record (priority, status, owner, group, \
risk, SLA, dates, root cause, …). A connective ("and", comma) joining \
attribute names signals multi-field.
1a. **Detect a linked-record hop**: phrases like "of the linked X", "of \
the related X", "of its X", "of its parent X", "of the affected X" \
indicate a 2-hop query: the user wants a field of the LINKED entity X, \
not of the current focus. Set "via_link" to the focus-record label that \
points to X (e.g. "Related Problem" when the user says "the linked \
problem"). If no linked-record phrasing is present, "via_link" is null.
2. **Resolve synonyms semantically** against the provided label list. \
Common mappings (apply structurally, not as a keyword catalog):
   - importance / criticality / priority → Priority (or Risk Level on changes)
   - owner / assignee / who's assigned / who's handling → Assigned To
   - state / current state → Status (or State on changes)
   - group / team / squad → Assignment Group
   - due / due date / sla / sla deadline → SLA Due
   - breached / sla breached / overdue → SLA Breached
   - created / when was it opened / opened at → Created At
   - updated / last modified → Updated At
   - caller / requester / reporter / reported by → Reported By
   - linked CIs / affected CIs / CI / configuration items → Linked CIs OR Affected CIs (whichever the label list contains)
   - approved by / approvers / sign-off → Approved By
   - risk / risk rating / how risky → Risk Level
   - root cause / cause / why did it happen → Root Cause
   - workaround / temp fix → Workaround
3. **Cross-type fallback**: if the user asks for "priority" on a record \
type that has no "Priority" label but has "Risk Level" (typically a \
change), map priority → Risk Level. If the user asks for "status" on a \
change that has "State" instead, map status → State. The principle: pick \
the closest semantic match that EXISTS in the provided label list.
4. **Bare attribute names** (no verb, no question mark) still count as \
field-reads when focus is active. "approved by" / "affected CIs" / \
"warranty" / "owner" — these are valid targeted asks. Treat them as \
field-reads, not as ambiguous.
5. **Multi-field**: if multiple attributes are named, return ALL of them \
in the order the user mentioned them. Never collapse to the first.
6. **Coverage**: any attribute the user named must either appear in the \
result or be unmappable. If a field genuinely does not exist on this \
record and has no semantic equivalent, omit it (the renderer will say \
"not available on this <service>").
7. **Full-summary fallback**: "summarize / tell me about / give me the \
details / overview / what happened" → empty list. The handler falls \
through to the full summary card.

## Few-shot examples (study the contrast)

INPUT user_message: "what is the priority?"
INPUT available_labels: ["Status","Priority","Severity","Reported By", …]
OUTPUT: {"labels":["Priority"],"via_link":null}

INPUT user_message: "priority"
INPUT available_labels: ["Status","Priority","Severity","Reported By", …]
OUTPUT: {"labels":["Priority"],"via_link":null}
(bare attribute, no verb or question mark — still a field-read on the focus)

INPUT user_message: "priority of INC0001001"
INPUT available_labels: ["Status","Priority","Severity", …]
OUTPUT: {"labels":["Priority"],"via_link":null}
(the "of INC0001001" suffix is a focus reaffirmation from the rewriter,
 NOT a via_link — via_link is ALWAYS a focus-record FIELD LABEL like
 "Related Problem" / "Related Change" / "Parent Problem", NEVER a
 canonical record id)

INPUT user_message: "what's the importance?"
INPUT available_labels: ["Status","Priority", …]
OUTPUT: {"labels":["Priority"],"via_link":null}

INPUT user_message: "what is the priority and when was it created"
INPUT available_labels: ["Type","Risk Level","State","Approval Status","Approved By","Created At","Updated At", …]   (NOTE: no "Priority" — this is a change record)
OUTPUT: {"labels":["Risk Level","Created At"],"via_link":null}
(reasoning: priority → Risk Level on change; created → Created At; both kept; no linked-record phrasing)

INPUT user_message: "approved by"
INPUT available_labels: ["Type","Risk Level","State","Approved By","Affected CIs", …]
OUTPUT: {"labels":["Approved By"],"via_link":null}

INPUT user_message: "affected CIs"
INPUT available_labels: ["Type","Risk Level","State","Affected CIs", …]
OUTPUT: {"labels":["Affected CIs"],"via_link":null}

INPUT user_message: "priority and status"
INPUT available_labels: ["Status","Priority","Severity", …]
OUTPUT: {"labels":["Priority","Status"],"via_link":null}

INPUT user_message: "summarize it" / "tell me about INC0001001" / "hello"
OUTPUT: {"labels":[],"via_link":null}

*** Linked-record hops (via_link is the focus-record's link label) ***

INPUT user_message: "priority of the linked problem of INC0001004"
INPUT available_labels: ["Status","Priority","Severity","Related Problem","Related Change", …]
OUTPUT: {"labels":["Priority"],"via_link":"Related Problem"}
(reasoning: user wants the linked PROBLEM's priority; via_link is the
 focus label that holds the problem id; "labels" names what to read on
 the linked record)

INPUT user_message: "who owns the related problem"
INPUT available_labels: ["Status","Priority","Assigned To","Related Problem", …]
OUTPUT: {"labels":["Owner"],"via_link":"Related Problem"}
(reasoning: owner of the related problem → linked PBM's Owner)

INPUT user_message: "tell me about the linked change"
INPUT available_labels: ["Status","Priority","Related Change","Related Problem", …]
OUTPUT: {"labels":[],"via_link":"Related Change"}
(reasoning: full summary of the linked change — labels empty so the
 handler will summarise the linked record fully, not a single field)

INPUT user_message: "risk of its linked change"
INPUT available_labels: [..., "Related Change"]
OUTPUT: {"labels":["Risk Level"],"via_link":"Related Change"}

INPUT user_message: "owner of the parent problem"
INPUT available_labels: [..., "Parent Problem"]
OUTPUT: {"labels":["Owner"],"via_link":"Parent Problem"}

## Hard rules

- Only return labels that exist VERBATIM in the available_labels list. \
The cross-type fallback (step 3) picks an EXISTING label that is the \
semantic equivalent — never invent.
- Preserve user-mention order.
- Never return [] for a clearly targeted field-read; that drops the user's intent.
- **`via_link` is a focus-record LINK LABEL** (e.g. "Related Problem", \
"Related Change", "Parent Problem", "Affected CIs"). It is NEVER a \
canonical record id (`INC0001001`, `PBM0003001`, etc.) and NEVER any \
other kind of label. If you cannot identify a clear link label, return \
`null` for `via_link`.

Return STRICT JSON ONLY: {"labels": ["Label A", "Label B"]}"""


class LlmFieldReadExtractor:
    """Production extractor — one gateway call returning the label subset.

    Falls back to an empty list (= "not a field-read") on any LLM or
    parse failure. A failure here is not a fault: the handler's
    summarise path is a complete answer to any inbound question.
    """

    def __init__(self, gateway, *, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model

    async def extract(
        self, user_message: str, available_labels: list[str],
        tenant_id: str, model: str, user_id: str = "",
    ) -> FieldReadIntent:
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.policy import Profile, compose

        if not user_message or not user_message.strip():
            return FieldReadIntent()
        if not available_labels:
            return FieldReadIntent()

        # Stage 1 (2026-05-28): try the deterministic schema-aware
        # fast-path first. On a confident match, skip the LLM call
        # entirely — saves ~1s of LLM latency and removes the LLM's
        # speculative `via_link` mistakes for fields that live on the
        # focus record. On a miss, fall through to the LLM (preserved
        # for paraphrase and long-tail handling).
        deterministic = _try_deterministic_extract(user_message, list(available_labels))
        if deterministic is not None:
            _log.info("uc01.field_read.extraction_path",
                      path="deterministic",
                      user_message=user_message[:100],
                      labels=list(deterministic.labels))
            return deterministic
        _log.info("uc01.field_read.extraction_path",
                  path="llm_fallback",
                  user_message=user_message[:100])

        system_prompt = compose(Profile.INTERNAL_AGENT,
                                extra_sections=[_SYSTEM_PROMPT])
        user_block = json.dumps({
            "user_message": user_message,
            "available_labels": list(available_labels),
        })
        chosen_model = (model or self._model or "gpt-4o-mini").strip()
        try:
            response = await self._gateway.call(LlmRequest(
                messages=(
                    LlmMessage("system", system_prompt,
                               cache_control=True),
                    LlmMessage("user", user_block),
                ),
                model=chosen_model,
                tenant_id=tenant_id or "_unknown",
                user_id=user_id or "",
                response_format=ResponseFormat.JSON,
                request_id=""))
            doc = json.loads(response.content)
            raw_labels = doc.get("labels") or []
            raw_via = doc.get("via_link") or ""
            _log.info("uc01.field_read.llm_returned",
                      user_message=user_message[:100],
                      raw_labels=raw_labels,
                      via_link=raw_via)
            if not isinstance(raw_labels, list):
                raw_labels = []
            # Carry the LLM's via_link choice through to the handler
            # VERBATIM. We tag it with `via_link_known` so the handler
            # can distinguish three cases:
            #   1. via_link present + in available_labels → real link
            #      on this focus → do 2-hop.
            #   2. via_link present + NOT in available_labels → user
            #      asked for a link this focus type doesn't have →
            #      surface a clear mismatch message ("INC0001030 has
            #      no Related Problem"). NEVER silently fall through
            #      to single-hop or summary.
            #   3. via_link absent → normal single-hop or summary.
            via_link_raw = ""
            via_link_known = False
            if isinstance(raw_via, str) and raw_via.strip():
                via_link_raw = raw_via.strip()
                # Defence-in-depth: via_link must be a FIELD LABEL on the
                # focus record, never a canonical record id. Reject ids
                # the LLM may have leaked into this slot (e.g. "INC0001001"
                # / "PBM0003001"). 2026-05-27: spotted the LLM returning
                # the focus-id as via_link when the rewriter appended
                # "of INC0001001" to a bare "priority" message.
                import re as _re
                if _re.fullmatch(r"[A-Z]{2,6}\d{4,}", via_link_raw):
                    _log.info(
                        "uc01.field_read.via_link_id_rejected",
                        via_link=via_link_raw,
                        reason="canonical-id-shape not a link label")
                    via_link_raw = ""
                via_link_known = (via_link_raw != "" and
                                  via_link_raw in set(available_labels))
                if not via_link_known:
                    _log.info("uc01.field_read.via_link_unknown",
                              via_link=via_link_raw,
                              available=list(available_labels))
            if via_link_raw:
                # via_link set (known OR unknown): pass labels through
                # verbatim — they target the LINKED record's label set,
                # which we cannot validate here.
                labels = tuple(
                    str(x) for x in raw_labels
                    if isinstance(x, str) and x.strip())
            else:
                allowed = set(available_labels)
                seen: set[str] = set()
                kept: list[str] = []
                for item in raw_labels:
                    if not isinstance(item, str):
                        continue
                    if item in allowed and item not in seen:
                        seen.add(item)
                        kept.append(item)
                labels = tuple(kept)
            return FieldReadIntent(
                labels=labels,
                via_link=via_link_raw,
                via_link_known=via_link_known,
            )
        except (LLMGatewayError, ValueError, KeyError, TypeError) as exc:
            _log.warning("uc01.field_read.extract_failed",
                         error=str(exc)[:200])
            return FieldReadIntent()


async def extract_requested_fields(
    user_message: str, available_labels: list[str],
    *, tenant_id: str = "", user_id: str = "", model: str = "",
) -> FieldReadIntent:
    """Public seam used by the handler. Returns an empty intent when no
    extractor is wired (test paths) — the handler falls through to
    summarise.

    `available_labels` is the *exact* canonical label set the user can
    target on the focus record, derived from `humanise_record(record)`.
    The extractor never invents labels — it returns a subset of this
    list (or an empty list) for `labels`, and optionally a label from
    this same list for `via_link` (the field that points to a linked
    record).
    """
    fn = _get_fn()
    if fn is None:
        return FieldReadIntent()
    if not user_message or not user_message.strip():
        return FieldReadIntent()
    return await fn(user_message, list(available_labels), tenant_id, model,
                    user_id=user_id)


def render_field_read(
    record_view: dict[str, Any], requested_labels: list[str],
    service_id: str,
) -> str:
    """Deterministically format the chosen labels into a user reply.

    Single label → `Label: Value`. Multiple labels → bullet list.
    Missing values (label not present on this record, or empty after
    humanisation) become `Label: not available on this {service}.` so
    the user is never silently dropped.
    """
    if not requested_labels:
        return ""
    pairs: list[tuple[str, str]] = []
    for label in requested_labels:
        if label in record_view:
            value = record_view[label]
            value_str = ", ".join(value) if isinstance(value, list) else str(value)
            pairs.append((label, value_str))
        else:
            pairs.append((label, f"not available on this {service_id}."))
    if len(pairs) == 1:
        label, value = pairs[0]
        return f"{label}: {value}"
    return "\n".join(f"- {label}: {value}" for label, value in pairs)


__all__ = [
    "FieldReadIntent",
    "FieldReadFn",
    "LlmFieldReadExtractor",
    "set_field_read_llm",
    "extract_requested_fields",
    "render_field_read",
]
