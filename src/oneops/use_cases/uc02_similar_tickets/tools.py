"""Tool entrypoint for UC-2 — registry shim.

`tool-registry.json` lists `find_similar_entities` with this module path so
the ToolRunner can resolve it. The shim does three things:

  1. Validate `arguments` + `context` at the tool boundary (strict typing).
  2. Acquire a DB connection from the runtime via `context["uc02_connection_provider"]`
     so tests and the API factory inject their own pool.
  3. Delegate to `core.find_similar()` — no business logic lives here.

Same shim is invoked from chat (via the handler resolver) and from the
button route (via NATS dispatch → agent → tool). Identical results are
guaranteed because every path lands here.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from oneops.uc_common import TimeFilter
from oneops.use_cases.uc02_similar_tickets.contracts import (
    ServiceId,
    SimilarTicketsResponse,
)
from oneops.use_cases.uc02_similar_tickets.core import find_similar
from oneops.use_cases.uc02_similar_tickets.id_resolver import (
    ResolveError,
)
from oneops.use_cases.uc02_similar_tickets.id_resolver import (
    resolve as _resolve_id,
)
from oneops.use_cases.uc02_similar_tickets.render import render as _render

# ── Optional discriminator-LLM injection ─────────────────────────────────
# Wired at boot from `oneops.api.app:lifespan` (alongside the UC-1 LLM
# wiring). When unset, `find_similar` simply skips the discriminator pass
# and returns rows with `discriminator=None`. Production-safe by default.
_discriminator_gateway: Any = None
_discriminator_model: str | None = None


def set_discriminator_llm(gateway: Any, model: str | None) -> None:
    """Wire the LLM used to generate per-result discriminator labels.

    Single-egress (rule §2.5): `gateway` MUST be an instance of
    `oneops.llm.gateway.LlmGateway`. `model` may be None to disable.
    """
    global _discriminator_gateway, _discriminator_model
    _discriminator_gateway = gateway
    _discriminator_model = model


async def _default_conn_provider():
    """Per-call asyncpg connection over POSTGRES_URL. Matches UC-5's pattern.

    Each call opens + closes its own connection — cheap (~5ms) and avoids
    any shared-pool surprises. Production with high QPS can wire a pool-
    backed provider via context to amortise the overhead.
    """
    import os

    import asyncpg
    pg_url = os.getenv("POSTGRES_URL")
    if not pg_url:
        raise RuntimeError("POSTGRES_URL not set; UC-2 cannot reach Postgres")
    return await asyncpg.connect(pg_url)


_UC2_BOUNDARY_MSG = (
    "I can only find similar tickets for incidents and requests. The current "
    "focus is a different record type — try asking about an incident (INC…) "
    "or request (REQ…) instead."
)


def _resolve_boundary_response(exc: ResolveError) -> dict[str, Any]:
    """Map a ResolveError to the friendly UC-2 boundary payload when the focus
    is a record type UC-2 doesn't cover (problem / change / KB / …) — telling
    the user what we CAN do, not what we can't. Any other resolver error is
    re-raised as ValueError so the tool-runner emits a clean error envelope."""
    msg = str(exc)
    if "UC-2 supports" in msg or "service_id must be one of" in msg:
        return {"display_text": _UC2_BOUNDARY_MSG,
                "message": _UC2_BOUNDARY_MSG, "results": []}
    raise ValueError(msg) from exc


def _coerce_time_filter(raw_tf: Any) -> TimeFilter | None:
    """Accept an already-validated TimeFilter or a dict (from JSON); anything
    else (or a malformed dict) degrades to None — a bad scope must not kill the
    query (§2.7: the operator sees time_filter.outcome=invalid on the span)."""
    if isinstance(raw_tf, TimeFilter):
        return raw_tf
    if isinstance(raw_tf, dict):
        try:
            return TimeFilter(**raw_tf)
        except Exception:                                          # noqa: BLE001
            return None
    return None


def _find_similar_error_response(
    exc: RuntimeError, *, ticket_id: str, tenant_id: str, service_id: Any,
) -> dict[str, Any]:
    """Translate find_similar's RuntimeError (not-found / anchor-pending) into a
    clean chat payload so multi-turn keeps flowing; re-raise anything else."""
    msg = str(exc).lower()
    base = {"results": [], "source_ticket_id": ticket_id,
            "service_id": service_id, "tenant_id": tenant_id}
    if "not found" in msg:
        return {"display_text":
                f"Ticket {ticket_id} not found in tenant {tenant_id}.", **base}
    if "anchor" in msg or "refresh" in msg:
        return {"display_text": (
            f"Embedding for {ticket_id} is still being computed — "
            f"please try again in a moment."), **base}
    raise exc


_TEXT_SERVICES: tuple[ServiceId, ...] = ("incident", "request")


def _text_min_score() -> float:
    """Composite floor for the same-by-text path. Lower than the id path's 0.5
    because a free-text query carries NO source metadata (same_ci / same_group
    / same_category all absent), so the composite is effectively semantic-only
    (~0.6·cosine) — the 0.5 default would reject all but near-identical wording.
    Read PER-CALL so .env tuning takes effect (parity with UC-3/UC-8)."""
    try:
        return float(os.environ.get("UC02_TEXT_MIN_SCORE", "0.20"))
    except ValueError:
        return 0.20


def _text_query_from(arguments: dict[str, Any]) -> str:
    """The free-text symptom description for the same-by-text path. The router's
    `_chat_bind` threads the (rewritten) sub-query as `query`/`user_message`;
    a button/REST caller may pass `query_text` explicitly."""
    return str(
        arguments.get("query_text")
        or arguments.get("query")
        or arguments.get("user_message")
        or "",
    ).strip()


def _services_for_text(service_id: str | None) -> tuple[ServiceId, ...]:
    """Which services a text query searches. A service-qualified query (explicit
    service_id, or a prior-turn focus that set focus_service_id) searches just
    that one; a service-agnostic NL query searches BOTH and merges — guessing a
    single service would silently hide half the corpus (§2.7)."""
    if service_id in _TEXT_SERVICES:
        return (service_id,)  # type: ignore[return-value]
    return _TEXT_SERVICES


def _merge_text_responses(
    responses: list[SimilarTicketsResponse], *, tenant_id: str,
    max_results: int, time_filter: TimeFilter | None,
) -> SimilarTicketsResponse:
    """Fuse per-service text responses into one ranked list. Each `SimilarTicket`
    carries its own `service_id`, so a mixed incident/request list renders
    correctly; we re-sort by the composite score and cap at `max_results`."""
    merged = sorted(
        (r for resp in responses for r in resp.results),
        key=lambda r: r.similarity_score, reverse=True,
    )[:max_results]
    top_service: ServiceId = merged[0].service_id if merged else "incident"
    message = None if merged else "no significantly similar tickets found"
    warning = next((resp.warning for resp in responses if resp.warning), None)
    return SimilarTicketsResponse(
        source_ticket_id="",
        service_id=top_service,
        tenant_id=tenant_id,
        results=merged,
        total_candidates_considered=sum(
            resp.total_candidates_considered for resp in responses),
        message=message,
        warning=warning,
        cached=False,
        time_filter=(
            time_filter if time_filter is not None
            and not time_filter.is_empty() else None),
        source_ticket=None,
    )


async def _find_similar_by_text(
    *, arguments: dict[str, Any],
    tenant_id: str, user_id: str, role: str, service_id: str | None,
    time_filter: TimeFilter | None, cp: Any,
) -> dict[str, Any]:
    """Same-by-text entry: embed the symptom description and search the
    applicable service(s). Returns the same payload shape as the id path."""
    query_text = _text_query_from(arguments)
    if not query_text:
        # No ticket AND no describable text — same contract as the id path.
        raise ValueError(
            "provide a ticket id (e.g. 'similar to INC0001234') or describe "
            "the problem to find similar tickets")

    max_results = int(arguments.get("max_results") or 5)
    explicit_min = arguments.get("min_similarity_score")
    min_score = (float(explicit_min) if explicit_min is not None
                 else _text_min_score())
    prefer_status = str(arguments.get("prefer_status") or "any")
    services = _services_for_text(service_id)

    # The per-service searches are independent — run them CONCURRENTLY. Each
    # opens its own connection and does an embed + ANN + discriminator LLM
    # batch; sequential fan-out across two services blew the 15s tool timeout
    # on a cold cache. Concurrency makes wall-clock ≈ the slowest single service.
    async def _search(svc: ServiceId) -> SimilarTicketsResponse:
        return await find_similar(
            tenant_id=tenant_id,
            service_id=svc,
            user_id=user_id or "anonymous",
            role=role or "service_desk_agent",
            max_results=max_results,
            time_filter=time_filter,
            prefer_status=prefer_status,  # type: ignore[arg-type]
            min_similarity_score=min_score,
            diagnosis_confirm=False,  # no source diagnosis_trail in text mode
            connection_provider=cp,
            discriminator_gateway=_discriminator_gateway,
            discriminator_model=_discriminator_model,
            query_text=query_text,
            embedding_gateway=_discriminator_gateway,
        )

    responses: list[SimilarTicketsResponse] = list(
        await asyncio.gather(*(_search(svc) for svc in services)))

    resp = _merge_text_responses(
        responses, tenant_id=tenant_id, max_results=max_results,
        time_filter=time_filter)
    payload = resp.model_dump(mode="json")
    payload["display_text"] = _render(
        resp, time_filter_label=(time_filter.label if time_filter else None))
    return payload


async def find_similar_entities(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Registry-resolved tool handler. Returns plain JSON-serializable dict.

    Two entry shapes land here, both producing the same payload:
      • id path  — a ticket id (arg or prior-turn focus) → similar to THAT
        ticket's stored symptom_anchor.
      • text path — no ticket, a free-text symptom description (`query` /
        `user_message` / `query_text`) → embed the text and search the
        applicable service(s). See `_find_similar_by_text`.

    Auto-derives service_id from the ticket_id prefix (INC… → incident,
    REQ… → request) when the chat path doesn't pass it explicitly — mirrors
    UC-1's `summarize_entity` fast-path contract. The id_resolver is the
    single source of truth for canonicalisation across button + chat.
    """
    tenant_id = str(context.get("tenant_id") or arguments.get("tenant_id") or "").strip()
    user_id = str(context.get("user_id") or arguments.get("user_id") or "").strip()
    role = str(context.get("role") or arguments.get("role") or "").strip()
    explicit_ticket = str(arguments.get("ticket_id") or "").strip()
    focus_ticket = str(context.get("focus_entity_id") or "").strip()
    explicit_service = str(arguments.get("service_id") or "").strip().lower() or None
    focus_service = str(context.get("focus_service_id") or "").strip().lower() or None
    text_query = _text_query_from(arguments)
    if not tenant_id:
        raise ValueError("tenant_id missing from context and arguments")

    # Connection provider: prefer one injected via context (lets tests stub
    # the DB), fall back to a process-default asyncpg over POSTGRES_URL.
    # The fallback is what the chat path uses — it parallels UC-5's runner.
    cp = context.get("uc02_connection_provider") or _default_conn_provider

    # TimeFilter — populated by the executor's conditional extractor step
    # for chat turns, or by the route directly for button calls. Accept dict
    # (from JSON) or already-validated TimeFilter; reject anything else.
    time_filter = _coerce_time_filter(
        context.get("time_filter") or arguments.get("time_filter"))

    # ── source resolution: stale-focus guard (multi-turn) ──────────────────
    # `focus_entity_id` is conversational state carried over from a PRIOR turn.
    # It must SCOPE a referential follow-up ("any similar ones?"), never HIJACK a
    # turn whose message introduces its OWN symptom ("...other tickets for
    # database issues"). Precedence:
    #   1. an explicit ticket id named in THIS message → that ticket (id path).
    #   2. a carried focus + a free-text symptom in THIS message → try the
    #      message's own topic first (text path, NOT scoped to the focus's
    #      service); only when it is contentless (a bare reference that matches
    #      nothing) fall back to the focused ticket. The corpus is the judge — no
    #      keyword test of "is this a topic".
    #   3. a carried focus alone (no symptom text) → the focused ticket (id path).
    #   4. symptom text alone (or nothing) → text path / friendly error.
    if explicit_ticket:
        ticket_id_raw = explicit_ticket
        service_id = explicit_service or focus_service
    elif focus_ticket and text_query:
        text_payload = await _find_similar_by_text(
            arguments=arguments, tenant_id=tenant_id, user_id=user_id, role=role,
            service_id=explicit_service,  # new topic → search all, not focus's svc
            time_filter=time_filter, cp=cp)
        if text_payload.get("results"):
            return text_payload                       # the message's own topic won
        ticket_id_raw = focus_ticket                  # bare reference → focused ticket
        service_id = focus_service
    elif focus_ticket:
        ticket_id_raw = focus_ticket
        service_id = focus_service
    else:
        return await _find_similar_by_text(
            arguments=arguments, tenant_id=tenant_id, user_id=user_id, role=role,
            service_id=explicit_service, time_filter=time_filter, cp=cp)

    # Canonicalise + (when missing) auto-derive service_id from the prefix.
    try:
        resolved = _resolve_id(ticket_id_raw, service_id)
    except ResolveError as exc:
        return _resolve_boundary_response(exc)

    ticket_id = resolved.entity_id
    service_id = resolved.service_id

    try:
        resp: SimilarTicketsResponse = await find_similar(
            tenant_id=tenant_id,
            service_id=service_id,  # type: ignore[arg-type]
            ticket_id=ticket_id,
            user_id=user_id or "anonymous",
            role=role or "service_desk_agent",
            max_results=int(arguments.get("max_results") or 5),
            time_filter=time_filter,
            same_category_only=bool(arguments.get("same_category_only") or False),
            same_service_only=bool(arguments.get("same_service_only") or False),
            prefer_status=str(arguments.get("prefer_status") or "any"),  # type: ignore[arg-type]
            min_similarity_score=float(arguments.get("min_similarity_score") if arguments.get("min_similarity_score") is not None else 0.5),
            diagnosis_confirm=bool(arguments.get("diagnosis_confirm", True)),
            connection_provider=cp,
            discriminator_gateway=_discriminator_gateway,
            discriminator_model=_discriminator_model,
        )
    except RuntimeError as exc:
        return _find_similar_error_response(
            exc, ticket_id=ticket_id, tenant_id=tenant_id, service_id=service_id)

    # Structured response (for downstream LLM refinement, follow-ups) +
    # pre-rendered chat-ready text. The chat composer reads `display_text`
    # as a first-class output contract — see `oneops.executor.nodes._compose
    # _step_text`. This keeps the structured `message` field free to carry
    # its semantic meaning ("no significantly similar tickets found" on the
    # empty path) without conflating it with rendered output.
    payload = resp.model_dump(mode="json")
    payload["display_text"] = _render(
        resp,
        time_filter_label=(time_filter.label if time_filter else None),
    )
    return payload


__all__ = ["find_similar_entities"]
