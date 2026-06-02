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
from enum import StrEnum

from oneops.observability import get_logger
from oneops.registry.service import RegistryService

_log = get_logger("oneops.router.plan")


@dataclass(frozen=True)
class ParameterBinding:
    """A data-flow edge: feed an upstream step's runtime output into this
    step's input. Resolved by the executor at dispatch time (not plan time).

    `from_field` is a dotted path into the upstream result's `output`
    (e.g. "affected_ci_ids", "summary.root_cause"). `to_param` is the
    downstream handler parameter to populate. `required=True` means a missing
    value blocks the step (surfaced, never silent); `required=False` omits it.
    """

    from_step: str
    from_field: str
    to_param: str
    required: bool = True


@dataclass(frozen=True)
class PlanStep:
    """One agent invocation. `depends_on` holds the step_ids of prerequisites."""

    step_id: str
    agent_id: str
    parameters: tuple[tuple[str, str], ...] = ()
    depends_on: tuple[str, ...] = ()
    # Data-flow bindings: pull declared fields from upstream results into this
    # step's inputs at dispatch time. Empty ⇒ ordering-only dependency (today's
    # behaviour). See `executor.nodes._resolve_bindings`.
    parameter_bindings: tuple[ParameterBinding, ...] = ()
    # Per-dependency enforcement (policy ORDERING §dependency_type): a
    # (dep_step_id, "hard"|"soft") pair. Absent dep ⇒ "hard" (default): a
    # hard dep that did not succeed blocks this step; a soft dep that failed
    # lets this step proceed best-effort. Empty ⇒ all deps hard.
    dependency_types: tuple[tuple[str, str], ...] = ()

    def params_dict(self) -> dict[str, str]:
        return dict(self.parameters)

    def dependency_type(self, dep_step_id: str) -> str:
        """`"hard"` (default) or `"soft"` for the given dependency."""
        for dep, kind in self.dependency_types:
            if dep == dep_step_id:
                return kind
        return "hard"


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


class RouteOutcome(StrEnum):
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
               unrouted: list[str] | None = None) -> RouteResult:
        return RouteResult(RouteOutcome.ROUTED, plan, "", tuple(diagnostics),
                           tuple(unrouted or ()))

    @staticmethod
    def no_match(reason: str, diagnostics: list[str]) -> RouteResult:
        return RouteResult(RouteOutcome.NO_CONFIDENT_MATCH, None, reason, tuple(diagnostics))

    @staticmethod
    def policy_denied(reason: str, diagnostics: list[str]) -> RouteResult:
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
    # Data-flow bindings carried from the decomposer: (from_sq, from_field,
    # to_param). Mapped to step-level ParameterBindings in assemble_plan.
    bindings: list[tuple[str, str, str]] = field(default_factory=list)


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

        # Translate sub-query-level data-flow bindings into step-level
        # ParameterBindings on this route's PRIMARY step (the terminal agent,
        # after its prereqs). Source = the terminal step of the referenced
        # upstream sub-query. This is a structural mapping — no keywords, no
        # field catalogs; the (from_sq, from_field, to_param) triples were
        # declared by the planner LLM, and the executor resolves them with a
        # generic dotted-path lookup at dispatch time.
        primary_agent = needed[-1] if needed else None
        prim_bindings: list[ParameterBinding] = []
        prim_dep_types: list[tuple[str, str]] = []
        prim_extra_deps: list[str] = []
        for from_sq, from_field, to_param in route.bindings:
            up_steps = steps_by_subquery.get(from_sq)
            if not up_steps:
                # Binding names a sub-query that isn't an upstream of this one
                # — a planner mistake. Drop it (loudly, never silent) rather
                # than emit an unresolvable plan.
                _log.warning("router.binding_dropped_unknown_source",
                             route=route.sub_query_id, from_sq=from_sq)
                continue
            from_step = up_steps[-1]
            prim_bindings.append(ParameterBinding(
                from_step=from_step, from_field=from_field,
                to_param=to_param, required=True))
            prim_dep_types.append((from_step, "hard"))
            prim_extra_deps.append(from_step)

        for agent_id in needed:
            agent = registry.agents.get(agent_id)
            params = route.parameters_by_agent.get(agent_id, {})
            dep_steps = [local_step_id[d] for d in agent.depends_on] + upstream
            is_primary = agent_id == primary_agent
            if is_primary and prim_extra_deps:
                # A bound dependency implies an ordering edge — add any that the
                # registry/sub-query deps didn't already cover, so the source
                # has a result before this step resolves its bindings.
                dep_steps = dep_steps + [d for d in prim_extra_deps
                                         if d not in dep_steps]
            steps.append(PlanStep(
                step_id=local_step_id[agent_id],
                agent_id=agent_id,
                parameters=tuple(sorted(params.items())),
                depends_on=tuple(dep_steps),
                parameter_bindings=tuple(prim_bindings) if is_primary else (),
                dependency_types=tuple(prim_dep_types) if is_primary else (),
            ))
        steps_by_subquery[route.sub_query_id] = list(local_step_id.values())

    return RoutePlan(steps=tuple(steps))


__all__ = [
    "ParameterBinding", "PlanStep", "RoutePlan", "RouteOutcome", "RouteResult",
    "SubQueryRoute", "assemble_plan",
]
