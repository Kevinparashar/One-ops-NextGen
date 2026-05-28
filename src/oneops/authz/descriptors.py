"""Bridge — registry records → AuthZ `ResourceDescriptor`.

P1 produces declarative `AgentRecord` / `ToolRecord`s; P4 decides access over
`ResourceDescriptor`s. These builders are the single seam between the two, so
the AuthZ evaluator never imports registry internals and the registry never
knows about AuthZ.

The registry's `AbacTags` already carries `tier`, `audience`, and
`data_classification`; the enums share value strings with the AuthZ enums, so
the mapping is value-preserving.
"""
from __future__ import annotations

from oneops.authz.models import DataClass, ResourceDescriptor, Tier
from oneops.registry.models import AgentRecord, ToolRecord


def from_agent_record(agent: AgentRecord, *, resource_tenant_id: str) -> ResourceDescriptor:
    """The access-control descriptor for invoking a use-case agent.

    `resource_tenant_id` is the tenant whose data the call targets — it comes
    from the request envelope, and Rule 1 (tenant isolation) compares it to
    the caller's tenant."""
    tags = agent.abac_tags
    return ResourceDescriptor(
        resource_id=agent.id,
        resource_tenant_id=resource_tenant_id,
        tier=Tier(tags.tier.value),
        data_classification=DataClass(tags.data_classification.value),
        audience=tuple(tags.audience),
        required_scopes=(),                 # scopes are declared per tool, not per agent
    )


def from_tool_record(tool: ToolRecord, *, resource_tenant_id: str) -> ResourceDescriptor:
    """The access-control descriptor for invoking a tool. A tool gates on its
    `requires_scopes` and its execution tier; audience gating is the agent's
    job (the tool's owning agent already passed the audience check)."""
    return ResourceDescriptor(
        resource_id=tool.id,
        resource_tenant_id=resource_tenant_id,
        tier=Tier(tool.execution_type.value),
        data_classification=DataClass.INTERNAL,
        audience=(),
        required_scopes=tuple(tool.requires_scopes),
    )


__all__ = ["from_agent_record", "from_tool_record"]
