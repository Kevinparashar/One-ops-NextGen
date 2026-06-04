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

from typing import Any

from oneops.uc_common import TimeFilter
from oneops.use_cases.uc02_similar_tickets.contracts import (
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


async def find_similar_entities(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Registry-resolved tool handler. Returns plain JSON-serializable dict.

    Auto-derives service_id from the ticket_id prefix (INC… → incident,
    REQ… → request) when the chat path doesn't pass it explicitly — mirrors
    UC-1's `summarize_entity` fast-path contract. The id_resolver is the
    single source of truth for canonicalisation across button + chat.
    """
    tenant_id = str(context.get("tenant_id") or arguments.get("tenant_id") or "").strip()
    user_id = str(context.get("user_id") or arguments.get("user_id") or "").strip()
    role = str(context.get("role") or arguments.get("role") or "").strip()

    # Multi-turn focus binding (LangGraph state channel — see [[project_poc5mw1_focus_state_channel_2026_05_28]]).
    # Follow-up turns like "are there any similar?" carry no ticket in the
    # arguments; the executor sets focus_entity_id / focus_service_id on the
    # tool context after the previous turn. We read both as fallbacks.
    ticket_id_raw = str(
        arguments.get("ticket_id")
        or context.get("focus_entity_id")
        or ""
    ).strip()
    service_id = str(
        arguments.get("service_id")
        or context.get("focus_service_id")
        or ""
    ).strip().lower() or None

    if not tenant_id:
        raise ValueError("tenant_id missing from context and arguments")
    if not ticket_id_raw:
        raise ValueError(
            "ticket_id is required — provide it explicitly (e.g. "
            "'similar to INC0001234') or first set focus by summarising a "
            "ticket")

    # Canonicalise + (when missing) auto-derive service_id from the prefix.
    try:
        resolved = _resolve_id(ticket_id_raw, service_id)
    except ResolveError as exc:
        msg = str(exc)
        # Out-of-scope service (problem / change / KB / etc.) — the user's
        # focus is on a record type UC-2 doesn't cover. Return a friendly
        # explanatory message instead of raising; the composer surfaces it
        # via the `message` field so the chat shows a useful sentence
        # rather than "I wasn't able to complete that request." This is
        # the §UC-2 boundary case: tell the user what we can do, not what
        # we can't.
        if ("UC-2 supports" in msg
                or "service_id must be one of" in msg):
            return {
                "display_text": (
                    "I can only find similar tickets for incidents and "
                    "requests. The current focus is a different record "
                    "type — try asking about an incident (INC…) or request "
                    "(REQ…) instead."
                ),
                "message": (
                    "I can only find similar tickets for incidents and "
                    "requests. The current focus is a different record "
                    "type — try asking about an incident (INC…) or request "
                    "(REQ…) instead."
                ),
                "results": [],
            }
        # Re-raise other resolver errors as ValueError so the tool-runner
        # emits a clean tool-error envelope back to the composer.
        raise ValueError(msg) from exc

    ticket_id = resolved.entity_id
    service_id = resolved.service_id

    # Connection provider: prefer one injected via context (lets tests stub
    # the DB), fall back to a process-default asyncpg over POSTGRES_URL.
    # The fallback is what the chat path uses — it parallels UC-5's runner.
    cp = context.get("uc02_connection_provider") or _default_conn_provider

    # TimeFilter — populated by the executor's conditional extractor step
    # for chat turns, or by the route directly for button calls. Accept dict
    # (from JSON) or already-validated TimeFilter; reject anything else.
    raw_tf = context.get("time_filter") or arguments.get("time_filter")
    time_filter: TimeFilter | None = None
    if isinstance(raw_tf, TimeFilter):
        time_filter = raw_tf
    elif isinstance(raw_tf, dict):
        try:
            time_filter = TimeFilter(**raw_tf)
        except Exception:                                          # noqa: BLE001
            # Loud is rule §2.7, but a malformed scope shouldn't kill the
            # whole query — degrade to no filter and the operator sees the
            # span attribute time_filter.outcome=invalid for diagnosis.
            time_filter = None

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
        # `find_similar` raises RuntimeError for: not-found, anchor-pending,
        # or DB unreachable. The chat composer needs a clean text response —
        # we translate into a user-readable string so multi-turn keeps flowing.
        msg = str(exc).lower()
        if "not found" in msg:
            return {
                "display_text": f"Ticket {ticket_id} not found in tenant {tenant_id}.",
                "results": [],
                "source_ticket_id": ticket_id,
                "service_id": service_id,
                "tenant_id": tenant_id,
            }
        if "anchor" in msg or "refresh" in msg:
            return {
                "display_text": (
                    f"Embedding for {ticket_id} is still being computed — "
                    f"please try again in a moment."
                ),
                "results": [],
                "source_ticket_id": ticket_id,
                "service_id": service_id,
                "tenant_id": tenant_id,
            }
        raise

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
