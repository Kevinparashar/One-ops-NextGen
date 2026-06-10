"""UC-8 service-catalog approval workflow — feature flag, group resolution, gate.

Phase-1 scaffolding. EVERYTHING in this module is INERT until the feature flag
`UC08_APPROVAL_ENABLED` is turned on (default OFF). With the flag off,
`create_service_request` behaves exactly as it does today — fulfil immediately,
no approval gate. See `APPROVAL_IMPLEMENTATION_PLAN.md` (this folder) for the
ordered, gated build plan and `docs/design/uc08-approval-workflow.md` for the
design.

Steps landed so far:
  Step 0 — the feature flag (`approval_enabled`).
  Step 2 — owning-group resolution: `owner_group` (GRP-*) -> the real people who
           staff it, via a SINGLE JOIN of `itsm.group_role_map` (the owner_group
           -> role/department bridge, loaded config-as-code from
           `data/itsm/group_role_map.json`) with `itsm.sys_user`.
  Step 3 — `resolve_manager` (the manager_of_requester resolver).
  Step 4 — `match_policy` / `load_policies` (the approval matrix decision table).
The create-path gate arrives in a later step and is guarded by
`approval_enabled()`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from oneops.config import _parse_flag
from oneops.observability import get_logger, get_tracer, increment
from oneops.use_cases.uc08_fulfillment import db as _db

if TYPE_CHECKING:
    import asyncpg

_tracer = get_tracer("oneops.uc08.approval")
_log = get_logger("oneops.uc08.approval")

# Single kill-switch for the whole approval feature. `UC08_*` prefix matches the
# sibling flags in this folder (UC08_RERANK_FLOOR, UC08_CATALOG_COSINE_FLOOR, …).
_FLAG = "UC08_APPROVAL_ENABLED"


def approval_enabled() -> bool:
    """True when the UC-8 second-party approval gate is active.

    Default ``False`` — the live catalog flow is byte-for-byte unchanged until
    this is flipped on (Step 12 of the implementation plan). Read on every call
    (not cached at import) so it can be flipped without a restart and toggled
    per-case in tests. Truthy/falsy parsing (1/true/yes/on …) is shared with the
    rest of the codebase via ``config._parse_flag``; unknown values fall back to
    ``False`` with a logged warning rather than raising.
    """
    return _parse_flag(_FLAG, default=False)


async def resolve_group_members(
    *, owner_group: str, tenant_id: str, conn: asyncpg.Connection,
) -> list[str]:
    """Active `sys_user` ids that staff `owner_group` within `tenant_id`.

    A SINGLE query JOINs `itsm.group_role_map` (the owner_group -> role/department
    bridge) with `itsm.sys_user` — one round-trip, no in-memory lookup, no added
    latency. Tenant-scoped, active users only, deterministic order. Returns ``[]``
    when the group has no mapping OR no active members; the gate then fails safe
    to the service-desk queue (never silently auto-approves), and the gap is
    surfaced via metric + warning (§2.7) — never lost.

    `group_role_map` is seeded config-as-code today; the Phase-2 HR/IdP sync
    populates the same table, so this resolver is the single, unchanged seam.
    """
    with _tracer.start_as_current_span(
        "uc08.approval.resolve_group_members",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.owner_group": owner_group,
        },
    ):
        rows = await conn.fetch(
            """
            SELECT u.user_id
              FROM itsm.group_role_map g
              JOIN itsm.sys_user u
                ON ((g.attribute = 'role'       AND u.role = g.value)
                 OR (g.attribute = 'department' AND u.department = g.value))
             WHERE g.owner_group = $1
               AND u.tenant_id = $2
               AND u.is_active
             ORDER BY u.user_id
            """,
            owner_group, tenant_id,
        )
        members = [r["user_id"] for r in rows]
        if not members:
            # Unmapped group OR mapped-but-no-active-members: both fail safe +
            # loud. The owner_group label identifies which team needs a mapping
            # (or sync) — never a silent empty approval.
            increment("ai.uc08.approval.group_unresolved",
                      owner_group=owner_group, reason="unresolved")
            _log.warning("uc08.approval.group_unresolved",
                         owner_group=owner_group, tenant_id=tenant_id)
        return members


async def resolve_manager(
    *, requester_id: str, tenant_id: str, conn: asyncpg.Connection,
) -> str | None:
    """The requester's ACTIVE manager (the `manager_of_requester` resolver).

    Returns the manager's `user_id`, or ``None`` when the requester has no
    manager set OR the manager is inactive — a fail-safe signal (the gate then
    escalates / routes to the service desk). Surfaced via metric + warning so it
    is never silent (§2.7). Tenant-scoped; the manager must be in the same tenant
    and active to count.
    """
    with _tracer.start_as_current_span(
        "uc08.approval.resolve_manager",
        attributes={
            "oneops.tenant_id": tenant_id,
            "uc08.requester_id": requester_id,
        },
    ):
        row = await conn.fetchrow(
            """
            SELECT m.user_id
              FROM itsm.sys_user u
              JOIN itsm.sys_user m
                ON m.tenant_id = u.tenant_id AND m.user_id = u.manager_id
             WHERE u.tenant_id = $1 AND u.user_id = $2 AND m.is_active
            """,
            tenant_id, requester_id,
        )
        if row is None:
            increment("ai.uc08.approval.manager_unresolved",
                      tenant_id=tenant_id, reason="no_active_manager")
            _log.warning("uc08.approval.manager_unresolved",
                         requester_id=requester_id, tenant_id=tenant_id)
            return None
        return row["user_id"]


def match_policy(
    attrs: dict[str, object], policies: list[dict],
) -> dict | None:
    """First policy whose `match` is a subset of the request attrs (PURE).

    `policies` must be ordered for evaluation (lowest priority first — see
    `load_policies`). A policy matches when EVERY key in its `match` equals the
    request's attribute (an empty `match` = the catch-all, matches anything).
    Deterministic: the first match in order wins. Returns ``None`` only when no
    policy matches — which cannot happen if the fail-safe catch-all row is
    present (the caller treats ``None`` as "route to service desk").
    """
    for p in policies:
        match = p.get("match") or {}
        if all(attrs.get(k) == v for k, v in match.items()):
            return p
    return None


async def load_policies(
    *, tenant_id: str, conn: asyncpg.Connection,
) -> list[dict]:
    """This tenant's approval matrix, ordered for top-down evaluation.

    Reads `itsm.approval_policy` (enabled rows only), priority ascending so the
    most-specific rule is tried first and the catch-all (priority 999) last.
    JSONB columns are decoded to plain dict/list for `match_policy`.
    """
    rows = await conn.fetch(
        """
        SELECT policy_id, priority, match, required, stages
          FROM itsm.approval_policy
         WHERE tenant_id = $1 AND enabled
         ORDER BY priority ASC, policy_id ASC
        """,
        tenant_id,
    )

    def _json(v: object) -> object:
        return json.loads(v) if isinstance(v, str) else v

    return [
        {
            "policy_id": r["policy_id"],
            "priority": r["priority"],
            "match": _json(r["match"]),
            "required": r["required"],
            "stages": _json(r["stages"]),
        }
        for r in rows
    ]


@dataclass(frozen=True)
class ApprovalDecision:
    """Outcome of evaluating the matrix + resolving approvers for one request.

    `resolved` is the gate's go/no-go: a decision is resolved when no approval is
    needed, OR approval is needed AND at least one approver was found. An
    unresolved decision (required but `approvers` empty) must NEVER be treated as
    auto-approved — the gate holds and surfaces it (§2.7).
    """
    required: bool
    policy_id: str
    approver_type: str          # resolver type: manager_of_requester|owning_group|service_desk
    approval_type: str          # itsm.approval enum (from matrix DATA): manager|catalog_owner|…
    approvers: tuple[str, ...]
    rule: str
    reason: str
    fell_back: bool

    @property
    def resolved(self) -> bool:
        return (not self.required) or bool(self.approvers)


def _service_desk_stage(policies: list[dict]) -> dict | None:
    """The catch-all policy's service-desk stage — its group AND approval_type are
    read from DATA (the matrix), never hardcoded. Used by the fail-safe path."""
    for p in policies:
        if (p.get("match") or {}) == {}:
            for st in p.get("stages") or []:
                ap = st.get("approver") or {}
                if ap.get("type") == "service_desk" and ap.get("group"):
                    return st
    return None


async def _resolve_stage(
    approver: dict, item: dict, requester_id: str, tenant_id: str,
    conn: asyncpg.Connection, group_resolver, manager_resolver,
) -> list[str]:
    """Approver user_ids for one stage, by resolver type (all targets are DATA)."""
    t = approver.get("type")
    if t == "owning_group":
        og = item.get("owner_group")
        return await group_resolver(owner_group=og, tenant_id=tenant_id, conn=conn) if og else []
    if t == "manager_of_requester":
        mgr = await manager_resolver(requester_id=requester_id, tenant_id=tenant_id, conn=conn)
        return [mgr] if mgr else []
    if t == "group":  # a named group from the matrix
        gid = approver.get("id") or approver.get("group")
        return await group_resolver(owner_group=gid, tenant_id=tenant_id, conn=conn) if gid else []
    if t == "user":  # a named user from the matrix
        uid = approver.get("id")
        return [str(uid)] if uid else []
    if t == "service_desk":
        g = approver.get("group")
        return await group_resolver(owner_group=g, tenant_id=tenant_id, conn=conn) if g else []
    return []


async def resolve_approvers(
    *, item: dict, requester_id: str, tenant_id: str, conn: asyncpg.Connection,
    policies: list[dict] | None = None,
    group_resolver=resolve_group_members,
    manager_resolver=resolve_manager,
) -> ApprovalDecision:
    """Decide whether a request needs approval and WHO can approve it.

    Pure composition over the matrix + resolvers (DB reads only — no writes, not
    wired to create). `item` carries `catalog_item_id` / `category` / `owner_group`.
    Steps: match the matrix → if `required:false` return no-approval → else resolve
    the stage's approvers → drop the requester (self-approval guard) → if that
    leaves nobody, FAIL SAFE to the service desk (group read from DATA) → if STILL
    nobody, return unresolved (gate must not auto-approve; §2.7).

    `group_resolver`/`manager_resolver` are injectable for unit tests; production
    uses the real DB resolvers.
    """
    if policies is None:
        policies = await load_policies(tenant_id=tenant_id, conn=conn)

    rule = match_policy(item, policies) or {
        "policy_id": "<no-match>", "required": True, "stages": [],
        "description": "no matching policy (catch-all missing)",
    }
    policy_id = str(rule.get("policy_id", "<no-match>"))
    reason = str(rule.get("description") or policy_id)

    if not rule.get("required", True):
        return ApprovalDecision(
            required=False, policy_id=policy_id, approver_type="none",
            approval_type="", approvers=(), rule="", reason=reason, fell_back=False)

    stages = rule.get("stages") or []
    # required but no stage defined → fail safe to the service-desk stage (DATA).
    stage = stages[0] if stages else (_service_desk_stage(policies) or {})
    approver = stage.get("approver") or {}
    approver_type = str(approver.get("type", ""))
    approval_type = str(stage.get("approval_type", ""))
    rule_name = str(stage.get("rule", "any_one"))
    block_self = bool(stage.get("block_self_approval", True))

    approvers = await _resolve_stage(
        approver, item, requester_id, tenant_id, conn, group_resolver, manager_resolver)
    if block_self:
        approvers = [a for a in approvers if a != requester_id]

    fell_back = False
    if not approvers and approver_type != "service_desk":
        sd_stage = _service_desk_stage(policies)
        if sd_stage:
            fell_back = True
            approver_type = "service_desk"
            approval_type = str(sd_stage.get("approval_type", approval_type))
            sd_group = (sd_stage.get("approver") or {}).get("group")
            sd = await group_resolver(owner_group=sd_group, tenant_id=tenant_id, conn=conn)
            approvers = [a for a in sd if not (block_self and a == requester_id)]

    decision = ApprovalDecision(
        required=True, policy_id=policy_id, approver_type=approver_type,
        approval_type=approval_type, approvers=tuple(approvers), rule=rule_name,
        reason=reason, fell_back=fell_back)

    if not decision.approvers:
        # Required approval but NOBODY can approve (empty roster everywhere) —
        # never auto-approve (§2.7): surface it; the gate holds.
        increment("ai.uc08.approval.unresolved_approvers",
                  policy_id=policy_id, approver_type=approver_type)
        _log.warning("uc08.approval.no_approvers", policy_id=policy_id,
                     approver_type=approver_type, tenant_id=tenant_id,
                     requester_id=requester_id)
    return decision


@dataclass(frozen=True)
class DecisionOutcome:
    """Result of the non-chat approve/reject action (Step 7′)."""
    ok: bool
    ritm_id: str | None
    state: str                 # approved | rejected | error | already
    should_dispatch: bool      # True → caller releases the held fulfilment
    message: str


async def decide_approval(
    *, approval_id: str, decision: str, decided_by: str, tenant_id: str,
    comment: str | None = None, conn: asyncpg.Connection,
) -> DecisionOutcome:
    """Approve or reject ONE parked approval — the NON-chat "IT team handles it
    on the request" action (the chat agent never calls this; an endpoint/portal
    does). `any_one` semantics: the first approve releases the RITM (sibling
    pending rows are withdrawn); a reject stops it.

    Guards: validates the actor is the assigned approver (`requested_from`);
    idempotent on an already-decided row; transactional so a partial failure
    leaves the parked state intact (never a half-release). On approve it returns
    `should_dispatch=True` — the caller dispatches the fulfilment the gate held.
    """
    if decision not in ("approved", "rejected"):
        return DecisionOutcome(False, None, "error", False,
                               "decision must be 'approved' or 'rejected'")
    with _tracer.start_as_current_span(
        "uc08.approval.decide",
        attributes={"oneops.tenant_id": tenant_id, "uc08.approval_id": approval_id,
                    "uc08.decision": decision},
    ):
        appr = await _db.get_approval(
            tenant_id=tenant_id, approval_id=approval_id, conn=conn)
        if appr is None:
            return DecisionOutcome(False, None, "error", False, "approval not found")
        if appr["state"] != "pending":
            # already decided/withdrawn — idempotent no-op
            return DecisionOutcome(True, appr["ritm_id"], appr["state"], False,
                                   "this approval was already decided")
        if decided_by != appr["requested_from"]:
            increment("ai.uc08.approval.decide.denied", reason="not_approver")
            return DecisionOutcome(False, appr["ritm_id"], "error", False,
                                   "you are not the assigned approver for this request")

        approved = decision == "approved"
        async with conn.transaction():
            ritm_id = await _db.update_approval_decision(
                tenant_id=tenant_id, approval_id=approval_id, decision=decision,
                decided_by=decided_by, comment=comment, conn=conn)
            if ritm_id is None:  # lost a race — already decided
                return DecisionOutcome(True, appr["ritm_id"], "already", False,
                                       "this approval was already decided")
            if approved:
                await _db.withdraw_other_pending_approvals(
                    tenant_id=tenant_id, ritm_id=ritm_id,
                    keep_approval_id=approval_id, conn=conn)
            # `apply_approval_outcome` returns the parent request_id on transition.
            request_id = await _db.apply_approval_outcome(
                tenant_id=tenant_id, ritm_id=ritm_id, approved=approved, conn=conn)
            transitioned = request_id is not None
            if request_id is not None:
                # Keep the customer-facing SR in sync (what UC-1 / TRACK reads):
                #   approve → approved / fulfillment   ·   reject → rejected / closed
                await _db.set_request_lifecycle(
                    tenant_id=tenant_id, request_id=request_id,
                    status="approved" if approved else "rejected",
                    stage="fulfillment" if approved else "closed", conn=conn)

        increment("ai.uc08.approval.decided", decision=decision)
        _log.info("uc08.approval.decided", tenant_id=tenant_id, ritm_id=ritm_id,
                  approval_id=approval_id, decision=decision, decided_by=decided_by)
        return DecisionOutcome(
            ok=True, ritm_id=ritm_id, state=decision,
            should_dispatch=(approved and transitioned),
            message=(f"Approved — fulfilment for {ritm_id} will start."
                     if approved else
                     f"Rejected — {ritm_id} will not be fulfilled."))
