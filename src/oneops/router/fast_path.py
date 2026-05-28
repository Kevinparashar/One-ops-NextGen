"""Fast-path dispatcher — one generalised entry serving every opted-in UC.

Moveworks "deep-link" / Salesforce "quick action" pattern: when the UI
already knows the user's intent + entity (because they pressed a button),
the request skips routing/disambiguation and goes directly to the named UC.
Every safety stage (policy, authz_recheck, hooks, persist) still runs — the
ONLY thing the fast-path skips is the "figure out what they want" step.

Designed for 1000 UCs from day one ([[feedback_poc5mw_design_for_1000_ucs_from_day_1]]):

  * **Registry-declarative.** A UC opts in by declaring `fast_path` on its
    `AgentRecord`. Adding the Nth fast-path UC is a JSON edit, zero code.
  * **One dispatcher, all UCs.** This module is registry-driven; there is
    no per-UC branch. The same code that serves UC-1 summarization today
    serves sentiment, similar-tickets, root-cause-analysis tomorrow.
  * **Schema-validated input.** Each UC declares its input fields; the
    dispatcher refuses any call that misses a required field or sends an
    unknown one. No free-form passthrough.
  * **Single-step plan output.** The dispatcher returns a one-step plan the
    executor consumes via its direct-plan entry point — exactly the same
    plan shape the router would produce for a single-intent message.
  * **Loud, typed errors.** Unknown UC, disabled UC, missing input, unknown
    field — each is a typed `FastPathError` with a non-leaky message.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from oneops.errors import OneOpsError
from oneops.observability import get_logger, get_tracer
from oneops.registry.models import FastPathInputField, FastPathSpec
from oneops.registry.service import RegistryService
from oneops.router.plan import PlanStep, RoutePlan

_log = get_logger("oneops.router.fast_path")
_tracer = get_tracer("oneops.router.fast_path")


class FastPathError(OneOpsError):
    """The dispatcher refused a fast-path call. Always typed; never silent."""

    code = "FAST_PATH_REFUSED"


@dataclass(frozen=True)
class FastPathRequest:
    """A structured fast-path call. `inputs` is the caller-supplied field map
    (validated against the UC's declared schema)."""

    uc_id: str
    inputs: Mapping[str, Any]


@dataclass(frozen=True)
class FastPathDispatchResult:
    """The dispatcher's output — a single-step plan + the validated
    parameters the executor will hand to the tool. Both are produced
    together so the executor never re-parses inputs."""

    plan: RoutePlan
    parameters: Mapping[str, Any]


# ── input coercion (no static catalogues — one rule per declared type) ──


def _coerce(value: Any, *, field: FastPathInputField) -> Any:
    """Coerce a caller-supplied value to the declared field type.

    The set of supported types matches what registries already validate at
    tool boundaries (`str`, `int`, `bool`, `float`). Unknown types raise so
    the dispatcher is a strict gate, not a permissive parser."""
    declared = field.type
    if value is None:
        return None
    if declared == "str":
        return str(value)
    if declared == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise FastPathError(
                f"field {field.name!r} expects an int, got {type(value).__name__}",
                cause=exc) from exc
    if declared == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise FastPathError(
                f"field {field.name!r} expects a float, got {type(value).__name__}",
                cause=exc) from exc
    if declared == "bool":
        if isinstance(value, bool):
            return value
        # Accept truthy/falsy textual forms commonly arriving from URL params.
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "1", "yes"}:
                return True
            if v in {"false", "0", "no"}:
                return False
        raise FastPathError(
            f"field {field.name!r} expects a bool, got {value!r}")
    # An unknown declared type is a registry bug (caught at integrity check
    # in production); fail loud rather than pass-through a free-form value.
    raise FastPathError(
        f"field {field.name!r} declares unsupported type {declared!r}; "
        f"supported: str|int|bool|float")


# ── Field derivation (registry-data-driven) ─────────────────────────────
#
# A `FastPathInputField` may declare `auto_derive_from = "<other_field>"`.
# Before missing-field validation, the dispatcher tries each declared
# derivation. Each rule is a small pure function — registry data points at
# the rule by `(target_field, source_field)`; the function maps a source
# value to the target value. Today we ship one rule (`service_id` ⇐ ticket
# prefix); adding more rules is a one-line entry in this table, not a per-UC
# code branch.


def _derive_service_id_from_ticket(source_value: Any) -> str | None:
    """`service_id` ⇐ first valid ITSM prefix in the provided id token.
    Uses the platform `EntityIdNormalizer` (same data the chat-path uses);
    unknown prefix or malformed body returns `None` (the field is then
    treated as missing and the loud "requires fields" error fires)."""
    if not source_value:
        return None
    # Lazy import keeps the dispatcher's import surface small and cold-start
    # friendly (see scale concern #21).
    from oneops.router.entity_id import EntityIdNormalizer
    normalizer = EntityIdNormalizer.from_registry_file()
    result = normalizer.normalize(str(source_value))
    if result.entity is None:
        return None
    return result.entity.service_id


# (target_field, source_field) -> derivation fn (source_value → target_value)
_DERIVATIONS: dict[tuple[str, str], Any] = {
    ("service_id", "ticket_id"): _derive_service_id_from_ticket,
}


def _derive_field(*, target: str, source_field: str, source_value: Any) -> Any:
    fn = _DERIVATIONS.get((target, source_field))
    if fn is None:
        # An unknown derivation is a registry config bug. Loud, never silent.
        raise FastPathError(
            f"no derivation rule for ({target!r} ⇐ {source_field!r}); "
            f"either remove auto_derive_from from the registry or register "
            f"a derivation in fast_path._DERIVATIONS")
    return fn(source_value)


# ── Dispatcher ──────────────────────────────────────────────────────────


class FastPathDispatcher:
    """Generalised fast-path entry. Construct once per process, share."""

    def __init__(self, registry: RegistryService) -> None:
        self._registry = registry

    def is_eligible(self, uc_id: str) -> bool:
        """Cheap pre-check used by routers, UIs, and SDK schema emitters. A
        UC is fast-path-eligible iff it is active + declares an enabled
        `fast_path` block."""
        agent = self._registry.agents.get_optional(uc_id)
        if agent is None or agent.status.value != "active":
            return False
        return agent.fast_path is not None and agent.fast_path.enabled

    def describe(self, uc_id: str) -> FastPathSpec | None:
        """Return the UC's fast-path spec for SDK / UI introspection. `None`
        if the UC is not eligible — never raises (this is a discovery API)."""
        agent = self._registry.agents.get_optional(uc_id)
        if agent is None or agent.fast_path is None or not agent.fast_path.enabled:
            return None
        return agent.fast_path

    def dispatch(self, request: FastPathRequest) -> FastPathDispatchResult:
        """Validate `request` against the UC's declared fast-path spec and
        return a single-step plan + the coerced parameters.

        Failure modes (all `FastPathError`):
          * unknown UC, retired UC, draft UC
          * UC has no `fast_path` block, or it is disabled
          * a required input field is missing
          * an unknown field was supplied (no silent pass-through)
          * a field value cannot be coerced to the declared type
        """
        uc_id = (request.uc_id or "").strip()
        if not uc_id:
            raise FastPathError("uc_id is required")

        with _tracer.start_as_current_span(
            "router.fast_path.dispatch",
            attributes={"oneops.uc_id": uc_id},
        ) as span:
            agent = self._registry.agents.get_optional(uc_id)
            if agent is None:
                raise FastPathError(f"unknown use case {uc_id!r}")
            if agent.status.value != "active":
                raise FastPathError(
                    f"use case {uc_id!r} is not active "
                    f"(status={agent.status.value})")
            spec = agent.fast_path
            if spec is None or not spec.enabled:
                raise FastPathError(
                    f"use case {uc_id!r} does not expose a fast-path entry")

            # Validate input field set: every required field is present, no
            # unknown fields are passed through.
            declared_names = {f.name for f in spec.input_fields}
            supplied_names = set(request.inputs.keys())
            unknown = supplied_names - declared_names
            if unknown:
                raise FastPathError(
                    f"use case {uc_id!r} received unknown fast-path "
                    f"fields: {sorted(unknown)}")

            params: dict[str, Any] = {}
            missing: list[str] = []
            for field in spec.input_fields:
                value = request.inputs.get(field.name)
                # Empty-string and None both mean "caller did not supply it".
                # Before declaring it missing, attempt registry-data-declared
                # derivation (e.g. service_id ⇐ ticket-id prefix).
                if value is None or (isinstance(value, str) and not value.strip()):
                    if field.auto_derive_from:
                        derived = _derive_field(
                            target=field.name,
                            source_field=field.auto_derive_from,
                            source_value=request.inputs.get(field.auto_derive_from))
                        if derived is not None:
                            params[field.name] = _coerce(derived, field=field)
                            continue
                    if field.required:
                        missing.append(field.name)
                    continue
                params[field.name] = _coerce(value, field=field)
            if missing:
                raise FastPathError(
                    f"use case {uc_id!r} fast-path requires fields: "
                    f"{sorted(missing)}")

            # Build the one-step plan. The executor's direct-plan entry will
            # run load_session → policy → authz_recheck → handler → persist
            # exactly as if a router had produced the same plan.
            step = PlanStep(
                step_id="step_1",
                agent_id=uc_id,
                # PlanStep carries (str, str) pairs — coerce ints/bools back
                # to their canonical string repr for the wire (the handler
                # re-parses against its own tool schema).
                parameters=tuple(
                    (k, str(v)) for k, v in params.items()
                ),
                depends_on=(),
            )
            plan = RoutePlan(steps=(step,))
            span.set_attribute("fast_path.fields", ",".join(sorted(params)))
            span.set_attribute("fast_path.primary_tool_id", spec.primary_tool_id)
            _log.info(
                "fast_path.dispatched",
                uc_id=uc_id, fields=sorted(params),
                primary_tool_id=spec.primary_tool_id,
            )
            return FastPathDispatchResult(plan=plan, parameters=params)


__all__ = [
    "FastPathError",
    "FastPathRequest",
    "FastPathDispatchResult",
    "FastPathDispatcher",
]
