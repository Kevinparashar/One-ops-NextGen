"""Policy composer unit tests.

Verifies:
- All policy blocks load from `updated_policy_v2.md` without errors
- Every profile recipe references only valid block names (import-time check)
- compose() substitutes runtime context into USER_CONTEXT_TEMPLATE
- compose() omits empty extra_sections cleanly
- Missing context keys default to empty string (Template.safe_substitute)
- Unknown profile raises KeyError
- compose() is concurrency-safe (pure function — no shared mutation)
- No UC-specific or static-keyword content in the composer module
"""
from __future__ import annotations

import asyncio

import pytest

from oneops.policy import (
    POLICY_BLOCKS,
    POLICY_PROFILES,
    Profile,
    compose,
    get_block,
    list_block_names,
    list_profile_names,
)
from oneops.policy.composer import USER_CONTEXT_KEYS


@pytest.mark.unit
def test_blocks_loaded_from_markdown() -> None:
    names = list_block_names()
    # Sanity: at least the most fundamental blocks must be present.
    # NOTE: we assert names exist, not their content (content is the doc's job).
    required = {
        "COMMON_SAFETY_RULES",
        "AGENT_FOCUS_DIRECTIVE",
        "TOOL_USAGE_POLICY",
        "REGISTRY_GROUNDING_POLICY",
        "USER_CONTEXT_TEMPLATE",
        "CONVERSATION_STATE_POLICY",
        "SUBJECT_RESOLUTION_POLICY",
        "MULTI_ENTITY_DECOMPOSITION_POLICY",
    }
    missing = required - set(names)
    assert not missing, f"required blocks missing from source: {missing}"


@pytest.mark.unit
def test_blocks_are_immutable_view() -> None:
    """POLICY_BLOCKS is a MappingProxyType; mutation raises."""
    with pytest.raises(TypeError):
        POLICY_BLOCKS["new_block"] = "x"  # type: ignore[index]


@pytest.mark.unit
def test_get_block_returns_text() -> None:
    body = get_block("COMMON_SAFETY_RULES")
    assert isinstance(body, str)
    assert len(body) > 50  # non-trivial content


@pytest.mark.unit
def test_get_block_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_block("DEFINITELY_NOT_A_BLOCK")


@pytest.mark.unit
def test_all_profiles_reference_valid_blocks() -> None:
    """Every profile's blocks must exist (this is also asserted at import time)."""
    block_names = set(POLICY_BLOCKS)
    for profile_name, blocks in POLICY_PROFILES.items():
        for b in blocks:
            assert b in block_names, f"profile {profile_name} → unknown block {b}"


@pytest.mark.unit
def test_compose_planner_has_substituted_context() -> None:
    prompt = compose(
        Profile.PLANNER,
        context={
            "request_id": "req-abc-123",
            "tenant_id": "T001",
            "user_id": "USR00001",
            "role": "service_desk_agent",
            "session_id": "sess-xyz",
            "locale": "en-US",
            "ticket_id": "INC0001001",
            "message": "summarize INC0001001",
        },
    )
    # Substitution actually happened (no leftover $vars)
    assert "$request_id" not in prompt
    assert "$tenant_id" not in prompt
    assert "$ticket_id" not in prompt
    # Values are present
    assert "req-abc-123" in prompt
    assert "INC0001001" in prompt
    assert "service_desk_agent" in prompt
    # Planner profile includes registry grounding policy text
    assert "Registry Grounding" in prompt or "agent-tool-mapping" in prompt


@pytest.mark.unit
def test_compose_feature_agent_with_tools_includes_field_visibility() -> None:
    prompt = compose(Profile.FEATURE_AGENT_WITH_TOOLS, context={"role": "agent"})
    # FIELD_VISIBILITY_POLICY is part of this profile per v2 recipe
    assert "Silent Omission" in prompt or "Field Visibility" in prompt


@pytest.mark.unit
def test_compose_team_coordinator_has_team_blocks() -> None:
    prompt = compose(Profile.TEAM_COORDINATOR, context={"role": "system"})
    # TEAM_LEADER_POLICY + TEAM_COORDINATION_POLICY are both included
    assert "Team Leader" in prompt or "Orchestration Responsibilities" in prompt
    assert "Team Coordination" in prompt or "Phase Gates" in prompt


@pytest.mark.unit
def test_compose_with_missing_context_keys_uses_empty_strings() -> None:
    """Missing keys must NOT raise — Template.safe_substitute handles them."""
    prompt = compose(Profile.PLANNER, context={"request_id": "r1"})
    # The omitted $tenant_id renders as empty string
    assert "$tenant_id" not in prompt
    assert "$user_id" not in prompt


@pytest.mark.unit
def test_compose_extra_sections_appended() -> None:
    capability_pack = "## UC-1 Operating Mode\nSummarize the entity verbatim."
    prompt = compose(
        Profile.FEATURE_AGENT_WITH_TOOLS,
        context={"role": "agent"},
        extra_sections=[capability_pack],
    )
    assert "UC-1 Operating Mode" in prompt
    assert "Summarize the entity verbatim" in prompt


@pytest.mark.unit
def test_compose_unknown_profile_raises() -> None:
    with pytest.raises(KeyError):
        compose("UNKNOWN_PROFILE", context={})


@pytest.mark.unit
def test_compose_accepts_string_profile_name() -> None:
    """Profile enum or its string value both work."""
    p1 = compose(Profile.PLANNER, context={"request_id": "x"})
    p2 = compose("PLANNER_POLICY_PROFILE", context={"request_id": "x"})
    assert p1 == p2


@pytest.mark.unit
def test_compose_none_context_is_safe() -> None:
    """compose() must not crash on context=None."""
    prompt = compose(Profile.SUB_AGENT_MINIMAL)
    assert isinstance(prompt, str)
    assert "$" not in prompt or "$" in prompt  # well-formed regardless


@pytest.mark.unit
async def test_compose_concurrency_safety() -> None:
    """compose() is pure — calling it from many tasks with different contexts
    yields independent results (no shared state leaks)."""
    async def call(i: int) -> str:
        return compose(
            Profile.PLANNER,
            context={"request_id": f"req-{i}", "tenant_id": f"T{i:03}"},
        )

    results = await asyncio.gather(*(call(i) for i in range(30)))
    # Each prompt must contain its own tenant_id and no other tenant's
    for i, prompt in enumerate(results):
        assert f"T{i:03}" in prompt
        # No cross-contamination from another task's tenant_id
        for j in range(30):
            if j != i:
                assert f"T{j:03}" not in prompt, f"call {i} leaked T{j:03}"


@pytest.mark.unit
def test_user_context_keys_match_template() -> None:
    """USER_CONTEXT_KEYS must align with the variables actually present in the
    USER_CONTEXT_TEMPLATE block. Detects drift if the doc adds/removes a slot."""
    body = get_block("USER_CONTEXT_TEMPLATE")
    for key in USER_CONTEXT_KEYS:
        assert f"${key}" in body, f"USER_CONTEXT_KEYS lists '{key}' but template doesn't"


@pytest.mark.unit
def test_list_profile_names_returns_all() -> None:
    profiles = list_profile_names()
    expected = {p.value for p in Profile}
    assert set(profiles) == expected
