"""Route plan — the DAG of agent invocations the router hands to the executor.

The router's output is a `RouteResult`: either a `RoutePlan` (a dependency-
ordered DAG of `PlanStep`s) or a non-routed outcome (no confident match, or a
policy denial → the boundary responder).

`assemble_plan` turns the stage-4 agent selection into the DAG:

  * **dependencies** — a selected agent's `depends_on` agents are pulled in
    transitively as upstream steps; the executor runs them first.
  * **exclusions** — when the selection contains two agents and one `excludes`
    the other, the higher-priority agent stays and the other is dropped (a
    declared, logged decision — never a silent fall-through).
  * the steps are topologically ordered (the registry integrity check
    guarantees the `depends_on` graph is acyclic, so this always terminates).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from oneops.observability import get_logger
from oneops.registry.service import RegistryService

_log = get_logger("oneops.router.plan")


@dataclass(frozen=True)
class PlanStep:
    """One agent invocation. `depends_on` holds the step_ids of prerequisites."""

    step_id: str
    agent_id: str
    parameters: tuple[tuple[str, str], ...] = ()
    depends_on: tuple[str, ...] = ()

    def params_dict(self) -> dict[str, str]:
        return dict(self.parameters)


@dataclass(frozen=True)
class RoutePlan:
    """A dependency-ordered DAG of agent invocations."""

    steps: tuple[PlanStep, ...]

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(s.agent_id for s in self.steps)

    @property
    def is_parallelisable(self) -> bool:
        """True when no step depends on another — the executor can fan them out."""
        return all(not s.depends_on for s in self.steps)


class RouteOutcome(str, Enum):
    ROUTED = "routed"                       # a plan was produced
    NO_CONFIDENT_MATCH = "no_confident_match"   # → boundary responder
    POLICY_DENIED = "policy_denied"         # → boundary responder voices the refusal


@dataclass(frozen=True)
class RouteResult:
    """The router's verdict for one request.

    `unrouted` holds the texts of sub-queries that did not route — for a
    compound message where some parts routed and some did not, the outcome is
    still `ROUTED` (a plan exists) and `unrouted` names what the boundary
    responder must tell the user it could not act on.
    """

    outcome: RouteOutcome
    plan: RoutePlan | None = None
    boundary_reason: str = ""               # set when outcome != ROUTED
    diagnostics: tuple[str, ...] = ()       # audit trail of funnel decisions
    unrouted: tuple[str, ...] = ()          # sub-query texts that did not route

    @staticmethod
    def routed(plan: RoutePlan, diagnostics: list[str],
               unrouted: list[str] | None = None) -> "RouteResult":
        return RouteResult(RouteOutcome.ROUTED, plan, "", tuple(diagnostics),
                           tuple(unrouted or ()))

    @staticmethod
    def no_match(reason: str, diagnostics: list[str]) -> "RouteResult":
        return RouteResult(RouteOutcome.NO_CONFIDENT_MATCH, None, reason, tuple(diagnostics))

    @staticmethod
    def policy_denied(reason: str, diagnostics: list[str]) -> "RouteResult":
        return RouteResult(RouteOutcome.POLICY_DENIED, None, reason, tuple(diagnostics))


def _apply_exclusions(agent_ids: list[str], registry: RegistryService) -> list[str]:
    """Drop the lower-priority side of any declared exclusion among the
    selected agents. Higher `priority` wins; the decision is logged."""
    selected = set(agent_ids)
    dropped: set[str] = set()
    for agent_id in agent_ids:
        agent = registry.agents.get(agent_id)
        for exc in agent.excludes:
            if exc.agent_id in selected:
                other = registry.agents.get(exc.agent_id)
                other_pri = next(
                    (e.priority for e in other.excludes if e.agent_id == agent_id), -1
                )
                # This agent excludes `exc.agent_id` at priority `exc.priority`;
                # the other excludes back at `other_pri`. Higher priority keeps.
                loser = exc.agent_id if exc.priority >= other_pri else agent_id
                dropped.add(loser)
                _log.info("router.exclusion_applied",
                          kept=(agent_id if loser == exc.agent_id else exc.agent_id),
                          dropped=loser)
    return [a for a in agent_ids if a not in dropped]


@dataclass
class SubQueryRoute:
    """The funnel outcome for one routed sub-query — the agents it selected,
    their parameters, and which other sub-queries must run first."""

    sub_query_id: str
    agent_ids: list[str]
    parameters_by_agent: dict[str, dict[str, str]] = field(default_factory=dict)
    depends_on_subqueries: list[str] = field(default_factory=list)


def _expand_with_deps(agent_ids: list[str], registry: RegistryService) -> list[str]:
    """Transitively pull in each agent's `depends_on` prerequisites. The
    returned list is topologically ordered — a prerequisite always precedes
    the agent that needs it (post-order DFS; the registry integrity check
    guarantees the graph is acyclic)."""
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(agent_id: str) -> None:
        if agent_id in seen:
            return
        seen.add(agent_id)
        agent = registry.agents.get(agent_id)
        for dep in agent.depends_on:
            visit(dep)
        ordered.append(agent_id)

    for agent_id in agent_ids:
        visit(agent_id)
    return ordered


def _topo_subqueries(routes: list[SubQueryRoute]) -> list[SubQueryRoute]:
    """Order sub-query routes so a route's dependencies precede it."""
    by_id = {r.sub_query_id: r for r in routes}
    ordered: list[SubQueryRoute] = []
    done: set[str] = set()
    active: set[str] = set()

    def visit(sq_id: str) -> None:
        if sq_id in done or sq_id not in by_id:
            return
        if sq_id in active:
            raise ValueError(f"sub-query dependency cycle involving '{sq_id}'")
        active.add(sq_id)
        for dep in by_id[sq_id].depends_on_subqueries:
            visit(dep)
        active.discard(sq_id)
        done.add(sq_id)
        ordered.append(by_id[sq_id])

    for route in routes:
        visit(route.sub_query_id)
    return ordered


def assemble_plan(
    routes: list[SubQueryRoute],
    registry: RegistryService,
) -> RoutePlan:
    """Build the dependency-ordered plan DAG from the per-sub-query routes.

    Dependency edges come from two sources:
      * registry `depends_on` — an agent's prerequisites, within its sub-query;
      * sub-query `depends_on` — every step of a dependent sub-query waits on
        every step of the sub-query it depends on.

    Raises `ValueError` if no route carries any agent — an empty plan is a
    non-routed outcome the caller handles as such.
    """
    routes = [r for r in routes if r.agent_ids]
    if not routes:
        raise ValueError("cannot assemble a plan with no routed sub-queries")

    steps: list[PlanStep] = []
    steps_by_subquery: dict[str, list[str]] = {}
    counter = 0

    for route in _topo_subqueries(routes):
        surviving = _apply_exclusions(list(route.agent_ids), registry)
        needed = _expand_with_deps(surviving, registry)

        # Upstream steps: every step of every sub-query this one depends on.
        upstream: list[str] = []
        for dep_sq in route.depends_on_subqueries:
            upstream.extend(steps_by_subquery.get(dep_sq, []))

        local_step_id: dict[str, str] = {}
        for agent_id in needed:
            counter += 1
            local_step_id[agent_id] = f"step_{counter}"

        for agent_id in needed:
            agent = registry.agents.get(agent_id)
            params = route.parameters_by_agent.get(agent_id, {})
            dep_steps = [local_step_id[d] for d in agent.depends_on] + upstream
            steps.append(PlanStep(
                step_id=local_step_id[agent_id],
                agent_id=agent_id,
                parameters=tuple(sorted(params.items())),
                depends_on=tuple(dep_steps),
            ))
        steps_by_subquery[route.sub_query_id] = list(local_step_id.values())

    return RoutePlan(steps=tuple(steps))


__all__ = [
    "PlanStep", "RoutePlan", "RouteOutcome", "RouteResult",
    "SubQueryRoute", "assemble_plan",
]
