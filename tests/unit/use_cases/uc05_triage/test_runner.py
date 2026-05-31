"""Phase 3 tests — production runner builder + checkpointer durability."""
from __future__ import annotations

from typing import Any

import pytest

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import (
    LlmRequest,
    ResponseFormat,
    TransportResult,
)
from oneops.use_cases.uc05_triage.runner import build_runner


class _Transport:
    """Returns canned JSON responses sized for each LLM call type."""
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, texts, *, model: str, dimensions: int | None):
        return [[0.0] * 1536 for _ in texts]

    async def complete(self, req: LlmRequest):
        self.calls.append(req.messages[0].content[:30])
        if req.response_format == ResponseFormat.JSON:
            # Could be tag (list) or prioritize (object). Try to detect.
            sys = req.messages[0].content
            if "IMPACT values" in sys:
                return TransportResult(
                    content='{"impact":"On Department","urgency":"High"}',
                    prompt_tokens=20, completion_tokens=8,
                    actual_model=req.model,
                )
            return TransportResult(
                content='["vpn","tunnel","gateway"]',
                prompt_tokens=20, completion_tokens=8,
                actual_model=req.model,
            )
        # tiebreak (plain text)
        return TransportResult(
            content="vpn",
            prompt_tokens=20, completion_tokens=3,
            actual_model=req.model,
        )


class _FakeAsyncpgConn:
    """A connection stub that returns canned rows for the FTS + vector queries."""
    async def fetch(self, query: str, *args: Any) -> list[dict]:
        # Return one row that looks like an incident neighbour
        return [{
            "id": "INC0001002",
            "title": "VPN drops after 10 minutes",
            "description": "tunnel keeps dropping",
            "category": "network",
            "subcategory": "vpn",
            "service_name": "Corporate VPN",
            "ci_id": "CI0000001",
            "assignment_group": "GRP-NETOPS",
            "assigned_to": "USR00003",
            "status": "open",
            "created_at": None,
            "fts_score": 1.0,
            "vec_score": 0.9,
        }]

    async def close(self) -> None:
        pass


async def _conn_provider():
    return _FakeAsyncpgConn()


class TestRunnerBuilder:
    @pytest.mark.asyncio
    async def test_runner_end_to_end(self) -> None:
        gw = LlmGateway(transport=_Transport(), redact=False)
        run = build_runner(gateway=gw, connection_provider=_conn_provider)
        proposal = await run(
            ticket_row={"incident_id": "INC0000001",
                        "title": "VPN keeps dropping at Mumbai DC",
                        "description": "tunnel drops every 10 minutes"},
            service_id="incident",
            tenant_id="T001",
        )
        assert proposal.ticket_id == "INC0000001"
        assert proposal.suggested_category == "network"
        assert proposal.suggested_impact == "On Department"
        assert proposal.suggested_urgency == "High"
        assert proposal.suggested_priority == "High"
        assert proposal.risk_class == "medium"
        assert proposal.mutation_intent == "recommend_only"

    @pytest.mark.asyncio
    async def test_runner_works_for_request_service(self) -> None:
        gw = LlmGateway(transport=_Transport(), redact=False)
        run = build_runner(gateway=gw, connection_provider=_conn_provider)
        proposal = await run(
            ticket_row={"request_id": "REQ0000001",
                        "title": "MacBook for ML eng",
                        "description": "new joiner data science"},
            service_id="request",
            tenant_id="T001",
        )
        assert proposal.ticket_id == "REQ0000001"
        assert proposal.service_id == "request"


class TestCheckpointerThreadIsolation:
    """Distinct tenant/ticket → distinct thread_id, so concurrent runs don't
    collide on the in-memory checkpointer."""

    @pytest.mark.asyncio
    async def test_two_tickets_two_proposals(self) -> None:
        gw = LlmGateway(transport=_Transport(), redact=False)
        run = build_runner(gateway=gw, connection_provider=_conn_provider)
        p1 = await run(
            ticket_row={"incident_id": "INC0000001",
                        "title": "VPN A", "description": "a"},
            service_id="incident", tenant_id="T001",
        )
        p2 = await run(
            ticket_row={"incident_id": "INC0000002",
                        "title": "VPN B", "description": "b"},
            service_id="incident", tenant_id="T001",
        )
        assert p1.proposal_id != p2.proposal_id
        assert p1.ticket_id == "INC0000001"
        assert p2.ticket_id == "INC0000002"
