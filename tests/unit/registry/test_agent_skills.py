"""Agent Skills (v0) — schema + backward-compat + registry declarations.

The `Skill` model + `AgentRecord.skills` are ADDITIVE and NOT-yet-wired into
routing. These tests lock: the schema validates, agents WITHOUT skills still parse
(backward compatible), and the active UCs carry well-formed skill cards. See
docs/agent-skills-spec.md.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from oneops.registry.models import Skill

# ── Skill schema ─────────────────────────────────────────────────────────────


def test_skill_minimal_valid():
    s = Skill(id="summarize_ticket", name="Summarize a ticket",
              description="Summarize a record's own fields; use for facts about it.")
    assert s.id == "summarize_ticket"
    assert s.use_when == () and s.not_when == () and s.tags == ()  # optional defaults


def test_skill_full_card_validates_and_is_frozen():
    s = Skill(
        id="search_knowledge_base", name="Search the knowledge base",
        description="Retrieve KB articles; use when the user wants external how-to.",
        use_when=("how do I fix",), not_when=("facts about the record itself",),
        tags=("knowledge", "read"), examples=("how do I reset MFA",))
    assert s.use_when == ("how do I fix",)
    with pytest.raises(ValidationError):       # frozen
        s.id = "x"  # type: ignore[misc]


def test_skill_rejects_empty_required_fields():
    with pytest.raises(ValidationError):
        Skill(id="x", name="", description="d")          # empty name
    with pytest.raises(ValidationError):
        Skill(id="bad id with spaces", name="n", description="d")  # id pattern


# ── backward-compat + registry declarations (loaded via the real registry) ───


@pytest.fixture(scope="module")
def active_agents():
    import os
    os.environ["UC_INVOKER_MODE"] = "local"
    from fastapi.testclient import TestClient

    from oneops.api.app import build_app
    app = build_app()
    with TestClient(app):
        yield list(app.state.registry.agents.list_active())


def test_every_active_agent_has_a_skills_attr(active_agents):
    # Backward-compat: the field exists on every agent (default empty if undeclared).
    assert all(hasattr(a, "skills") for a in active_agents)


def test_the_five_ucs_declare_well_formed_skills(active_agents):
    by_id = {a.id: a for a in active_agents}
    expected = {
        "uc01_summarization": "summarize_ticket",
        "uc02_similar_tickets": "find_similar_tickets",
        "uc03_kb_lookup": "search_knowledge_base",
        "uc05_triage": "triage_ticket",
        "uc08_fulfillment": "fulfill_catalog_request",
    }
    for agent_id, skill_id in expected.items():
        assert agent_id in by_id, f"missing active agent {agent_id}"
        skills = by_id[agent_id].skills
        assert skills, f"{agent_id} declares no skills"
        s = skills[0]
        assert s.id == skill_id
        assert s.name and s.description       # name + what/when present
        assert s.use_when and s.not_when      # disambiguation-as-data present
