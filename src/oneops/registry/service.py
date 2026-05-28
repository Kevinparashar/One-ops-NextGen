"""RegistryService — the registry layer's single entry point.

Composes the three `VersionedStore`s (agents, tools, schemas) and enforces the
*cross-record* integrity rules that no single record can check on its own:

  * every `tool_ref` on an active agent resolves to an active tool;
  * every `depends_on` / `excludes` / `compound_of` agent id resolves to an
    active agent;
  * the `depends_on` graph is acyclic (Parlant dependency relationships must
    form a DAG — the executor builds plan edges from them);
  * `excludes` priorities are unambiguous (no two excluded agents share a
    priority on the same agent).

Integrity is checked explicitly via `check_integrity()` — it is run at load
time (loader.py) and is a CI gate. It raises `RegistryIntegrityError` listing
*every* violation, not just the first, so a fix pass sees the whole picture.
"""
from __future__ import annotations

from oneops.errors import RegistryIntegrityError
from oneops.registry.models import AgentRecord, SchemaRecord, ToolRecord
from oneops.registry.store import FileBackend, RegistryBackend, VersionedStore

AGENT_KIND = "agents"
TOOL_KIND = "tools"
SCHEMA_KIND = "schemas"


class RegistryService:
    """Facade over the agent / tool / schema stores."""

    def __init__(self, backend: RegistryBackend) -> None:
        self.agents: VersionedStore[AgentRecord] = VersionedStore(
            AGENT_KIND, AgentRecord, backend
        )
        self.tools: VersionedStore[ToolRecord] = VersionedStore(
            TOOL_KIND, ToolRecord, backend
        )
        self.schemas: VersionedStore[SchemaRecord] = VersionedStore(
            SCHEMA_KIND, SchemaRecord, backend
        )

    @classmethod
    def from_path(cls, root: str) -> "RegistryService":
        """Build a file-backed service rooted at `root` (e.g. registries/v2)."""
        return cls(FileBackend(root))

    # ── cross-record integrity ───────────────────────────────────────────

    def check_integrity(self) -> None:
        """Validate every cross-record invariant over the ACTIVE record set.

        Raises `RegistryIntegrityError` with the complete violation list when
        anything is wrong. Returns None when the registry is consistent.
        """
        violations: list[str] = []

        active_agents = {a.id: a for a in self.agents.list_active()}
        active_tools = {t.id for t in self.tools.list_active()}

        for agent in active_agents.values():
            # 1. tool_refs resolve to active tools.
            for ref in agent.tool_refs:
                if ref.tool_id not in active_tools:
                    violations.append(
                        f"agent '{agent.id}' references tool '{ref.tool_id}' "
                        "which has no active version"
                    )
            # 2. depends_on / excludes / compound_of resolve to active agents.
            for dep in agent.depends_on:
                if dep not in active_agents:
                    violations.append(
                        f"agent '{agent.id}' depends_on '{dep}' "
                        "which has no active version"
                    )
            for exc in agent.excludes:
                if exc.agent_id not in active_agents:
                    violations.append(
                        f"agent '{agent.id}' excludes '{exc.agent_id}' "
                        "which has no active version"
                    )
            for member in agent.compound_of:
                if member not in active_agents:
                    violations.append(
                        f"compound agent '{agent.id}' includes '{member}' "
                        "which has no active version"
                    )
            # 3. exclusion priorities must be unambiguous within one agent.
            priorities = [e.priority for e in agent.excludes]
            if len(priorities) != len(set(priorities)):
                violations.append(
                    f"agent '{agent.id}' has duplicate exclusion priorities — "
                    "tie-break is ambiguous"
                )

        # 4. the depends_on graph must be acyclic.
        cycle = _find_dependency_cycle(active_agents)
        if cycle:
            violations.append(
                "depends_on graph has a cycle: " + " -> ".join(cycle)
            )

        if violations:
            raise RegistryIntegrityError(
                f"registry integrity check failed ({len(violations)} violation(s)):\n"
                + "\n".join(f"  - {v}" for v in violations)
            )

    # ── convenience reads ────────────────────────────────────────────────

    def active_agent_count(self) -> int:
        return len(self.agents.list_active())

    def resolve_agent_tools(self, agent_id: str) -> list[ToolRecord]:
        """The active tool records an agent references — the executor's
        allowlist for that agent."""
        agent = self.agents.get(agent_id)
        return [self.tools.get(ref.tool_id, ref.version) for ref in agent.tool_refs]


def _find_dependency_cycle(agents: dict[str, AgentRecord]) -> list[str]:
    """Return one cycle in the depends_on graph as a node list, or [] if the
    graph is acyclic. Iterative DFS with a three-colour marking."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {a: WHITE for a in agents}

    def visit(start: str) -> list[str]:
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, child_idx = stack[-1]
            if child_idx == 0:
                colour[node] = GREY
                path.append(node)
            deps = [d for d in agents[node].depends_on if d in agents]
            if child_idx < len(deps):
                stack[-1] = (node, child_idx + 1)
                nxt = deps[child_idx]
                if colour[nxt] == GREY:
                    # Found a back-edge — slice the path from nxt onward.
                    idx = path.index(nxt)
                    return path[idx:] + [nxt]
                if colour[nxt] == WHITE:
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
                path.pop()
                stack.pop()
        return []

    for agent_id in agents:
        if colour[agent_id] == WHITE:
            cycle = visit(agent_id)
            if cycle:
                return cycle
    return []


__all__ = ["RegistryService", "AGENT_KIND", "TOOL_KIND", "SCHEMA_KIND"]
