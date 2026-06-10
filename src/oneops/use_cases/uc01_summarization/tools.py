"""UC-1 deterministic data tools (Component Spec).

`get_ticket_links`, `get_ticket_timeline`, `get_ticket_attachment_metadata`
each return a tenant-scoped read-only view of one sub-collection on a work
record. The big sibling, `summarize_entity`, lives in a separate module
because it crosses the LLM gateway and has a very different conformance
surface (cost, redaction, retry).

All three deterministic tools share the same shape:
  * outcome ∈ {"found", "not_found", "invalid_request"}
  * data view is either a list (links/timeline/attachments) or None
  * tenant_id is bound from `context`, never from `arguments`
  * private/internal items are redacted via the registry field policy and the
    caller role (Component Spec C12 + C13)
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from oneops.observability import get_logger
from oneops.use_cases._shared.field_policy import get_field_policy
from oneops.use_cases._shared.ticket_store import get_ticket_store

_log = get_logger("oneops.use_cases.uc01.tools")

# Detect when the user has EXPLICITLY requested a linked-record traversal
# (the labels-on-focus precedence guard must NOT fire in that case —
# the LLM's via_link is legitimate, not spurious). Matches "linked X /
# related X / affected X / parent X / its X / the X" where X is a
# record-type word.
import re as _uc01_re

_LINKED_RECORD_PHRASE_RE = _uc01_re.compile(
    # Two word orders:
    #   (a) "<relation> <record-type>" — "linked problem", "related change"
    #   (b) "<record-type> linked to" — "problem linked to INC0001001"
    r"\b(?:"
    r"(?:the\s+|its\s+|any\s+)?"
    r"(?:linked|related|affected|parent|child)\s+"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)s?"
    r"|"
    r"(?:problem|change|incident|request|ci|cmdb[\s_-]?ci|asset|kb|article|ticket|record)s?"
    r"\s+linked\s+to"
    r")\b",
    _uc01_re.IGNORECASE,
)


@dataclass(frozen=True)
class SubCollectionResult:
    """Structured output for the deterministic sub-collection tools."""

    outcome: str          # "found" | "not_found" | "invalid_request"
    ticket_id: str
    service_id: str
    kind: str             # "links" | "timeline" | "attachments"
    message: str
    items: list[dict[str, Any]] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "ticket_id": self.ticket_id,
            "service_id": self.service_id,
            "kind": self.kind,
            "message": self.message,
            "items": self.items,
        }


def _invalid(kind: str, ticket_id: str, service_id: str, message: str) -> dict[str, Any]:
    return SubCollectionResult(
        outcome="invalid_request", ticket_id=ticket_id, service_id=service_id,
        kind=kind, message=message).to_dict()


def _not_found(kind: str, ticket_id: str, service_id: str) -> dict[str, Any]:
    return SubCollectionResult(
        outcome="not_found", ticket_id=ticket_id, service_id=service_id,
        kind=kind,
        message=f"No {service_id} with id {ticket_id} was found for this tenant.",
    ).to_dict()


def _found(
    kind: str, ticket_id: str, service_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return SubCollectionResult(
        outcome="found", ticket_id=ticket_id, service_id=service_id,
        kind=kind,
        message=f"Retrieved {len(items)} {kind} entr{'y' if len(items) == 1 else 'ies'} "
                f"for {service_id} {ticket_id}.",
        items=items,
    ).to_dict()


async def _fetch_record(
    arguments: dict[str, Any], context: dict[str, Any], *, kind: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Shared validation + tenant-scoped fetch. Returns (record, sentinel) —
    the sentinel is a populated structured response when validation/not-found
    happens, else `{}`; `record` is the row dict on success, else `None`."""
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()

    if not ticket_id:
        return None, _invalid(
            kind, ticket_id, service_id,
            "A ticket id is required.")
    if not service_id:
        return None, _invalid(
            kind, ticket_id, service_id,
            "A service module (incident, request, problem, change) is required.")
    if not tenant_id:
        return None, _invalid(
            kind, ticket_id, service_id,
            "No tenant scope was supplied for this request.")

    record = await get_ticket_store().get(
        ticket_id=ticket_id, service_id=service_id, tenant_id=tenant_id)
    if record is None:
        _log.info("uc01.tools.not_found",
                  kind=kind, ticket_id=ticket_id, service_id=service_id)
        return None, _not_found(kind, ticket_id, service_id)
    return record, {}


def _coerce_items(value: Any) -> list[dict[str, Any]]:
    """A sub-collection field may be missing, a single dict, or a list. Coerce
    to a list-of-dicts; drop scalars and Nones rather than fabricate shape."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return []


# ── get_ticket_links ─────────────────────────────────────────────────────


async def get_ticket_links(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Linked records — related incidents, parent problem, linked changes,
    affected CIs. Read-only, deterministic."""
    record, sentinel = await _fetch_record(arguments, context, kind="links")
    if record is None:
        return sentinel
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()

    items = _coerce_items(record.get("links"))
    # The field policy classifies each sub-item field; expose only what passes.
    policy = get_field_policy()
    exposed = [policy.expose(item) for item in items]
    return _found("links", ticket_id, service_id, exposed)


# ── get_ticket_timeline ──────────────────────────────────────────────────


async def get_ticket_timeline(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Chronological timeline — work notes, customer comments, state
    transitions. Internal notes are gated by the caller's role (Component
    Spec C12). Read-only, deterministic."""
    record, sentinel = await _fetch_record(arguments, context, kind="timeline")
    if record is None:
        return sentinel
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()
    role = str(context.get("role") or "").strip()

    items = _coerce_items(record.get("timeline"))
    policy = get_field_policy()
    exposed = [policy.expose(item) for item in items]
    # `redact_internal_content` strips internal entries from list-shaped
    # collections by role — the same machinery that filters work_notes on
    # the parent record.
    visible_row = policy.redact_internal_content({"timeline": exposed}, role)
    return _found("timeline", ticket_id, service_id,
                  list(visible_row.get("timeline") or []))


# ── get_ticket_attachment_metadata ───────────────────────────────────────


async def get_ticket_attachment_metadata(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Attachment metadata — name, id, size, content_type. Binary content is
    NEVER returned (Component Spec C13 — data-classification respected by the
    field policy). Read-only, deterministic."""
    record, sentinel = await _fetch_record(
        arguments, context, kind="attachments")
    if record is None:
        return sentinel
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()

    items = _coerce_items(record.get("attachments"))
    policy = get_field_policy()
    # Field policy drops classified-too-high fields (e.g. raw bytes). The
    # handler holds no per-field allowlist of its own (no static catalogs).
    exposed = [policy.expose(item) for item in items]
    return _found("attachments", ticket_id, service_id, exposed)


# ── summarize_entity ─────────────────────────────────────────────────────


# Lazy / injectable LLM entry point so tests can supply a fake without
# importing the live gateway. The default factory calls into the gateway
# module's singleton accessor if/when it is wired; tests inject a stub via
# `set_summarize_llm`.
SummarizeFn = Callable[..., Awaitable[dict[str, Any]]]
# Concretely: async fn(record, tenant_id, model, user_id="") -> dict.
# `user_id` is keyword-only with a default; older callers / tests that
# omit it still work, and the LLM span gets `oneops.user_id` filled when
# the handler threads context.user_id through.
"""(record, tenant_id, model) -> structured summary dict.

The function MUST return a dict with at minimum:
  {"summary": str, "key_points": list[str], "model": str, "usage": dict}
It MAY call the LLM gateway, or return a deterministic synthesis for tests.
"""

_summarize_fn: SummarizeFn | None = None


def set_summarize_llm(fn: SummarizeFn | None) -> None:
    """Inject (or clear) the LLM-backed summariser. Tests use this; the
    runtime wiring sets it during application startup."""
    global _summarize_fn
    _summarize_fn = fn


def _get_summarize_fn() -> SummarizeFn | None:
    return _summarize_fn


_RESERVED_SUMMARY_KEYS = frozenset({
    "outcome", "ticket_id", "service_id", "message", "summary",
    "cache_hit", "cache_age_s",
})

# Search/embedding substrate columns that travel ON the record but are NOT
# business fields any downstream step binds to — the FTS vector, per-chunk
# content hashes, and the (string-serialised) embedding array + its provenance.
# They pass the scalar filter below as strings, so they must be name-excluded;
# otherwise every summary's bindable surface carries a multi-thousand-element
# embedding blob for no consumer. Same intent as field_labels._HIDDEN, but kept
# separate because bindable INTENTIONALLY keeps title/description (chainable)
# which _HIDDEN drops from the user-facing grid.
_BINDABLE_NOISE_KEYS = frozenset({
    "search_tsv", "content_tsv",
    "content_hash", "content_hash_symptom", "content_hash_diagnosis",
    "content_hash_kb",
    "embedding", "embedding_model", "embedding_version", "embedded_at",
})


def _record_bindable_fields(record: dict[str, Any] | None) -> dict[str, Any]:
    """The record's OWN scalar fields, surfaced as the summary's bindable output
    so a downstream step can consume any of them BY NAME (data-flow binding).

    Fully dynamic — NO hardcoded field catalog: it reflects whatever fields the
    (already RBAC/classification-filtered) record carries right now. Add, rename,
    or delete a field in the data/schema and the bindable surface follows
    automatically. A binding to a field that no longer exists simply omits at
    runtime (planner bindings are optional), so field churn never breaks a turn.

    Search/embedding substrate columns (`_BINDABLE_NOISE_KEYS`) are excluded —
    they are not chainable business fields and would otherwise bloat every
    response with the embedding array.
    """
    out: dict[str, Any] = {}
    for k, v in (record or {}).items():
        if k in _RESERVED_SUMMARY_KEYS or k in _BINDABLE_NOISE_KEYS:
            continue
        if k.startswith("_"):                       # internal bookkeeping (e.g. _updated_at)
            continue
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            out[k] = v
    return out


@dataclass(frozen=True)
class SummarizeResult:
    outcome: str          # "summarized" | "not_found" | "invalid_request" | "llm_unavailable"
    ticket_id: str
    service_id: str
    message: str
    summary: dict[str, Any] | None = None
    # The record's own scalar fields (dynamic), spread at the top level of the
    # output so a downstream step's data-flow binding can resolve any of them by
    # name. Empty unless populated from the source record.
    bindable_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # `bindable` is a single, namespaced container for the record's dynamic
        # fields — kept OUT of the top level so the summary's output contract is
        # unchanged for existing consumers (they read outcome/summary/message).
        # The data-flow resolver looks here for a downstream binding's field.
        return {
            "outcome": self.outcome,
            "ticket_id": self.ticket_id,
            "service_id": self.service_id,
            "message": self.message,
            "summary": self.summary,
            "bindable": dict(self.bindable_fields),
        }


async def _resolve_field_intent(
    user_message: str, humanised: dict[str, Any], *,
    tenant_id: str, user_id: str, model: str,
):
    """Decide which fields THIS turn asks for. First-line is the embedding
    field matcher (pure semantic, no synonym list, Stage 3 2026-05-29) — skipped
    for linked-record / whole-record asks which still need the LLM's via_link
    judgment. Falls through to the keyword+LLM extractor on no embed match /
    fail-OPEN. Returns a FieldReadIntent."""
    from oneops.use_cases.uc01_summarization.field_embedder import (
        get_field_embedder,
    )
    from oneops.use_cases.uc01_summarization.field_read import (
        _LINKED_RECORD_BAIL,
        _WHOLE_RECORD_BAIL,
        FieldReadIntent,
        extract_requested_fields,
    )
    embedder = get_field_embedder()
    embed_labels: list[str] | None = None
    if (embedder is not None
            and not _LINKED_RECORD_BAIL.search(user_message)
            and not _WHOLE_RECORD_BAIL.search(user_message)):
        try:
            embed_labels = await embedder(
                user_message, list(humanised.keys()),
                tenant_id=tenant_id, user_id=user_id)
        except Exception as exc:                  # noqa: BLE001
            _log.warning("uc01.field_embedder.error", error=str(exc)[:200])
            embed_labels = None
    if embed_labels:
        # Confident embedding match — synthesise a single-hop intent.
        _log.info("uc01.field_read.extraction_path",
                  path="embedding", user_message=user_message[:100],
                  labels=embed_labels)
        return FieldReadIntent(labels=tuple(embed_labels),
                               via_link="", via_link_known=False)
    return await extract_requested_fields(
        user_message, list(humanised.keys()),
        tenant_id=tenant_id, user_id=user_id, model=model)


async def _serve_via_link(
    intent: Any, humanised: dict[str, Any], *,
    ticket_id: str, service_id: str, tenant_id: str, user_id: str,
    role: str, model: str, user_message: str,
) -> dict[str, Any]:
    """Two-hop "X of the linked Y". Three sub-cases: (c) unknown link on the
    focus → surface the mismatch; (a) known link + reachable → traverse; (b)
    known link unresolved (empty / USR id / RBAC / cross-tenant) → surface the
    link value verbatim. Never silently falls through to a summary."""
    if not intent.via_link_known:
        msg = (
            f"{ticket_id} has no \"{intent.via_link}\" field. "
            f"This {service_id} record doesn't expose that "
            f"linked-record type."
        )
        _log.info("uc01.field_read.linked_unknown_on_focus",
                  ticket_id=ticket_id, via_link=intent.via_link,
                  service_id=service_id)
        return SummarizeResult(
            outcome="field_read", ticket_id=ticket_id,
            service_id=service_id, message=msg, summary={"summary": msg},
        ).to_dict()

    linked_outcome = await _resolve_linked_field_read(
        focus_humanised=humanised, via_link=intent.via_link,
        tenant_id=tenant_id, user_id=user_id, role=role, model=model,
        focus_ticket_id=ticket_id, user_message=user_message,
    )
    if linked_outcome is not None:
        return linked_outcome

    link_value = humanised.get(intent.via_link, "")
    link_value_str = (", ".join(link_value) if isinstance(link_value, list)
                      else str(link_value or ""))
    if link_value_str:
        msg = (f"{intent.via_link}: {link_value_str}. "
               f"Ask about that record directly to see its "
               f"{', '.join(intent.labels) if intent.labels else 'details'}.")
    else:
        msg = f"No {intent.via_link} on this {service_id}."
    _log.info("uc01.field_read.linked_unresolved",
              ticket_id=ticket_id, via_link=intent.via_link,
              target_labels=list(intent.labels))
    return SummarizeResult(
        outcome="field_read", ticket_id=ticket_id,
        service_id=service_id, message=msg, summary={"summary": msg},
    ).to_dict()


async def _serve_field_read(
    intent: Any, humanised: dict[str, Any], *,
    ticket_id: str, service_id: str, tenant_id: str, user_id: str,
    role: str, model: str, user_message: str,
) -> dict[str, Any] | None:
    """Render a field-read outcome for a resolved intent, or None to fall
    through to a full summary. Precedence: unavailable-field → single-hop
    (a label directly on the focus wins over a spurious 2-hop UNLESS the user
    explicitly named a link traversal) → two-hop via_link."""
    from oneops.use_cases.uc01_summarization.field_read import render_field_read

    if getattr(intent, "unavailable_field", "") and not intent.labels:
        requested = intent.unavailable_field.strip()
        msg = (
            f"{ticket_id} ({service_id}) doesn't have a "
            f"\"{requested}\" field. Available fields on this record: "
            f"{', '.join(sorted(humanised.keys()))}."
        )
        _log.info("uc01.field_read.field_unavailable",
                  ticket_id=ticket_id, service_id=service_id,
                  requested=requested)
        return SummarizeResult(
            outcome="field_read", ticket_id=ticket_id,
            service_id=service_id, message=msg, summary={"summary": msg},
        ).to_dict()

    user_requests_traversal = bool(_LINKED_RECORD_PHRASE_RE.search(user_message))
    labels_on_focus = [lbl for lbl in (intent.labels or []) if lbl in humanised]
    if labels_on_focus and not user_requests_traversal:
        text = render_field_read(humanised, labels_on_focus, service_id)
        _log.info("uc01.field_read.served",
                  ticket_id=ticket_id, service_id=service_id,
                  labels=labels_on_focus,
                  via_link_overridden=bool(intent.via_link))
        return SummarizeResult(
            outcome="field_read", ticket_id=ticket_id,
            service_id=service_id, message=text, summary={"summary": text},
        ).to_dict()
    if intent.labels and not intent.via_link:
        text = render_field_read(humanised, list(intent.labels), service_id)
        _log.info("uc01.field_read.served",
                  ticket_id=ticket_id, service_id=service_id,
                  labels=list(intent.labels))
        return SummarizeResult(
            outcome="field_read", ticket_id=ticket_id,
            service_id=service_id, message=text, summary={"summary": text},
        ).to_dict()

    if intent.via_link:
        return await _serve_via_link(
            intent, humanised, ticket_id=ticket_id, service_id=service_id,
            tenant_id=tenant_id, user_id=user_id, role=role, model=model,
            user_message=user_message)
    return None


def _validate_summarize_inputs(
    ticket_id: str, service_id: str, tenant_id: str,
) -> dict[str, Any] | None:
    """Required-field guards → an invalid_request outcome dict, or None to
    proceed (ticket id, service module, and tenant scope are all mandatory)."""
    if not ticket_id:
        return SummarizeResult(
            outcome="invalid_request", ticket_id=ticket_id, service_id=service_id,
            message="A ticket id is required to summarise a record.",
        ).to_dict()
    if not service_id:
        return SummarizeResult(
            outcome="invalid_request", ticket_id=ticket_id, service_id=service_id,
            message="A service module is required to summarise a record.",
        ).to_dict()
    if not tenant_id:
        return SummarizeResult(
            outcome="invalid_request", ticket_id=ticket_id, service_id=service_id,
            message="No tenant scope was supplied for this request.",
        ).to_dict()
    return None


def _build_summary_result(
    summary: Any, visible: dict[str, Any], ticket_id: str, service_id: str,
) -> dict[str, Any]:
    """Assemble the 'summarized' outcome. Surfaces the cache-aside signal
    (cache_hit / age) when the SummarizeFn returned one, and the record's own
    RBAC-filtered fields as the dynamic bindable surface (no hardcoded names)."""
    cache_meta = summary.pop("_cache", None) if isinstance(summary, dict) else None
    out = SummarizeResult(
        outcome="summarized", ticket_id=ticket_id, service_id=service_id,
        message=(
            f"Summarised {service_id} {ticket_id}"
            + (" (from cache)." if cache_meta and cache_meta.get("hit") else ".")
        ),
        summary=summary,
        bindable_fields=_record_bindable_fields(visible),
    ).to_dict()
    if cache_meta is not None:
        out["cache_hit"] = bool(cache_meta.get("hit"))
        out["cache_age_s"] = cache_meta.get("age_s")
    return out


async def summarize_entity(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Synthesise a structured summary of a work record via the LLM gateway.

    Spec conformance:
      * C8  — structured output: a typed `SummarizeResult`.
      * C11 — LLM gateway is the only egress; this handler never calls a
              provider directly. The injected `SummarizeFn` carries that
              discipline (or its tests).
      * C13 — tenant-scoped: tenant_id bound from `context`.
      * C17 — no silent failure: LLM unavailability is an explicit outcome,
              not an exception bubbling up to the runner.
    """
    ticket_id = str(arguments.get("ticket_id") or "").strip()
    service_id = str(arguments.get("service_id") or "").strip()
    tenant_id = str(context.get("tenant_id") or "").strip()
    role = str(context.get("role") or "").strip()
    model = str(arguments.get("model") or context.get("model") or "").strip()

    invalid = _validate_summarize_inputs(ticket_id, service_id, tenant_id)
    if invalid is not None:
        return invalid

    record = await get_ticket_store().get(
        ticket_id=ticket_id, service_id=service_id, tenant_id=tenant_id)
    if record is None:
        return SummarizeResult(
            outcome="not_found", ticket_id=ticket_id, service_id=service_id,
            message=f"No {service_id} with id {ticket_id} was found for this tenant.",
        ).to_dict()

    # Reduce to the role/policy-visible projection before any LLM ever sees it.
    policy = get_field_policy()
    exposed = policy.expose(record)
    visible = policy.redact_internal_content(exposed, role)

    # ── Field-read branch (with optional 2-hop traversal) ────────────────
    # If the caller passed the user's current chat message, ask the
    # field-extractor whether THIS turn is a targeted field-read against
    # the focused record. The extractor returns a FieldReadIntent with:
    #   * `labels`     — canonical labels to render (subset of focus's
    #                    humanised labels when no via_link is set;
    #                    target labels on the LINKED record otherwise).
    #   * `via_link`   — non-empty when the user wants a field of a
    #                    LINKED record reached through the focus
    #                    (e.g. "priority of the linked problem" → the
    #                    linked PBM's priority, not the INC's).
    # When via_link is set, the handler does a deterministic 2-hop:
    # fetch the focus's linked id, fetch the linked record, run field-
    # extraction on that record's labels, and render.
    user_message = str(arguments.get("user_message") or "").strip()
    if user_message:
        from oneops.use_cases._shared.field_labels import humanise_record
        humanised = humanise_record(visible)
        intent = await _resolve_field_intent(
            user_message, humanised,
            tenant_id=tenant_id, user_id=str(context.get("user_id") or ""),
            model=model)
        served = await _serve_field_read(
            intent, humanised, ticket_id=ticket_id, service_id=service_id,
            tenant_id=tenant_id, user_id=str(context.get("user_id") or ""),
            role=role, model=model, user_message=user_message)
        if served is not None:
            return served

    fn = _get_summarize_fn()
    if fn is None:
        # No LLM is wired (test paths without injection, or pre-init startup).
        # Explicit outcome — never a silent empty summary (Component Spec C17).
        _log.warning("uc01.summarize.llm_unavailable",
                     ticket_id=ticket_id, service_id=service_id)
        return SummarizeResult(
            outcome="llm_unavailable", ticket_id=ticket_id, service_id=service_id,
            message="The summariser is not wired to an LLM in this process.",
        ).to_dict()

    summary = await fn(visible, tenant_id, model,
                       user_id=str(context.get("user_id") or ""))
    return _build_summary_result(summary, visible, ticket_id, service_id)


def _first_link_token(raw_link_value: Any) -> str:
    """First candidate id from a link value: the first element of a list, or
    the part before the first comma of a string (multi-target traversal is a
    separate UI concern). Empty string when there's nothing to follow."""
    if isinstance(raw_link_value, list):
        return str(raw_link_value[0]).strip() if raw_link_value else ""
    return str(raw_link_value).split(",")[0].strip()


async def _summarize_linked_record(
    linked_visible: dict[str, Any], *, tenant_id: str, model: str,
    user_id: str, linked_id: str, linked_service: str,
) -> dict[str, Any]:
    """Full summary of a linked record (2-hop with no specific target labels).
    Falls back to a structural note when no LLM is wired."""
    fn = _get_summarize_fn()
    if fn is None:
        note = (f"The linked {linked_service} {linked_id} is on file. "
                f"Ask 'summarize {linked_id}' to see its details.")
        return SummarizeResult(
            outcome="field_read", ticket_id=linked_id,
            service_id=linked_service, message=note,
            summary={"summary": note},
        ).to_dict()
    linked_summary = await fn(linked_visible, tenant_id, model, user_id=user_id)
    cache_meta = (linked_summary.pop("_cache", None)
                  if isinstance(linked_summary, dict) else None)
    out = SummarizeResult(
        outcome="summarized", ticket_id=linked_id, service_id=linked_service,
        message=(f"Summarised linked {linked_service} {linked_id}"
                 + (" (from cache)." if cache_meta and cache_meta.get("hit") else ".")),
        summary=linked_summary,
    ).to_dict()
    if cache_meta is not None:
        out["cache_hit"] = bool(cache_meta.get("hit"))
        out["cache_age_s"] = cache_meta.get("age_s")
    return out


async def _resolve_linked_field_read(
    *,
    focus_humanised: dict[str, Any],
    via_link: str,
    tenant_id: str,
    user_id: str,
    role: str,
    model: str,
    focus_ticket_id: str,
    user_message: str,
) -> dict[str, Any] | None:
    """Two-hop traversal for "X of the linked Y" / "owner of the related
    problem" / similar.

    Steps:
      1. Read the link value from the focus's humanised record (e.g.
         "Related Problem" → "PBM0003002"). If the link value is a list
         (Related Incidents, Affected CIs, Approved By), only the first
         id is followed — UI clarification for multi-target traversal
         is out of scope here.
      2. Normalise the id to its service via `EntityIdNormalizer` (the
         same registry the router uses). USR/GRP-style ids are not
         work-record entities and short-circuit gracefully.
      3. Tenant-scoped fetch of the linked record. Tenant isolation +
         role-aware field policy stay intact on the linked side.
      4. Run field extraction against the LINKED record's humanised
         labels with the user's original message. Cross-type synonyms
         (priority on a change → Risk Level) are already handled by
         the extractor prompt.
      5. Render and return a `field_read` outcome anchored on the
         linked record (so the user sees which entity answered).

    Returns the structured outcome dict, or None to let the caller
    handle the unresolvable case.
    """
    from oneops.router.entity_id import EntityIdNormalizer
    from oneops.use_cases._shared.field_labels import humanise_record
    from oneops.use_cases._shared.field_policy import get_field_policy
    from oneops.use_cases.uc01_summarization.field_read import (
        extract_requested_fields,
        render_field_read,
    )

    raw_link_value = focus_humanised.get(via_link)
    if not raw_link_value:
        return None
    # Take the first id when the link is a list-of-ids (Affected CIs,
    # Related Incidents, Approved By, Linked CIs). Multi-target
    # traversal is a separate UI concern.
    candidate_token = _first_link_token(raw_link_value)
    if not candidate_token:
        return None

    normalizer = EntityIdNormalizer.from_registry_file()
    norm = normalizer.normalize(candidate_token)
    if norm.entity is None:
        # User-style ids (USR00006) and similar aren't work records.
        _log.info("uc01.field_read.link_not_a_record",
                  via_link=via_link, value=candidate_token,
                  reason=norm.reason)
        return None
    linked_id = norm.entity.entity_id
    linked_service = norm.entity.service_id

    linked_record = await get_ticket_store().get(
        ticket_id=linked_id, service_id=linked_service,
        tenant_id=tenant_id)
    if linked_record is None:
        # Cross-tenant link or stale id — handler returns None and the
        # caller surfaces the link value verbatim.
        _log.info("uc01.field_read.linked_not_found",
                  via_link=via_link, linked_id=linked_id,
                  linked_service=linked_service)
        return None

    policy = get_field_policy()
    linked_exposed = policy.expose(linked_record)
    linked_visible = policy.redact_internal_content(linked_exposed, role)
    linked_humanised = humanise_record(linked_visible)

    # Second extraction call against the LINKED record's labels. The
    # same user_message goes through; the extractor maps target labels
    # against this new label set (Priority on a change → Risk Level).
    second = await extract_requested_fields(
        user_message, list(linked_humanised.keys()),
        tenant_id=tenant_id, user_id=user_id, model=model)
    # If the user asked for a full summary of the linked entity
    # ("tell me about the linked change") `second.labels` may be empty
    # but via_link won't recur — fall back to the full summary path.
    if not second.labels:
        # Full summary of the linked record (the 2-hop named no specific
        # target labels — "tell me about the linked change").
        return await _summarize_linked_record(
            linked_visible, tenant_id=tenant_id, model=model, user_id=user_id,
            linked_id=linked_id, linked_service=linked_service)

    body = render_field_read(linked_humanised, list(second.labels), linked_service)
    # Prefix with the linked record id so the user sees WHICH linked
    # record answered. Without this, a query like "status of the linked
    # problem" returns "Status: root_cause_identified" with no indication
    # of which PBM produced the value — UX gap that hides the hop.
    text = f"{linked_id} — {body}" if body else body
    _log.info("uc01.field_read.linked_served",
              focus_ticket_id=focus_ticket_id, via_link=via_link,
              linked_id=linked_id, linked_service=linked_service,
              labels=list(second.labels))
    return SummarizeResult(
        outcome="field_read", ticket_id=linked_id,
        service_id=linked_service, message=text,
        summary={"summary": text},
    ).to_dict()


__all__ = [
    "SubCollectionResult",
    "SummarizeResult",
    "SummarizeFn",
    "set_summarize_llm",
    "get_ticket_links",
    "get_ticket_timeline",
    "get_ticket_attachment_metadata",
    "summarize_entity",
]
