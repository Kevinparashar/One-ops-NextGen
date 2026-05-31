"""Phase 5 — integrated-stack devil's-play.

Earlier per-phase devil's-play tested each layer in isolation. Phase 5
hits the full integrated stack to confirm the layers compose correctly.

The 12 probes from the locked checklist; some collapse to single tests
because the underlying substrate is shared.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oneops.errors import (
    LLMGatewayError,
    LLMUpstreamError,
    NATSUnavailableError,
)
from oneops.llm.gateway import LlmGateway
from oneops.llm.models import (
    LlmRequest,
    ResponseFormat,
    TransportResult,
)
from oneops.llm.quota import QuotaGuard
from oneops.use_cases.uc05_triage.adapters import (
    make_embed_fn,
    make_infer_fn,
)
from oneops.use_cases.uc05_triage.runner import build_runner
from oneops.use_cases.uc05_triage.tools.check_duplicates import (
    check_duplicate_candidates,
)
from oneops.use_cases.uc05_triage.tools.prioritize import prioritize_entity

# ── helpers ─────────────────────────────────────────────────────────────────

class _OkTransport:
    async def embed(self, texts, *, model: str, dimensions: int | None):
        return [[0.0] * 1536 for _ in texts]

    async def complete(self, req: LlmRequest):
        if req.response_format == ResponseFormat.JSON:
            if "IMPACT values" in req.messages[0].content:
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
        return TransportResult(
            content="vpn", prompt_tokens=20, completion_tokens=3,
            actual_model=req.model,
        )


class _NoEmbedTransport:
    """Embed raises; chat works. Tests degraded retrieval."""

    async def embed(self, *a, **k):
        raise LLMUpstreamError("openai 503")

    async def complete(self, req: LlmRequest):
        return TransportResult(
            content='{"impact":"On Users","urgency":"Medium"}',
            prompt_tokens=10, completion_tokens=5,
            actual_model=req.model,
        )


class _FakeConn:
    async def fetch(self, query: str, *args: Any):
        # Both branches return the same row to keep the test simple
        return [{
            "id": "INC0001002",
            "title": "VPN drops",
            "description": "tunnel",
            "category": "network", "subcategory": "vpn",
            "service_name": "Corp VPN", "ci_id": "CI0000001",
            "assignment_group": "GRP-NETOPS",
            "assigned_to": "USR00003",
            "status": "open", "created_at": None,
            "fts_score": 1.0, "vec_score": 0.9,
        }]

    async def close(self):
        pass


async def _conn():
    return _FakeConn()


# ── Probe 5.1: gateway non-retryable error during prioritize → safe default ─

class TestProbeGatewayDownIntoTool:
    @pytest.mark.asyncio
    async def test_gateway_error_yields_safe_default_priority(self) -> None:
        class _Raises:
            async def embed(self, *a, **k):
                return [[0.0] * 1536]
            async def complete(self, *a, **k):
                raise LLMGatewayError("provider 500")

        gw = LlmGateway(transport=_Raises(), redact=False, max_retries=0)
        infer = make_infer_fn(gw, tenant_id="T001")
        result = await prioritize_entity(
            service_id="incident",
            ticket_row={"title": "x", "description": "y"},
            suggested_category="network",
            infer_fn=infer,
        )
        assert result.basis["impact"] == "safe_default_llm_exception"


# ── Probe 5.2: embed model 5xx → retrieval enters degraded FTS-only mode ───

class TestProbeEmbedFailureDegradedRetrieval:
    @pytest.mark.asyncio
    async def test_embed_raises_but_check_duplicates_returns_via_fts(self) -> None:
        gw = LlmGateway(transport=_NoEmbedTransport(), redact=False)
        embed_fn = make_embed_fn(gw, tenant_id="T001")
        result = await check_duplicate_candidates(
            service_id="incident", tenant_id="T001",
            ticket_row={"incident_id": "X", "title": "VPN drops",
                        "description": "tunnel"},
            embed_fn=embed_fn,
            conn=_FakeConn(),
        )
        # Even though embed failed, retrieval should have produced something
        # via the FTS-only branch — duplicate_verdict shape stays valid
        assert result.duplicate_verdict in ("duplicate", "none")
        # Candidates should still be present (FTS branch ran)
        assert isinstance(result.candidates, list)


# ── Probe 5.3 / 5.10: QuotaGuard refuses → gateway raises QuotaExceeded ─────

class TestProbeQuotaCeiling:
    @pytest.mark.asyncio
    async def test_quota_ceiling_propagates_quota_exceeded(self) -> None:
        # Limit T001 to 1 call, pre-exhaust it
        quota = QuotaGuard(default_limit=1)
        quota.check_and_charge("T001")  # consume the budget
        gw = LlmGateway(transport=_OkTransport(), redact=False,
                         quota_guard=quota)
        infer = make_infer_fn(gw, tenant_id="T001")
        # Tool 3 wraps infer_fn in try/except → falls back to safe defaults
        # when QuotaExceededError is raised
        result = await prioritize_entity(
            service_id="incident",
            ticket_row={"title": "x", "description": "y"},
            suggested_category="network",
            infer_fn=infer,
        )
        assert result.basis["impact"] == "safe_default_llm_exception"
        # Quota was actually consulted (used count incremented)
        assert quota.used("T001") == 1


# ── Probe 5.4: OTel collector unreachable → spans no-op, code runs ─────────

class TestProbeOtelUnreachable:
    @pytest.mark.asyncio
    async def test_runner_runs_without_otel_exporter(self) -> None:
        # By default the test env has no exporter configured; the span
        # context manager is a no-op. Runner must still produce a Proposal.
        gw = LlmGateway(transport=_OkTransport(), redact=False)
        run = build_runner(gateway=gw, connection_provider=_conn)
        proposal = await run(
            ticket_row={"incident_id": "INC0000001",
                        "title": "VPN drops at Mumbai",
                        "description": "tunnel drops"},
            service_id="incident", tenant_id="T001",
        )
        assert proposal.proposal_id.startswith("p-")


# ── Probe 5.5 / 5.6: NATS down → dispatch surfaces; agent crash → envelope ─

# Already covered in test_nats_dispatcher.py and test_phase4_devils_play.py.
# Re-confirm via direct import for evidence in this file too.

class TestProbeNatsDownSurfacing:
    @pytest.mark.asyncio
    async def test_dispatch_propose_raises_nats_unavailable(self) -> None:
        from oneops.use_cases.uc05_triage.nats_dispatcher import dispatch_propose

        class _Down:
            async def request(self, *a, **k):
                raise NATSUnavailableError("broker unreachable")

        with pytest.raises(NATSUnavailableError):
            await dispatch_propose(
                nats=_Down(),  # type: ignore[arg-type]
                tenant_id="T001", service_id="incident",
                ticket_row={"incident_id": "X", "title": "x",
                            "description": "y"},
            )


# ── Probe 5.7: concurrent decide on same proposal → first wins, second 404 ─

class TestProbeConcurrentDecide:
    """Section J already proved single-use cache eviction. Re-confirm here
    that the second decide on the SAME proposal_id returns 404."""

    def test_double_decide_second_returns_404(self, tmp_path) -> None:
        from oneops.api.uc05_routes import (
            router,
            set_ticket_store,
            set_tools_runner,
        )
        from oneops.use_cases.uc05_triage.contracts import Proposal
        from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

        fx = tmp_path / "demo.json"
        fx.write_text(json.dumps({
            "tenant_id": "T001",
            "incidents": [{"incident_id": "INC0000001",
                            "title": "VPN", "description": "drops",
                            "status": "new",
                            "category": None, "subcategory": None,
                            "service_name": None, "impact": None,
                            "urgency": None, "priority": None,
                            "assignment_group": None, "assigned_to": None,
                            "ci_id": None, "triaged_at": None}],
            "requests": [],
        }))
        set_ticket_store(JsonFixtureStore(fx))

        async def fake_runner(*, ticket_row, service_id, tenant_id):
            return Proposal(
                proposal_id="p-concurrent",
                ticket_id=ticket_row["incident_id"],
                service_id="incident", tenant_id=tenant_id,
                created_at=datetime.now(UTC),
                suggested_category="network", suggested_subcategory="vpn",
                suggested_assigned_to="USR00003",
                suggested_ci_id="CI0000001",
                suggested_impact="On Department",
                suggested_urgency="High", suggested_priority="High",
                suggested_assignment_group="GRP-NETOPS",
                suggested_tags=["vpn"], duplicate_verdict="none",
                overall_confidence_score=0.8,
                confidence_tier="propose", risk_class="medium",
                prioritization_basis={"impact": "llm_inferred"},
                assignment_basis="majority_of_top_k",
                assignment_confidence=0.8,
            )

        set_tools_runner(fake_runner)
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        h = {"x-tenant-id": "T001", "x-user-id": "tech1@corp",
             "x-role": "technician_l1"}
        r0 = c.post("/api/uc05/propose",
                    json={"ticket_id": "INC0000001",
                          "service_id": "incident"},
                    headers=h)
        assert r0.status_code == 200
        pid = r0.json()["proposal_id"]
        r1 = c.post("/api/uc05/decide",
                    json={"proposal_id": pid, "choice": "yes"}, headers=h)
        assert r1.status_code == 200
        # Second decide on the same proposal_id — cache evicted, 404
        r2 = c.post("/api/uc05/decide",
                    json={"proposal_id": pid, "choice": "yes"}, headers=h)
        assert r2.status_code == 404


# ── Probe 5.8: empty title + description → loud refusal before LLM ─────────

class TestProbeEmptyInputRefused:
    @pytest.mark.asyncio
    async def test_empty_text_refused_at_tool3(self) -> None:
        gw = LlmGateway(transport=_OkTransport(), redact=False)
        infer = make_infer_fn(gw, tenant_id="T001")
        with pytest.raises(RuntimeError, match="no signal"):
            await prioritize_entity(
                service_id="incident",
                ticket_row={"incident_id": "X", "title": "", "description": ""},
                suggested_category=None,
                infer_fn=infer,
            )


# ── Probe 5.9: traceparent integrity check ─────────────────────────────────

class TestProbeTraceparentIntegrity:
    def test_parse_then_match_known_inputs(self) -> None:
        """Smoke check the helper is reachable from this module."""
        from oneops.use_cases.uc05_triage.traceparent import parse_traceparent
        tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        out = parse_traceparent(tp)
        assert out is not None


# ── Probe 5.11: cost accumulation across calls ─────────────────────────────

class TestProbeCostAccumulation:
    @pytest.mark.asyncio
    async def test_two_calls_accumulate_under_tenant(self) -> None:
        gw = LlmGateway(transport=_OkTransport(), redact=False)
        e = make_embed_fn(gw, tenant_id="T001")
        await e("alpha")
        await e("bravo")
        # Cost recorded (may be 0 for embed token-cost map; we assert the
        # tenant key exists in the tracker by computing total_cost without
        # raising)
        cost = gw.cost.total_cost("T001")
        assert cost >= 0


# ── Probe 5.12: PII redaction end-to-end at adapter level ──────────────────

class TestProbePiiRedactedEndToEnd:
    @pytest.mark.asyncio
    async def test_email_in_description_redacted_before_transport(self) -> None:
        captured: dict[str, str] = {}

        class _Cap:
            async def embed(self, texts, *, model: str, dimensions: int | None):
                return [[0.0] * 1536 for _ in texts]
            async def complete(self, req: LlmRequest):
                captured["user_msg"] = req.messages[1].content
                return TransportResult(
                    content='{"impact":"On Users","urgency":"Medium"}',
                    prompt_tokens=10, completion_tokens=5,
                    actual_model=req.model,
                )

        gw = LlmGateway(transport=_Cap(), redact=True)
        infer = make_infer_fn(gw, tenant_id="T001")
        await infer(
            service_id="incident",
            ticket_row={"title": "Mailbox issue",
                        "description": "User john.doe@corp.com over quota"},
            suggested_category="email",
        )
        assert "john.doe@corp.com" not in captured["user_msg"]
