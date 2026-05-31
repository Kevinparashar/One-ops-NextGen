"""Unit tests for Tool 3: prioritize_entity.

Devil's-play coverage:
  • Every cell of the Motadata 4x4 matrix (16 combinations)
  • Incident path: LLM happy / off-vocab / exception / None
  • Request path: each catalog category → impact mapping
  • VIP override on a normally-low request and on an incident
  • Breached SLA → Urgent
  • Missing catalog category → default_when_unmatched
  • Missing SLA state → default_when_no_sla
  • Empty input across all signals → raises loud
  • Off-vocabulary impact / urgency from LLM → safe default + basis
  • Case sensitivity ("HIGH" ≠ "High") → rejected
  • Unknown service_id → loud ValueError
"""
from __future__ import annotations

from typing import Any

import pytest

from oneops.use_cases.uc05_triage.tools.prioritize import (
    _SAFE_DEFAULT_IMPACT,
    _SAFE_DEFAULT_URGENCY,
    prioritize_entity,
)

# Motadata matrix as locked in service-schema.json — reference for cell tests
_EXPECTED_MATRIX: dict[tuple[str, str], str] = {
    ("Low",           "Low"):    "Low",
    ("Low",           "Medium"): "Low",
    ("Low",           "High"):   "Medium",
    ("Low",           "Urgent"): "Medium",
    ("On Users",      "Low"):    "Low",
    ("On Users",      "Medium"): "Low",
    ("On Users",      "High"):   "Medium",
    ("On Users",      "Urgent"): "High",
    ("On Department", "Low"):    "Medium",
    ("On Department", "Medium"): "Medium",
    ("On Department", "High"):   "High",
    ("On Department", "Urgent"): "Urgent",
    ("On Business",   "Low"):    "Medium",
    ("On Business",   "Medium"): "High",
    ("On Business",   "High"):   "Urgent",
    ("On Business",   "Urgent"): "Urgent",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _ticket(**fields: Any) -> dict[str, Any]:
    base = {"incident_id": "INC0001175", "title": "T", "description": "D"}
    base.update(fields)
    return base


def _make_llm(impact: str, urgency: str):
    async def fn(*, service_id, ticket_row, suggested_category,
                 suggested_subcategory, suggested_service_name, vip_flag):
        return {"impact": impact, "urgency": urgency}
    return fn


# ── 16-cell matrix coverage ──────────────────────────────────────────────────

@pytest.mark.parametrize(("impact", "urgency", "expected_priority"),
                         [(i, u, _EXPECTED_MATRIX[(i, u)])
                          for i, u in _EXPECTED_MATRIX])
class TestMatrixCells:
    @pytest.mark.asyncio
    async def test_each_matrix_cell(self, impact, urgency, expected_priority) -> None:
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=_make_llm(impact, urgency),
        )
        assert r.impact == impact
        assert r.urgency == urgency
        assert r.priority == expected_priority


# ── Incident LLM happy / failure paths ───────────────────────────────────────

class TestIncidentLLM:
    @pytest.mark.asyncio
    async def test_llm_off_vocab_falls_back_safely(self) -> None:
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=_make_llm("Catastrophic", "Yesterday"),
        )
        assert r.impact == _SAFE_DEFAULT_IMPACT
        assert r.urgency == _SAFE_DEFAULT_URGENCY
        assert r.basis["impact"] == "safe_default_llm_invalid"

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back(self) -> None:
        async def broken(**_):
            raise RuntimeError("gateway down")
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=broken,
        )
        assert r.impact == _SAFE_DEFAULT_IMPACT
        assert r.basis["impact"] == "safe_default_llm_exception"

    @pytest.mark.asyncio
    async def test_no_infer_fn_falls_back(self) -> None:
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=None,
        )
        assert r.basis["impact"] == "safe_default_no_llm"

    @pytest.mark.asyncio
    async def test_case_sensitive_rejection(self) -> None:
        # "HIGH" should not match "High" — Motadata vocabulary is case-sensitive
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=_make_llm("On Users", "HIGH"),
        )
        # urgency rejected → falls back to safe default
        assert r.urgency == _SAFE_DEFAULT_URGENCY
        assert "safe_default_llm_invalid" in r.basis["urgency"]

    @pytest.mark.asyncio
    async def test_vip_override_on_incident(self) -> None:
        """Even if LLM says On Users, VIP must lift impact to On Business."""
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            vip_flag=True,
            infer_fn=_make_llm("On Users", "High"),
        )
        assert r.impact == "On Business"
        assert "vip_override" in r.basis["impact"]


# ── Request deterministic path ───────────────────────────────────────────────

class TestRequestPath:
    @pytest.mark.asyncio
    async def test_hardware_request_on_users(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002001", "title": "Laptop", "description": "new"},
            suggested_category="hardware",
            suggested_catalog_item_id="CAT_LAPTOP_STD",
            sla_state="healthy",
        )
        assert r.impact == "On Users"
        assert r.urgency == "Low"
        assert r.priority == "Low"  # On Users + Low = Low

    @pytest.mark.asyncio
    async def test_onboarding_request_on_department(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002002", "title": "Onboard", "description": "new joiner"},
            suggested_category="onboarding",
            sla_state="approaching_50pct",
        )
        assert r.impact == "On Department"
        assert r.urgency == "Medium"
        assert r.priority == "Medium"

    @pytest.mark.asyncio
    async def test_sla_breached_forces_urgent(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002003", "title": "Access", "description": "needed"},
            suggested_category="access",
            sla_state="breached",
        )
        assert r.urgency == "Urgent"
        assert r.impact == "On Users"
        assert r.priority == "High"  # On Users + Urgent = High

    @pytest.mark.asyncio
    async def test_vip_override_on_request(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002004", "title": "Hardware", "description": "vip"},
            suggested_category="hardware",
            vip_flag=True,
            sla_state="healthy",
        )
        assert r.impact == "On Business"
        assert r.basis["impact"] == "vip_override"

    @pytest.mark.asyncio
    async def test_unknown_catalog_category_fallback(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002005", "title": "x", "description": "y"},
            suggested_category="quantum-flux",  # nonsense
            sla_state="healthy",
        )
        # Falls back to default_when_unmatched (On Users per service-schema.json)
        assert r.impact in {"On Users"}
        assert r.basis["impact"] == "default_when_unmatched"

    @pytest.mark.asyncio
    async def test_missing_sla_state_fallback(self) -> None:
        r = await prioritize_entity(
            service_id="request",
            ticket_row={"request_id": "SR0002006", "title": "x", "description": "y"},
            suggested_category="hardware",
            sla_state=None,
        )
        assert r.urgency == "Low"
        assert r.basis["urgency"] == "default_when_no_sla"


# ── Empty input + bad service_id ─────────────────────────────────────────────

class TestRefusal:
    @pytest.mark.asyncio
    async def test_unknown_service_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported service_id"):
            await prioritize_entity(
                service_id="problem",
                ticket_row=_ticket(),
            )

    @pytest.mark.asyncio
    async def test_completely_empty_input_raises(self) -> None:
        with pytest.raises(RuntimeError, match="no signal"):
            await prioritize_entity(
                service_id="incident",
                ticket_row={"incident_id": "INC0001175", "title": "", "description": ""},
                suggested_category=None,
            )

    @pytest.mark.asyncio
    async def test_title_only_still_runs(self) -> None:
        """Per user rule D: title-only is enough to run."""
        r = await prioritize_entity(
            service_id="incident",
            ticket_row={"incident_id": "X", "title": "VPN drops", "description": ""},
            suggested_category="network",
            infer_fn=_make_llm("On Users", "Medium"),
        )
        assert r.impact == "On Users"

    @pytest.mark.asyncio
    async def test_description_only_still_runs(self) -> None:
        r = await prioritize_entity(
            service_id="incident",
            ticket_row={"incident_id": "X", "title": "", "description": "VPN dropping constantly"},
            suggested_category="network",
            infer_fn=_make_llm("On Users", "Medium"),
        )
        assert r.impact == "On Users"


# ── Auditable basis dict ─────────────────────────────────────────────────────

class TestBasis:
    @pytest.mark.asyncio
    async def test_basis_dict_explains_each_axis(self) -> None:
        r = await prioritize_entity(
            service_id="incident",
            ticket_row=_ticket(),
            suggested_category="network",
            infer_fn=_make_llm("On Department", "High"),
        )
        assert "impact" in r.basis
        assert "urgency" in r.basis
        assert "priority" in r.basis
        assert "matrix" in r.basis["priority"]
        assert "On Department" in r.basis["priority"]
