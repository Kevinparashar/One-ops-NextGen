"""Profile composition + runtime context substitution.

Profile recipes are the canonical compositions from `updated_policy_v2.md`:

  INTERNAL_AGENT_POLICY              = COMMON_SAFETY_RULES + INTERNAL_AGENT_EXECUTION_BLOCK
  PLATFORM_SYSTEM_POLICY             = COMMON_SAFETY_RULES + AGENT_FOCUS_DIRECTIVE
  PLANNER_POLICY_PROFILE             = INTERNAL_AGENT_POLICY + REGISTRY_GROUNDING_POLICY + ...
  FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE = PLATFORM_SYSTEM_POLICY + ...
  TEAM_COORDINATOR_POLICY_PROFILE    = COMMON_SAFETY_RULES + TEAM_LEADER_POLICY + ...
  SUB_AGENT_POLICY_PROFILE (minimal) = COMMON_SAFETY_RULES + SUB_AGENT_POLICY + ...

A profile is an ordered list of block names. compose() concatenates the blocks
and substitutes runtime context into the USER_CONTEXT_TEMPLATE block.

NO HARDCODED UC NAMES: the profiles reference policy blocks only; UC content
lives outside policy.
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from string import Template
from types import MappingProxyType
from typing import Any

from oneops.errors import ConfigError
from oneops.observability import get_logger
from oneops.policy.blocks import POLICY_BLOCKS, get_block

_log = get_logger("oneops.policy.composer")

# Required keys for USER_CONTEXT_TEMPLATE substitution. Caller must supply each;
# we provide a sane empty-string default to avoid Template errors on missing keys.
USER_CONTEXT_KEYS: tuple[str, ...] = (
    "message",
    "request_id",
    "tenant_id",
    "user_id",
    "role",
    "session_id",
    "locale",
    "ticket_id",
)


class Profile(str, Enum):
    """Named profiles. String-valued so they round-trip cleanly through logs/traces."""

    INTERNAL_AGENT = "INTERNAL_AGENT_POLICY"
    PLATFORM_SYSTEM = "PLATFORM_SYSTEM_POLICY"
    PLANNER = "PLANNER_POLICY_PROFILE"
    FEATURE_AGENT = "FEATURE_AGENT_POLICY_PROFILE"
    FEATURE_AGENT_WITH_TOOLS = "FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE"
    FEATURE_AGENT_JSON = "FEATURE_AGENT_JSON_POLICY_PROFILE"
    TEAM_COORDINATOR = "TEAM_COORDINATOR_POLICY_PROFILE"
    SUB_AGENT_MINIMAL = "SUB_AGENT_POLICY_PROFILE"


# ── Profile recipes ─────────────────────────────────────────────
# Defined as data (block-name lists). Adding a new profile = add a list here;
# no code changes elsewhere. Matches `updated_policy_v2.md` §Updated Profile Recipes (v2).

_INTERNAL_AGENT_POLICY = (
    "COMMON_SAFETY_RULES",
    "INTERNAL_AGENT_EXECUTION_BLOCK",
)

_PLATFORM_SYSTEM_POLICY = (
    "COMMON_SAFETY_RULES",
    "AGENT_FOCUS_DIRECTIVE",
)

# Planner — v2
_PLANNER_POLICY_PROFILE = _INTERNAL_AGENT_POLICY + (
    "REGISTRY_GROUNDING_POLICY",
    "ORDERING_AND_DEPENDENCY_POLICY",
    "CONTEXT_TOOL_INPUT_BINDING_POLICY",
    "REQUEST_ID_CLARITY_POLICY",
    "TOOL_USAGE_POLICY",
    "OUTPUT_SCHEMA_POLICY",
    "OBSERVABILITY_POLICY",
    "USER_CONTEXT_TEMPLATE",
    "CONVERSATION_STATE_POLICY",
    "SUBJECT_RESOLUTION_POLICY",
    "MULTI_ENTITY_DECOMPOSITION_POLICY",
)

# Feature agent (no tools) — v1 default kept; minimal surface.
_FEATURE_AGENT_POLICY_PROFILE = _PLATFORM_SYSTEM_POLICY + (
    "REGISTRY_GROUNDING_POLICY",
    "REQUEST_ID_CLARITY_POLICY",
    "USER_CONTEXT_TEMPLATE",
)

# Feature agent WITH tools — v2 (adds conversation state, subject resolution,
# field visibility, cross-service navigation).
_FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE = _PLATFORM_SYSTEM_POLICY + (
    "REGISTRY_GROUNDING_POLICY",
    "CONTEXT_TOOL_INPUT_BINDING_POLICY",
    "REQUEST_ID_CLARITY_POLICY",
    "TOOL_USAGE_POLICY",
    "USER_CONTEXT_TEMPLATE",
    "CONVERSATION_STATE_POLICY",
    "SUBJECT_RESOLUTION_POLICY",
    "FIELD_VISIBILITY_POLICY",
    "CROSS_SERVICE_NAVIGATION_POLICY",
)

# Feature agent with strict JSON output (adds OUTPUT_SCHEMA + OBSERVABILITY)
_FEATURE_AGENT_JSON_POLICY_PROFILE = _FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE + (
    "OUTPUT_SCHEMA_POLICY",
    "OBSERVABILITY_POLICY",
)

# Team coordinator — v2
_TEAM_COORDINATOR_POLICY_PROFILE = (
    "COMMON_SAFETY_RULES",
    "TEAM_LEADER_POLICY",
    "TEAM_COORDINATION_POLICY",
    "ORDERING_AND_DEPENDENCY_POLICY",
    "OBSERVABILITY_POLICY",
    "REQUEST_ID_CLARITY_POLICY",
    "USER_CONTEXT_TEMPLATE",
    "CONVERSATION_STATE_POLICY",
    "SUBJECT_RESOLUTION_POLICY",
)

# Sub-agent — minimal
_SUB_AGENT_POLICY_PROFILE = (
    "COMMON_SAFETY_RULES",
    "SUB_AGENT_POLICY",
    "REQUEST_ID_CLARITY_POLICY",
    "USER_CONTEXT_TEMPLATE",
)


_PROFILES_RAW: dict[str, tuple[str, ...]] = {
    Profile.INTERNAL_AGENT.value: _INTERNAL_AGENT_POLICY,
    Profile.PLATFORM_SYSTEM.value: _PLATFORM_SYSTEM_POLICY,
    Profile.PLANNER.value: _PLANNER_POLICY_PROFILE,
    Profile.FEATURE_AGENT.value: _FEATURE_AGENT_POLICY_PROFILE,
    Profile.FEATURE_AGENT_WITH_TOOLS.value: _FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE,
    Profile.FEATURE_AGENT_JSON.value: _FEATURE_AGENT_JSON_POLICY_PROFILE,
    Profile.TEAM_COORDINATOR.value: _TEAM_COORDINATOR_POLICY_PROFILE,
    Profile.SUB_AGENT_MINIMAL.value: _SUB_AGENT_POLICY_PROFILE,
}

POLICY_PROFILES: Mapping[str, tuple[str, ...]] = MappingProxyType(_PROFILES_RAW)


def list_profile_names() -> list[str]:
    return sorted(POLICY_PROFILES)


def _validate_profiles_against_blocks() -> None:
    """Sanity check at import: every profile's blocks exist in POLICY_BLOCKS.

    Fail-fast: a missing block here means the policy markdown got corrupted or
    a profile recipe references a block that was renamed/deleted. Either way the
    LLM would receive a broken prompt at runtime — better to crash now.
    """
    missing: list[tuple[str, str]] = []
    for profile_name, block_names in POLICY_PROFILES.items():
        for block_name in block_names:
            if block_name not in POLICY_BLOCKS:
                missing.append((profile_name, block_name))
    if missing:
        details = ", ".join(f"{p}→{b}" for p, b in missing)
        raise ConfigError(
            f"policy profile references unknown blocks: {details}; "
            f"available blocks: {sorted(POLICY_BLOCKS)}"
        )


_validate_profiles_against_blocks()


# ── compose() ───────────────────────────────────────────────────

# Composed prompts for a *static* profile (called with no per-request context
# and no dynamic_context) are constant for the life of the process — the policy
# blocks are immutable after import. They are memoised here so a hot-path call
# (every routed query hits decompose / disambiguate) never re-concatenates the
# policy text — minimal latency.
#
# A static composition is also byte-identical on every call, so it forms a
# stable system-prompt prefix: the LLM provider's prompt cache applies and the
# policy tokens are billed at the cached (~10%) rate after the first call —
# minimal token cost. Keep per-request variability (the user's message,
# candidates, history) in the *user* message, never in this system prompt.
_STATIC_CACHE: dict[tuple[str, tuple[str, ...]], str] = {}


def _render_situation(dynamic_context: Mapping[str, Any] | None) -> str:
    """Render a ## Situation section the LLM treats as live request context.

    Any non-empty value in `dynamic_context` becomes a labeled line. Keys with
    None / "" / [] / {} are skipped so the prompt only carries signal. Lists
    of dicts are rendered as bullet sub-items (used for recent_turns, linked
    entities, available_capabilities). Pure scalars render inline.

    This is the ONLY mechanism by which situation-specific facts enter the
    prompt — never via Python string interpolation in handlers and never via
    pre-baked markdown. UCs build a dict at request time; the LLM gets all
    the live signal it needs to make a context-aware decision.
    """
    if not dynamic_context:
        return ""
    lines: list[str] = ["## Situation (live context for this turn)"]
    for key, value in dynamic_context.items():
        if value in (None, "", [], {}):
            continue
        label = key.replace("_", " ")
        if isinstance(value, list):
            lines.append(f"\n### {label}")
            for item in value:
                if isinstance(item, dict):
                    snippet = ", ".join(f"{k}={v}" for k, v in item.items() if v not in (None, ""))
                    lines.append(f"- {snippet}")
                else:
                    lines.append(f"- {item}")
        elif isinstance(value, dict):
            snippet = ", ".join(f"{k}={v}" for k, v in value.items() if v not in (None, ""))
            if snippet:
                lines.append(f"- **{label}**: {snippet}")
        else:
            lines.append(f"- **{label}**: {value}")
    return "\n".join(lines) if len(lines) > 1 else ""


def compose(
    profile: Profile | str,
    context: Mapping[str, Any] | None = None,
    *,
    extra_sections: list[str] | None = None,
    dynamic_context: Mapping[str, Any] | None = None,
) -> str:
    """Build the full system prompt for a profile, substituting runtime context.

    Args:
        profile: Profile enum or its string value. KeyError if unknown.
        context: dict of runtime fields for the USER_CONTEXT_TEMPLATE block.
            Recognized keys: message, request_id, tenant_id, user_id, role,
            session_id, locale, ticket_id.
        extra_sections: optional additional text appended at the end (e.g. a UC's
            capability-pack instruction block). Common code never knows what's in
            them — that's the UC author's responsibility.
        dynamic_context: situation-aware facts assembled at request time. Anything
            that should influence the LLM's decision NOW (recent turns, current
            focus, available capabilities, role-redacted fields, prior outcome,
            linked entities on the record) goes here. Rendered as a ## Situation
            section at the prompt tail so the LLM treats it as authoritative
            live data, not policy.

    Returns:
        The fully composed system prompt string.
    """
    profile_name = profile.value if isinstance(profile, Profile) else str(profile)
    if profile_name not in POLICY_PROFILES:
        raise KeyError(
            f"unknown profile {profile_name!r}; available: {list_profile_names()}"
        )

    # Static fast path — no per-request context means a constant result.
    # Served from cache; minimal latency, and a stable byte-identical prefix
    # for the provider's prompt cache.
    is_static = not context and not dynamic_context
    cache_key = (profile_name, tuple(extra_sections or ()))
    if is_static:
        cached = _STATIC_CACHE.get(cache_key)
        if cached is not None:
            return cached

    ctx = {k: "" for k in USER_CONTEXT_KEYS}
    if context:
        for k, v in context.items():
            ctx[k] = "" if v is None else str(v)

    parts: list[str] = []
    for block_name in POLICY_PROFILES[profile_name]:
        body = get_block(block_name)
        if block_name == "USER_CONTEXT_TEMPLATE":
            # Safe substitution — `$missing` literal stays if a key is omitted.
            body = Template(body).safe_substitute(ctx)
        parts.append(body)

    if extra_sections:
        parts.extend(extra_sections)

    situation = _render_situation(dynamic_context)
    if situation:
        parts.append(situation)

    # Join with a clear separator so block boundaries are visible to the LLM.
    result = "\n\n---\n\n".join(p for p in parts if p)
    if is_static:
        _STATIC_CACHE[cache_key] = result
    return result


__all__ = [
    "Profile",
    "POLICY_PROFILES",
    "USER_CONTEXT_KEYS",
    "compose",
    "list_profile_names",
]
