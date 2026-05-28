"""Phase F2 — UC-1 summarization is fast-path-eligible (registry-driven).

Loads the real `registries/v2/` data and proves the dispatcher serves UC-1
through the fast-path entry. UC-3 stays chat-only — confirming the
opt-in shape ([[feedback_poc5mw_design_for_1000_ucs_from_day_1]]: registry
edit is what turns it on, not code change).
"""
from __future__ import annotations

import pytest

from oneops.registry.loader import load_registry
from oneops.router.fast_path import (
    FastPathDispatcher,
    FastPathError,
    FastPathRequest,
)


@pytest.fixture(scope="module")
def live_dispatcher() -> FastPathDispatcher:
    return FastPathDispatcher(load_registry("registries/v2"))


def test_uc01_is_fast_path_eligible(live_dispatcher):
    assert live_dispatcher.is_eligible("uc01_summarization") is True


def test_uc03_kb_lookup_fast_path_is_disabled(live_dispatcher):
    # UC-3's fast_path block is registered but `enabled: false` — hidden
    # from the demo frontend without removing the record (one JSON flip
    # to re-enable). Proves the data-driven on/off discipline.
    assert live_dispatcher.is_eligible("uc03_kb_lookup") is False
    assert live_dispatcher.describe("uc03_kb_lookup") is None


def test_uc01_dispatch_auto_derives_service_id_from_ticket_id(live_dispatcher):
    # The UI sends only `ticket_id`; the registry declares `service_id`
    # as `auto_derive_from=ticket_id`. The dispatcher infers it from the
    # canonical prefix (INC → incident) without UC-specific code.
    out = live_dispatcher.dispatch(FastPathRequest(
        uc_id="uc01_summarization",
        inputs={"ticket_id": "INC0001001"}))
    assert dict(out.plan.steps[0].parameters) == {
        "ticket_id": "INC0001001", "service_id": "incident",
    }


def test_uc01_auto_derivation_covers_every_service_prefix(live_dispatcher):
    cases = [
        ("INC0001001", "incident"),
        ("REQ0001001", "request"),
        ("PBM0003003", "problem"),
        ("CHG0001001", "change"),
        ("AST0001006", "asset"),
        ("CI0000003",  "cmdb_ci"),
    ]
    for ticket_id, expected_service in cases:
        out = live_dispatcher.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={"ticket_id": ticket_id}))
        assert dict(out.plan.steps[0].parameters) == {
            "ticket_id": ticket_id, "service_id": expected_service,
        }, f"derivation failed for {ticket_id}"


def test_uc01_unknown_prefix_still_yields_loud_missing_field(live_dispatcher):
    # Malformed / unrecognised prefix → derivation returns None → the loud
    # "requires fields" error fires. No silent pass-through.
    with pytest.raises(FastPathError, match="requires fields"):
        live_dispatcher.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={"ticket_id": "XYZ12345"}))


def test_uc01_fast_path_declares_summarize_entity_as_primary(live_dispatcher):
    spec = live_dispatcher.describe("uc01_summarization")
    assert spec is not None
    assert spec.primary_tool_id == "summarize_entity"
    field_names = {f.name for f in spec.input_fields}
    assert field_names == {"ticket_id", "service_id"}


def test_uc01_dispatch_builds_one_step_plan(live_dispatcher):
    out = live_dispatcher.dispatch(FastPathRequest(
        uc_id="uc01_summarization",
        inputs={"ticket_id": "INC0048213", "service_id": "incident"}))
    assert len(out.plan.steps) == 1
    step = out.plan.steps[0]
    assert step.agent_id == "uc01_summarization"
    assert dict(step.parameters) == {
        "ticket_id": "INC0048213", "service_id": "incident",
    }


def test_uc01_dispatch_refuses_missing_ticket_id(live_dispatcher):
    # With auto-derivation, service_id is no longer caller-required when a
    # valid ticket_id is supplied. But ticket_id itself remains required —
    # an empty input still produces the loud "requires fields" error.
    with pytest.raises(FastPathError, match="requires fields"):
        live_dispatcher.dispatch(FastPathRequest(
            uc_id="uc01_summarization", inputs={}))


def test_uc01_dispatch_refuses_unknown_field(live_dispatcher):
    with pytest.raises(FastPathError, match="unknown fast-path fields"):
        live_dispatcher.dispatch(FastPathRequest(
            uc_id="uc01_summarization",
            inputs={"ticket_id": "INC0048213", "service_id": "incident",
                    "magic": "no"}))
