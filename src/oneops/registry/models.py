"""Registry record schemas — the declarative specification layer.

Design influences (every model here justifies itself against one):

  * AgentScript — an agent is *data, not code*. `AgentRecord` fully describes
    an agent; the executor interprets it. Determinism dial (`determinism_level`)
    and lifecycle hooks (`hooks`) are AgentScript primitives. Swapping the
    runtime in year 3 does not touch these records.
  * Parlant — `activation_condition` is the declarative observation that the
    router evaluates deterministically; `excludes` are exclusion relationships;
    `depends_on` are dependency relationships.
  * Moveworks — descriptions are length-bounded (attention budget is finite);
    `compound_of` declares compound actions; structured schema *is* the
    contract (prompts are hopes, schemas are rules).

These are Pydantic models — validation is enforcement, not decoration. A record
that violates a rule cannot be constructed.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator

# Attention-budget cap (Moveworks): a capability description competes for finite
# LLM attention at routing time. Tight is mandatory, not aspirational.
MAX_DESCRIPTION_CHARS = 1200
_ID_PATTERN = r"^[a-z][a-z0-9_]{2,63}$"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


# ── Enums ────────────────────────────────────────────────────────────────


class DeterminismLevel(StrEnum):
    """AgentScript determinism dial. HIGH = every step gated by code/hooks,
    canned responses at compliance touchpoints, minimal LLM autonomy.
    LOW = the agent reasons more freely. The executor respects this."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RoutingShape(StrEnum):
    """The six routing shapes the router must be able to produce
    (see docs/product/BEHAVIOR_CORPUS.md §2)."""

    SINGLE = "single_agent"
    PARALLEL = "multi_agent_parallel"
    DEPENDENT = "multi_agent_dependent"
    AMBIGUOUS = "ambiguous"
    JOURNEY = "slot_filling_journey"
    GATED = "rbac_abac_gated"


class RecordStatus(StrEnum):
    """Lifecycle of a registry record version (DOC-03 + production-maturity §A.1).

    Transitions:
      DRAFT      → ACTIVE         (activate; promotes new version live)
      ACTIVE     → DEPRECATED     (deprecate; still callable, emits warning)
      DEPRECATED → RETIRED        (retire; removed from live pool)
      DRAFT      → RETIRED        (skip; never activated)
      ACTIVE     → RETIRED        (rollback fast-path; demote)

    Router behaviour:
      ACTIVE     — selectable
      DEPRECATED — selectable BUT `get()` emits a deprecation warning span
      DRAFT      — invisible to router (never selected)
      RETIRED    — invisible to router (404 on direct lookup)
    """

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class ExecutionTier(StrEnum):
    READ = "read"
    ACTION = "action"


class DataClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PII = "pii"


class ConditionOperator(StrEnum):
    ALL_OF = "all_of"
    ANY_OF = "any_of"
    LEAF = "leaf"


class ConditionSignal(StrEnum):
    """Deterministically-evaluable signals available to the router (stage 3).
    No free-form text matching — each signal is a set-membership or boolean
    check the router computes from request context."""

    INTENT_IN = "intent_in"               # classified intent ∈ values
    ENTITY_PRESENT = "entity_present"      # query references a recognised entity id
    ENTITY_SERVICE_IN = "entity_service_in"  # referenced entity's service ∈ values
    FOCUS_REQUIRED = "focus_required"      # an active session focus must exist
    TENANT_CAPABILITY = "tenant_capability"  # tenant has capability flag ∈ values
    ROLE_IN = "role_in"                    # caller role ∈ values


# ── Activation condition (Parlant observation) ───────────────────────────


class ActivationCondition(BaseModel):
    """A declarative, deterministically-evaluable activation predicate.

    Recursive boolean tree. A LEAF node carries one `signal` + `values`.
    An ALL_OF / ANY_OF node carries `clauses`. The router evaluates this in
    stage 3 with zero LLM involvement — that is the whole point (Moveworks:
    move logic out of the LLM; Parlant: conditions, not free-form matching)."""

    model_config = {"frozen": True}

    operator: ConditionOperator = ConditionOperator.LEAF
    signal: ConditionSignal | None = None
    values: tuple[str, ...] = ()
    negate: bool = False
    clauses: tuple[ActivationCondition, ...] = ()

    @model_validator(mode="after")
    def _check_shape(self) -> ActivationCondition:
        if self.operator is ConditionOperator.LEAF:
            if self.signal is None:
                raise ValueError("leaf condition requires a `signal`")
            if self.clauses:
                raise ValueError("leaf condition must not carry `clauses`")
            # Signals that are set-membership tests require operands.
            membership = {
                ConditionSignal.INTENT_IN,
                ConditionSignal.ENTITY_SERVICE_IN,
                ConditionSignal.TENANT_CAPABILITY,
                ConditionSignal.ROLE_IN,
            }
            if self.signal in membership and not self.values:
                raise ValueError(f"signal {self.signal.value} requires non-empty `values`")
        else:
            if not self.clauses:
                raise ValueError(f"{self.operator.value} condition requires `clauses`")
            if self.signal is not None or self.values:
                raise ValueError(f"{self.operator.value} condition must not carry leaf fields")
        return self


# ── Sub-records ──────────────────────────────────────────────────────────


class ToolRef(BaseModel):
    """An agent references tools by id+version — it never embeds tool code.
    One tool serves many agents (AgentScript: specification decoupled)."""

    model_config = {"frozen": True}
    tool_id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(default=1, ge=1)


class AbacTags(BaseModel):
    """Attribute tags consumed by the AuthZ service for ABAC decisions and by
    the router's stage-3 filter. RBAC `audience` is the coarse gate; the ABAC
    attributes refine it per tenant/resource."""

    model_config = {"frozen": True}
    service: tuple[str, ...] = ()                      # service ids this agent serves
    tier: ExecutionTier
    audience: tuple[str, ...] = ()                     # roles permitted (coarse RBAC)
    data_classification: DataClassification = DataClassification.INTERNAL


class Hooks(BaseModel):
    """AgentScript lifecycle hooks. Hook ids resolve to deterministic code
    the executor runs before / after invocation — auth re-checks, state
    validation, output redaction. Hooks run in code, never in prompts."""

    model_config = {"frozen": True}
    before_invocation: tuple[str, ...] = ()
    after_invocation: tuple[str, ...] = ()


class ExclusionRef(BaseModel):
    """Parlant exclusion relationship: when this agent and `agent_id` would
    both activate on one query, the higher `priority` wins. No silent
    fall-through — the conflict is resolved by declared data."""

    model_config = {"frozen": True}
    agent_id: str = Field(pattern=_ID_PATTERN)
    priority: int = Field(ge=0)


class JourneySlot(BaseModel):
    """One slot in a slot-filling journey (BEHAVIOR_CORPUS C4/C14/C17)."""

    model_config = {"frozen": True}
    slot_id: str = Field(pattern=_ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=300)
    required: bool = True
    validator_ref: str | None = None       # deterministic validator id
    fans_out_to: tuple[str, ...] = ()          # agent ids this slot triggers (onboarding fan-out)


class JourneySpec(BaseModel):
    """A journey is a determinism-HIGH agent shape: ordered slots, per-slot
    validation, confirmation gates, resumable as a durable draft."""

    model_config = {"frozen": True}
    slots: tuple[JourneySlot, ...]
    confirmation_required: bool = True
    resumable: bool = True

    @field_validator("slots")
    @classmethod
    def _non_empty_unique(cls, v: tuple[JourneySlot, ...]) -> tuple[JourneySlot, ...]:
        if not v:
            raise ValueError("a journey requires at least one slot")
        ids = [s.slot_id for s in v]
        if len(ids) != len(set(ids)):
            raise ValueError("journey slot_ids must be unique")
        return v


# ── Top-level records ────────────────────────────────────────────────────


class _VersionedRecord(BaseModel):
    """Shared shape for every versioned registry record."""

    id: str = Field(pattern=_ID_PATTERN)
    version: int = Field(default=1, ge=1)
    status: RecordStatus = RecordStatus.DRAFT
    owner: str = Field(min_length=1, max_length=120)
    created_at: str = Field(default_factory=_utc_now)
    updated_at: str = Field(default_factory=_utc_now)


class FastPathInputField(BaseModel):
    """One declared input on a fast-path UC entry. The dispatcher validates
    the caller's structured input against this schema before any handler
    runs — no free-form fields slip through.

    `auto_derive_from` lets the registry declare that this field's value can
    be inferred from another supplied field (data-driven derivation, no
    UC-specific code). Today: `service_id` ⇐ ticket-id prefix via the
    platform's `EntityIdNormalizer`. The dispatcher attempts derivation
    BEFORE missing-field validation, so the UI never has to ask the user for
    a value the system can compute deterministically."""

    model_config = {"frozen": True}
    name: str = Field(pattern=_ID_PATTERN)
    type: str = Field(min_length=1, max_length=32)       # str|int|bool|enum…
    required: bool = True
    description: str = Field(min_length=1, max_length=240)
    auto_derive_from: str | None = Field(default=None, max_length=64)


class FastPathSpec(BaseModel):
    """Declarative fast-path entry for an agent — Moveworks "deep-link" /
    Salesforce "quick action" pattern.

    A UC opts in by declaring this block; the platform `/fast/{uc_id}` ingress
    serves every opted-in UC through one dispatcher (no per-UC code). The
    fast-path skips ONLY routing/disambiguation — every safety stage
    (load_session, policy, authz_recheck, hooks, persist) runs unchanged.
    """

    model_config = {"frozen": True}
    enabled: bool = True
    # Caller-supplied tool id whose handler answers the fast-path call. Must
    # appear in the agent's `tool_refs` — enforced at integrity-check time so
    # a misdeclared fast-path can never reach production.
    primary_tool_id: str = Field(pattern=_ID_PATTERN)
    # Declared input fields the dispatcher validates. The order here is the
    # canonical order — UIs and SDKs render in this order.
    input_fields: tuple[FastPathInputField, ...] = Field(min_length=1)
    # Optional reference to a registered schema (`SchemaRecord.id`) that
    # describes the response shape — for SDK/codegen consumers. The dispatcher
    # does NOT validate output against it (the handler already produces the
    # canonical contract); this is documentation-as-data.
    response_schema_ref: str | None = None


class Skill(BaseModel):
    """A structured, self-describing capability declaration on an agent.

    This is the routing contract for the dynamic, scale-generic router: agents
    declare WHAT they do and WHEN they should (and should NOT) be chosen, as
    DATA — so the router can match a query against these cards instead of a
    hand-tuned taxonomy baked into a prompt. Adding a use case = adding its
    skill card; the router needs no code/prompt change (agents-as-data).

    Schema synthesizes the converged industry pattern: Anthropic Agent Skills
    (`name` + `description` that says *what* AND *when*), the A2A AgentCard
    `skills[]` (id/name/description/tags/examples), and Salesforce Agentforce
    subagent routing (match the query to name + description). Disambiguation
    knowledge lives in `use_when`/`not_when` — moved off the router prompt and
    onto the skill, which is what lets routing generalize toward 1000 UCs.

    ADDITIVE / NOT-YET-WIRED: declaring skills changes no routing behavior
    today. The retrieval + disambiguation stages consume these cards only once
    the dynamic router is enabled (flag-gated + eval-gated). See
    docs/architecture/agent-skills-spec.md.
    """

    model_config = {"frozen": True}
    id: str = Field(pattern=_ID_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    # Must state BOTH what the skill does AND when to use it (Anthropic rule).
    description: str = Field(min_length=1, max_length=800)
    # Positive routing signals — intents/phrasings this skill should win.
    use_when: tuple[str, ...] = ()
    # Disambiguation-as-data — intents this skill must NOT be chosen for (e.g.
    # the KB-vs-summary trap). This is knowledge currently hardcoded in the
    # disambiguation prompt's taxonomy; declaring it here is what moves routing
    # from prompt-tuned to data-driven.
    not_when: tuple[str, ...] = ()
    # Structured retrieval/filter boosts (e.g. "summarization", "read").
    tags: tuple[str, ...] = ()
    # Illustrative queries — applied as PRINCIPLE, never a string-match list.
    examples: tuple[str, ...] = ()
    # Contrastive negative exemplars — queries this skill must NOT win (what it
    # is NOT, complementing use_when). Card-boundary documentation, available as
    # a disambiguation signal. Never embedded for retrieval (a query must not
    # retrieve an agent by its negatives).
    negative_examples: tuple[str, ...] = ()


class AgentRecord(_VersionedRecord):
    """A use case, fully described as data. One per UC; ~1000 at full scale.

    The executor interprets this record — there is no per-agent code module.
    This is the AgentScript principle that lets the platform survive a
    runtime/framework swap over the 5-year horizon."""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    intent_family: str = Field(min_length=1, max_length=64)   # docs/BEHAVIOR_CORPUS §1
    # Structural chat-routing eligibility (defence-in-depth, not prompt hope).
    # False ⇒ the agent is NEVER a conversational-router candidate (e.g. uc05
    # triage is API/operator-only); the router drops it from the chat funnel
    # regardless of retrieval/activation. Default True keeps every existing
    # agent chat-eligible.
    chat_router_eligible: bool = True
    # Routing scope: itsm | itom[.subdomain]. Default keeps existing cards valid;
    # the skill-card contract requires NEW cards to declare it explicitly so an
    # ITOM agent can't silently default to 'itsm' and get mis-scoped at retrieval.
    domain: str = Field(default="itsm", min_length=1, max_length=64)
    routing_shape: RoutingShape
    # Every agent is a use case the router matches deterministically on this
    # condition. The conversational / out-of-scope / policy-boundary responder
    # is NOT an agent — it is a platform component in the routing layer, so it
    # never appears here and every registry agent always has a condition.
    activation_condition: ActivationCondition
    tool_refs: tuple[ToolRef, ...] = ()
    policy_refs: tuple[str, ...] = ()
    # Structured, self-describing capability cards for dynamic/data-driven
    # routing (see Skill). Default empty: existing agents parse unchanged and
    # routing behavior is untouched until the dynamic router is enabled.
    skills: tuple[Skill, ...] = ()
    abac_tags: AbacTags
    determinism_level: DeterminismLevel
    hooks: Hooks = Hooks()
    depends_on: tuple[str, ...] = ()           # agent ids — DAG edges
    excludes: tuple[ExclusionRef, ...] = ()
    compound_of: tuple[str, ...] = ()          # if set, this is a compound action
    journey: JourneySpec | None = None
    # Opt-in flag: when true, the orchestrator runs the TimeFilterExtractor
    # for any turn whose survivor set contains this agent, and passes the
    # extracted TimeFilter through the tool context. Default False — no LLM
    # cost for UCs that don't consume time scopes (UC-1 ID lookup, etc.).
    # Spec: docs/issues/.../TimeFilter-design.md (or §UC-2.6 spec).
    consumes_time_filter: bool = False
    # Opt-in flag: when true, the executor does NOT fire its generic upfront
    # action-approval interrupt for this agent's steps — the agent's handler
    # manages approval itself, conversationally, at the right point (e.g. UC-8
    # catalog: search → pick → fields → CONFIRM → create). Default False ⇒
    # every existing action agent keeps the generic upfront gate (golden tests
    # unchanged). Only a handler that calls interrupt_for_confirmation itself
    # before its mutation may set this — never a shortcut to skip approval.
    manages_own_approval: bool = False
    # Optional fast-path entry — UCs that opt in are served through the
    # generalised `/fast/{uc_id}` dispatcher (Moveworks deep-link). Absent
    # ⇒ the UC is chat-only.
    fast_path: FastPathSpec | None = None

    @model_validator(mode="after")
    def _cross_field_rules(self) -> AgentRecord:
        if self.id in self.depends_on:
            raise ValueError(f"agent {self.id} cannot depend on itself")
        if any(x.agent_id == self.id for x in self.excludes):
            raise ValueError(f"agent {self.id} cannot exclude itself")
        if self.id in self.compound_of:
            raise ValueError(f"compound agent {self.id} cannot contain itself")
        if self.compound_of and self.routing_shape is RoutingShape.SINGLE:
            raise ValueError("a compound action cannot have routing_shape=single_agent")
        self._validate_journey()
        # Action-tier agents must declare an auth re-check hook — defence in
        # depth (docs/architecture/ARCHITECTURE.md §9: authz at every boundary).
        if self.abac_tags.tier is ExecutionTier.ACTION and not self.hooks.before_invocation:
            raise ValueError(
                f"action-tier agent {self.id} must declare a before_invocation hook "
                "(auth re-check) — see docs/architecture/ARCHITECTURE.md §9"
            )
        self._validate_fast_path()
        return self

    def _validate_journey(self) -> None:
        """Journey ⇔ routing_shape=JOURNEY consistency; a journey is a gated
        multi-turn flow so it can never be determinism_level=low."""
        if self.journey is not None:
            if self.routing_shape is not RoutingShape.JOURNEY:
                raise ValueError("journey set → routing_shape must be slot_filling_journey")
            if self.determinism_level is DeterminismLevel.LOW:
                raise ValueError("a journey agent cannot be determinism_level=low")
        if self.routing_shape is RoutingShape.JOURNEY and self.journey is None:
            raise ValueError("routing_shape=slot_filling_journey requires a `journey` spec")

    def _validate_fast_path(self) -> None:
        """Fast-path declaration sanity: primary_tool_id is one the agent owns
        and input_field names are unique. The dispatcher trusts the registry,
        so a bad fast_path must never load."""
        if self.fast_path is not None and self.fast_path.enabled:
            tool_ids = {ref.tool_id for ref in self.tool_refs}
            if self.fast_path.primary_tool_id not in tool_ids:
                raise ValueError(
                    f"agent {self.id} fast_path.primary_tool_id="
                    f"{self.fast_path.primary_tool_id!r} is not in tool_refs "
                    f"({sorted(tool_ids)}) — a fast-path UC must reference a "
                    f"tool the agent already owns")
            field_names = [f.name for f in self.fast_path.input_fields]
            if len(field_names) != len(set(field_names)):
                raise ValueError(
                    f"agent {self.id} fast_path input_fields have duplicate "
                    f"names: {field_names}")


class ToolParameter(BaseModel):
    model_config = {"frozen": True}
    name: str = Field(pattern=_ID_PATTERN)
    type: str = Field(min_length=1, max_length=32)        # str|int|bool|... — codec-validated
    required: bool = True
    description: str = Field(min_length=1, max_length=240)
    data_classification: DataClassification = DataClassification.INTERNAL


class ToolRecord(_VersionedRecord):
    """A tool, registered separately and referenced by id+version. Carries its
    own activation condition (Parlant: a tool enters the prompt only when its
    condition holds — protects attention budget, prevents false invocations)."""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    activation_condition: ActivationCondition
    handler_ref: str = Field(min_length=1, max_length=200)   # module:fn or FaaS handler id
    execution_type: ExecutionTier
    parameters: tuple[ToolParameter, ...] = ()
    timeout_ms: int = Field(default=30_000, ge=100, le=600_000)
    idempotent: bool = True
    requires_scopes: tuple[str, ...] = ()
    # Bindable output surface (data-flow binding contract): the top-level field
    # names this tool's handler emits in its result `output`. A downstream
    # data-flow binding may only target a declared field — the planner is
    # validated against this so it can't bind to a field the producer doesn't
    # emit. Empty ⇒ undeclared (binding validation is skipped for this tool).
    output_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _action_rules(self) -> ToolRecord:
        # An action tool that is not idempotent is a double-execution hazard
        # under NATS at-least-once re-delivery (docs/architecture/ARCHITECTURE.md §8).
        if self.execution_type is ExecutionTier.ACTION and not self.idempotent:
            raise ValueError(
                f"action tool {self.id} must be idempotent — re-delivery is "
                "guaranteed; declare an idempotency strategy or mark it read"
            )
        return self


class SchemaRecord(_VersionedRecord):
    """A wire/disk message-schema version (ADR-0001 codec). The schema registry
    tracks every version and its deprecation window so consumers can honour
    the N / N-1 compatibility rule."""

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    format: str = Field(pattern=r"^(protobuf|json)$")
    location: str = Field(min_length=1, max_length=300)      # .proto path or json-schema path
    deprecates_version: int | None = Field(default=None, ge=1)
    deprecation_window_days: int = Field(default=90, ge=0, le=730)

    @model_validator(mode="after")
    def _deprecation_rules(self) -> SchemaRecord:
        if self.deprecates_version is not None and self.deprecates_version >= self.version:
            raise ValueError("deprecates_version must be older than this version")
        return self


__all__ = [
    "MAX_DESCRIPTION_CHARS",
    "DeterminismLevel",
    "RoutingShape",
    "RecordStatus",
    "ExecutionTier",
    "DataClassification",
    "ConditionOperator",
    "ConditionSignal",
    "ActivationCondition",
    "ToolRef",
    "AbacTags",
    "Hooks",
    "ExclusionRef",
    "JourneySlot",
    "JourneySpec",
    "FastPathInputField",
    "FastPathSpec",
    "Skill",
    "AgentRecord",
    "ToolParameter",
    "ToolRecord",
    "SchemaRecord",
]
