"""UC-5 production tools runner (Phase 3).

Wires the LangGraph to the API. The API's `_tools_runner` calls this
and gets a Proposal back — replaces the Section J test stub.

Inputs:
  • gateway (LlmGateway) — single LLM/embed egress
  • db connection or pool — for retrieval engine (Tool 1)
  • store (TicketStore) — for the API; the runner doesn't use it
    (the row is passed in pre-fetched by the API)

The runner binds tenant + user + gateway + connection at call time,
constructs the three tool callables via the Phase 1 adapters, and
invokes the compiled graph.

Cost discipline: tag_fn + tiebreak_fn + infer_fn all run through the
gateway — per-tenant cost and llm.call spans are automatic.
"""
from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.observability import span
from oneops.use_cases.uc05_triage.adapters import (
    make_embed_fn,
    make_infer_fn,
    make_propose_fn,
    make_tag_fn,
    make_tiebreak_fn,
)
from oneops.use_cases.uc05_triage.contracts import Proposal
from oneops.use_cases.uc05_triage.graph import build_uc05_graph
from oneops.use_cases.uc05_triage.tools.check_duplicates import (
    check_duplicate_candidates,
)
from oneops.use_cases.uc05_triage.tools.prioritize import prioritize_entity
from oneops.use_cases.uc05_triage.tools.recommend_assignment import (
    recommend_assignment,
)

# Connection factory: callers pass an async fn that yields an asyncpg connection.
# This avoids a hard dep on asyncpg in unit tests.
ConnectionProvider = Callable[[], Awaitable[Any]]


def build_runner(
    *,
    gateway: LlmGateway,
    connection_provider: ConnectionProvider,
    user_id_default: str = "system",
):
    """Build a Section-J-compatible `_tools_runner` async function.

    The returned callable matches the signature:
        async fn(*, ticket_row, service_id, tenant_id) -> Proposal
    """

    async def _runner(*, ticket_row: dict[str, Any], service_id: str,
                       tenant_id: str) -> Proposal:
        with span("uc05.runner.invoke",
                  **{"oneops.tenant_id": tenant_id,
                     "uc05.service_id": service_id}):
            conn = await connection_provider()
            try:
                # Build the per-request adapter callables — tenant-scoped
                embed_fn = make_embed_fn(gateway, tenant_id=tenant_id,
                                          user_id=user_id_default)
                tiebreak_fn = make_tiebreak_fn(gateway, tenant_id=tenant_id,
                                                user_id=user_id_default)
                propose_fn = make_propose_fn(gateway, tenant_id=tenant_id,
                                              user_id=user_id_default)
                tag_fn = make_tag_fn(gateway, tenant_id=tenant_id,
                                      user_id=user_id_default)
                infer_fn = make_infer_fn(gateway, tenant_id=tenant_id,
                                          user_id=user_id_default)

                # Build the per-request tool wrappers — bind conn/adapters
                async def _check(*, ticket_row, service_id, tenant_id):
                    return await check_duplicate_candidates(
                        service_id=service_id, tenant_id=tenant_id,
                        ticket_row=ticket_row, embed_fn=embed_fn, conn=conn,
                        tiebreak_fn=tiebreak_fn, tag_fn=tag_fn,
                        propose_fn=propose_fn,
                    )

                async def _assign(*, candidates, probe_text, ticket_row):
                    return await recommend_assignment(
                        candidates=candidates, probe_text=probe_text,
                        ticket_row=ticket_row, tiebreak_fn=tiebreak_fn,
                    )

                async def _prio(*, service_id, ticket_row,
                                suggested_category, suggested_subcategory):
                    return await prioritize_entity(
                        service_id=service_id, ticket_row=ticket_row,
                        suggested_category=suggested_category,
                        suggested_subcategory=suggested_subcategory,
                        infer_fn=infer_fn,
                    )

                graph = build_uc05_graph(
                    check_duplicates=_check,
                    recommend_assignment=_assign,
                    prioritize=_prio,
                )
                ticket_id_str = str(ticket_row.get(f"{service_id}_id") or "")
                final = await graph.ainvoke(
                    {
                        "tenant_id": tenant_id,
                        "service_id": service_id,
                        "ticket_id": ticket_id_str,
                        "ticket_row": dict(ticket_row),
                    },
                    config={"configurable": {
                        # Stable thread id so a follow-up resume from the
                        # same proposal can find the checkpoint.
                        "thread_id": f"{tenant_id}:{service_id}:{ticket_id_str}",
                    }},
                )
                return final["proposal"]
            finally:
                if hasattr(conn, "close"):
                    with contextlib.suppress(Exception):
                        await conn.close()

    return _runner


__all__ = ["build_runner", "ConnectionProvider"]
