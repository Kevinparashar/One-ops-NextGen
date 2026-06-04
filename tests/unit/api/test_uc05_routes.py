"""Devil's-play tests for UC-5 API routes — RBAC + queue + propose + decide."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from oneops.api.uc05_routes import (
    router,
    set_ticket_store,
    set_tools_runner,
)
from oneops.use_cases.uc05_triage.contracts import Proposal
from oneops.use_cases.uc05_triage.stores.json_store import JsonFixtureStore

# ── Fixtures ────────────────────────────────────────────────────────────────

def _fixture(tmp: Path) -> Path:
    p = tmp / "demo.json"
    p.write_text(json.dumps({
        "tenant_id": "T001",
        "incidents": [
            # untriaged
            {"incident_id": "DEMO_INC_001", "title": "VPN drops",
             "description": "tunnel keeps dropping at HQ ...", "status": "new",
             "category": None, "subcategory": None, "service_name": None,
             "impact": None, "urgency": None, "priority": None,
             "assignment_group": None, "assigned_to": None,
             "ci_id": None, "created_at": "2026-05-29T18:00:00Z",
             "triaged_at": None},
            # fully triaged → not in queue
            {"incident_id": "DEMO_INC_002", "title": "Old fully triaged",
             "description": "...", "status": "assigned",
             "category": "network", "subcategory": "vpn",
             "service_name": "Corp VPN", "impact": "On Users", "urgency": "High",
             "priority": "Medium", "assignment_group": "GRP-NETOPS",
             "assigned_to": "USR00003", "ci_id": "CI001",
             "created_at": "2026-05-29T17:00:00Z", "triaged_at": "2026-05-29T17:30:00Z"},
            # closed → not in queue
            {"incident_id": "DEMO_INC_003", "title": "Closed",
             "description": "...", "status": "closed",
             "category": None, "subcategory": None, "service_name": None,
             "impact": None, "urgency": None, "priority": None,
             "assignment_group": None, "assigned_to": None,
             "ci_id": None, "created_at": "2026-05-28T10:00:00Z",
             "triaged_at": None},
        ],
        "requests": [
            {"request_id": "DEMO_SR_001", "title": "Laptop",
             "description": "...", "status": "new",
             "category": None, "catalog_item_id": None,
             "priority": None, "assignment_group": None, "assigned_to": None,
             "ci_id": None, "created_at": "2026-05-29T18:30:00Z",
             "triaged_at": None},
        ],
    }))
    return p


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    set_ticket_store(JsonFixtureStore(_fixture(tmp_path)))
    set_tools_runner(_fake_tools_runner)
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# Stub Tools runner — returns a Proposal without calling real LLM/DB
async def _fake_tools_runner(
    *, ticket_row, service_id, tenant_id
):
    return Proposal(
        proposal_id="p-test-001",
        ticket_id=ticket_row.get(f"{service_id}_id"),
        service_id=service_id,
        tenant_id=tenant_id,
        created_at=datetime.now(UTC),
        suggested_category="network",
        suggested_subcategory="vpn" if service_id == "incident" else None,
        suggested_assigned_to="USR00003",
        suggested_ci_id="CI0000001",
        suggested_impact="On Department",
        suggested_urgency="High",
        suggested_priority="High",
        suggested_assignment_group="GRP-NETOPS",
        suggested_tags=["vpn", "tunnel"],
        duplicate_verdict="none",
        overall_confidence_score=0.8,
        confidence_tier="propose",
        risk_class="medium",
        prioritization_basis={"impact": "llm_inferred"},
        assignment_basis="majority_of_top_k",
        assignment_confidence=0.8,
    )


def _auth(role="technician_l1", tenant="T001", user="tech1@corp"):
    return {"x-tenant-id": tenant, "x-user-id": user, "x-role": role}


# ── RBAC devil's-play ────────────────────────────────────────────────────────

class TestRBAC:
    def test_missing_tenant_header_401(self, client) -> None:
        r = client.get("/api/uc05/queue-summary",
                       headers={"x-user-id": "u", "x-role": "technician_l1"})
        assert r.status_code == 401
        assert "tenant" in r.text.lower()

    def test_missing_role_header_401(self, client) -> None:
        r = client.get("/api/uc05/queue-summary",
                       headers={"x-tenant-id": "T001", "x-user-id": "u"})
        assert r.status_code == 401

    def test_employee_role_403(self, client) -> None:
        r = client.get("/api/uc05/queue-summary", headers=_auth(role="employee"))
        assert r.status_code == 403

    def test_guest_role_403(self, client) -> None:
        r = client.get("/api/uc05/queue-summary", headers=_auth(role="guest"))
        assert r.status_code == 403

    def test_admin_role_passes(self, client) -> None:
        r = client.get("/api/uc05/queue-summary", headers=_auth(role="admin"))
        assert r.status_code == 200


# ── Queue summary ────────────────────────────────────────────────────────────

class TestQueueSummary:
    def test_counts_only_untriaged(self, client) -> None:
        r = client.get("/api/uc05/queue-summary", headers=_auth())
        assert r.status_code == 200
        body = r.json()
        # 1 untriaged incident (DEMO_INC_001); DEMO_INC_002 fully triaged, DEMO_INC_003 closed
        assert body["incidents"]["untriaged_count"] == 1
        assert body["requests"]["untriaged_count"] == 1

    def test_other_tenant_sees_zero(self, client) -> None:
        r = client.get("/api/uc05/queue-summary",
                       headers=_auth(tenant="T999"))
        assert r.status_code == 200
        assert r.json() == {"incidents": {"untriaged_count": 0},
                            "requests": {"untriaged_count": 0}}


# ── Queue list ───────────────────────────────────────────────────────────────

class TestQueueList:
    def test_lists_incidents(self, client) -> None:
        r = client.get("/api/uc05/queue?service_id=incident", headers=_auth())
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["ticket_id"] == "DEMO_INC_001"
        assert rows[0]["missing_field_count"] == 7
        assert "category" in rows[0]["missing_fields"]

    def test_lists_requests(self, client) -> None:
        r = client.get("/api/uc05/queue?service_id=request", headers=_auth())
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["ticket_id"] == "DEMO_SR_001"
        assert rows[0]["missing_field_count"] == 4

    def test_fully_triaged_not_in_queue(self, client) -> None:
        r = client.get("/api/uc05/queue?service_id=incident", headers=_auth())
        ids = [row["ticket_id"] for row in r.json()]
        assert "DEMO_INC_002" not in ids  # fully triaged

    def test_closed_not_in_queue(self, client) -> None:
        r = client.get("/api/uc05/queue?service_id=incident", headers=_auth())
        ids = [row["ticket_id"] for row in r.json()]
        assert "DEMO_INC_003" not in ids

    def test_invalid_service_id_422(self, client) -> None:
        r = client.get("/api/uc05/queue?service_id=problem", headers=_auth())
        assert r.status_code == 422


# ── Propose ──────────────────────────────────────────────────────────────────

class TestPropose:
    def test_happy_path(self, client) -> None:
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_001",
                              "service_id": "incident"},
                        headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["proposal_id"].startswith("p-")
        assert body["suggested_category"] == "network"
        assert body["risk_class"] == "medium"
        assert body["mutation_intent"] == "recommend_only"

    def test_bad_ticket_404(self, client) -> None:
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_999",
                              "service_id": "incident"},
                        headers=_auth())
        assert r.status_code == 404

    def test_tenant_mismatch_404_no_leak(self, client) -> None:
        # DEMO_INC_001 exists in T001; T999 should see 404 (not 403, not "exists elsewhere")
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_001",
                              "service_id": "incident"},
                        headers=_auth(tenant="T999"))
        assert r.status_code == 404

    def test_already_triaged_409(self, client) -> None:
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_002",
                              "service_id": "incident"},
                        headers=_auth())
        assert r.status_code == 409

    def test_wrong_service_for_id_404(self, client) -> None:
        # incident ID passed with service_id=request → not found in requests
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_001",
                              "service_id": "request"},
                        headers=_auth())
        assert r.status_code == 404

    def test_invalid_service_id_422(self, client) -> None:
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "x", "service_id": "problem"},
                        headers=_auth())
        assert r.status_code == 422


# ── Decide ───────────────────────────────────────────────────────────────────

class TestDecide:
    def _make_proposal(self, client) -> str:
        r = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_001",
                              "service_id": "incident"},
                        headers=_auth())
        return r.json()["proposal_id"]

    def test_decide_yes_applies(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes"},
                        headers=_auth())
        assert r.status_code == 200
        assert r.json()["outcome"] == "applied"

    def test_decide_no_discards(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "no"},
                        headers=_auth())
        assert r.status_code == 200
        assert r.json()["outcome"] == "discarded"

    def test_decide_yes_with_edits(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes",
                              "final_values": {"category": "platform",
                                               "priority": "Urgent"}},
                        headers=_auth())
        assert r.status_code == 200
        applied = r.json()["applied_fields"]
        assert applied["category"] == "platform"
        assert applied["priority"] == "Urgent"

    def test_decide_with_non_triage_field_422(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes",
                              "final_values": {"incident_id": "hacker"}},
                        headers=_auth())
        assert r.status_code == 422
        assert "non-triage" in r.text.lower()

    def test_decide_invalid_choice_422(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "maybe"},
                        headers=_auth())
        assert r.status_code == 422

    def test_decide_unknown_proposal_404(self, client) -> None:
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": "p-no-such", "choice": "yes"},
                        headers=_auth())
        assert r.status_code == 404

    def test_decide_other_tenant_404(self, client) -> None:
        pid = self._make_proposal(client)
        r = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes"},
                        headers=_auth(tenant="T999"))
        assert r.status_code == 404

    def test_double_decide_second_404(self, client) -> None:
        pid = self._make_proposal(client)
        r1 = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes"},
                        headers=_auth())
        assert r1.status_code == 200
        r2 = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "yes"},
                        headers=_auth())
        assert r2.status_code == 404  # cache evicted, single-use


# ── Cancel keeps ticket untouched ────────────────────────────────────────────

class TestCancelKeepsTicket:
    def test_no_then_propose_again_succeeds(self, client) -> None:
        # Propose, decide No, queue still shows the ticket
        r1 = client.post("/api/uc05/propose",
                        json={"ticket_id": "DEMO_INC_001",
                              "service_id": "incident"},
                        headers=_auth())
        pid = r1.json()["proposal_id"]
        r2 = client.post("/api/uc05/decide",
                        json={"proposal_id": pid, "choice": "no"},
                        headers=_auth())
        assert r2.json()["outcome"] == "discarded"
        # Ticket still in queue
        r3 = client.get("/api/uc05/queue?service_id=incident", headers=_auth())
        ids = [row["ticket_id"] for row in r3.json()]
        assert "DEMO_INC_001" in ids
