"""Registry-resolved tool handlers for UC-8.

The strings in `registries/v2/tools/uc08_fulfillment/*.json` (`handler_ref`)
point at the callables in this module. The dispatcher/runner resolves them
at boot.

Production-grade properties for every handler:
  • Pydantic input validation at the boundary (rule §2.7).
  • Tenant binding from the context dict (never from request arguments).
  • Structured OTel span.
  • Typed return — never bare dict.

Tool handlers for **active orchestration** (create_directory_account,
provision_email_mailbox, etc.) land in Phase 6 alongside the LangGraph
graph. This module hosts the **read-only** and **entry-point** tools we
can ship and test in Phase 5.

Phase 5 ships:
  • fulfill_request          — the catalog-fulfillment entry point
  • get_fulfillment_status   — status-query (chat + UI)
  • load_catalog_template    — read-only catalog lookup
  • check_request_duplicate  — duplicate-detection helper

Phase 6 adds (alongside the orchestrator):
  • decompose_fulfillment_request — LLM-driven plan (8.8 fallback)
  • create_directory_account, provision_email_mailbox, …
  • request_human_approval, rollback_completed_steps, …
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC
from typing import Any

import structlog
from opentelemetry import trace

from oneops.use_cases.uc08_fulfillment import catalog_search as _catalog_search
from oneops.use_cases.uc08_fulfillment import core as _core
from oneops.use_cases.uc08_fulfillment import db as _db
from oneops.use_cases.uc08_fulfillment import nats_dispatcher as _nats_dispatcher
from oneops.use_cases.uc08_fulfillment.contracts import (
    FulfillmentRequest,
    TriggerType,
)
from oneops.use_cases.uc08_fulfillment.errors import (
    CatalogItemNotFoundError,
    DuplicateRequestError,
    RequesterNotFoundError,
    RequestItemNotFoundError,
    RequestNotFoundError,
)

# Telemetry/HTTP literals → constants (sonar S1192).
_ONEOPS_TENANT_ID = "oneops.tenant_id"
_BAD_REQUEST = "UC08_BAD_REQUEST"

_log = structlog.get_logger("oneops.uc08.tools")
_tracer = trace.get_tracer("oneops.uc08.tools")

# Optional connection-provider injection — the API factory sets this so
# tests can override per-test (matches UC-5 pattern in `set_*` factories).
_connection_provider: _db.ConnectionProvider | None = None

# Embedding gateway (single egress, rule §2.5) — set at boot. Required by
# get_service_request_list (catalog semantic search). None ⇒ the tool
# degrades to a typed "search unavailable" result instead of raising.
_gateway: Any = None

# NATS client — set at boot. create_service_request publishes the
# fulfilment-execute event so the UC8FulfillmentAgent worker runs the task
# DAG. None ⇒ the SR + RITM are persisted but execution is not dispatched
# (the tool says so in its result; no silent drop, rule §2.7).
_nats_client: Any = None


def set_connection_provider(cp: _db.ConnectionProvider | None) -> None:
    """Wire a connection provider. None ⇒ default (per-call asyncpg.connect)."""
    global _connection_provider
    _connection_provider = cp


def set_gateway(gateway: Any) -> None:
    """Wire the embedding gateway used by get_service_request_list. MUST be an
    `oneops.llm.gateway.LlmGateway` (single egress, rule §2.5)."""
    global _gateway
    _gateway = gateway


def set_nats_client(nats: Any) -> None:
    """Wire the NATS client create_service_request uses to dispatch execution."""
    global _nats_client
    _nats_client = nats


def _principal_from_context(context: dict[str, Any]) -> tuple[str, str, str]:
    """Extract (tenant_id, user_id, role) from the tool-runner context.
    Mirrors UC-2 / UC-5 boundary discipline."""
    tenant_id = str(context.get("tenant_id") or "").strip()
    user_id = str(context.get("user_id") or "").strip()
    role = str(context.get("role") or "").strip()
    if not tenant_id:
        raise ValueError("uc08 tool: missing tenant_id in context")
    if not user_id:
        raise ValueError("uc08 tool: missing user_id in context")
    return tenant_id, user_id, role


# ════════════════════════════════════════════════════════════════════════════
#  Chat catalog flow — the 4 runbook tools (Playbook 3 "New service request").
#
#  The LLM sees exactly these four; the 14 fulfilment adapters are the
#  deterministic DAG engine BEHIND create_service_request, not LLM-pickable
#  tools. Tiers: list/fields are READ, create/update are ACTION.
#
#    1. get_service_request_list   (read)   — semantic catalog search
#    2. get_service_request_fields (read)   — intake form schema
#    3. create_service_request     (action) — open SR → fulfil → dispatch
#    4. update_service_request     (action) — merge field changes into an SR
#
#  Confirmation lives in the agent/flow layer (Playbook 3 step 5 → 6), not in
#  these handlers — they stay pure so they're independently callable + testable.
# ════════════════════════════════════════════════════════════════════════════


async def get_service_request_list(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Semantic catalog search (runbook Playbook 3 step 1).

    Arguments:
      service_catalogs — list of 2–4 keyword variations of the ask
                         (e.g. ["VPN", "remote access", "VPN setup"]), OR
      query            — a single free-text query (fallback).

    Only items that carry a request FORM are returned (requestable =
    has-form rule). Above-floor matches only — an empty list tells the
    caller to offer the incident path instead.
    """
    tenant_id, _, role = _principal_from_context(context)
    raw = arguments.get("service_catalogs")
    if raw is None:
        raw = arguments.get("query") or ""
    keywords = (
        [raw.strip()] if isinstance(raw, str) and raw.strip()
        else [str(k).strip() for k in (raw or []) if str(k).strip()]
    )
    if not keywords:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": "service_catalogs (keywords) or query is required",
                "display_text": "What would you like to request from IT?"}
    if _gateway is None:
        return {"ok": False, "error_code": "UC08_SEARCH_UNAVAILABLE",
                "error": "embedding gateway not wired",
                "display_text": "Catalog search is temporarily unavailable."}
    # Our catalog backend is embedding-based (not keyword), so the keyword
    # variations join into ONE well-formed query — semantically stronger and
    # one embed call, not N.
    query = " ".join(keywords)
    with _tracer.start_as_current_span(
        "uc08.tool.get_service_request_list",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "uc08.keywords": ",".join(keywords)},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            result = await _catalog_search.find_closest_catalog_items(
                tenant_id=tenant_id, sr_title=query, sr_description="",
                user_roles=[role] if role else None,
                gateway=_gateway, conn=conn, require_form=True,
            )
        except _catalog_search.CatalogSearchError as exc:
            _log.warning("uc08.tool.catalog_search_failed",
                         tenant_id=tenant_id, error=str(exc))
            return {"ok": False, "error_code": "UC08_SEARCH_FAILED",
                    "error": str(exc),
                    "display_text": "I couldn't search the catalog just now."}
        finally:
            await conn.close()
        # Show the top-K for the USER to choose from when the search is
        # relevant (the best match cleared the floor). We return the whole
        # top-K — not just the items over the floor — so the user always has a
        # few candidates to pick from; they are the relevance judge. A search
        # whose best match is below the floor is a genuine no-match.
        relevant = bool(result.matches) and result.matches[0].above_floor
        matches = [
            {"catalog_id": m.catalog_item_id, "name": m.name,
             "description": m.description, "category": m.category,
             "score": round(m.cosine_score, 4)}
            for m in (result.matches if relevant else ())
        ]
        if matches:
            lines = "\n".join(
                f"{i}. {m['name']} — {m['description'][:80]}"
                for i, m in enumerate(matches, start=1))
            display = f"I found these catalog items:\n{lines}\nWhich one?"
        else:
            display = ("No matching catalog item. Would you like me to raise "
                       "an incident instead?")
        return {"ok": True, "matches": matches, "count": len(matches),
                "display_text": display}


async def get_service_request_fields(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Intake-form schema for a catalog item (runbook Playbook 3 step 3).

    Arguments: catalog_id — the catalog item id.
    Returns the form fields ([{field_name,label,type,required,options?}])
    and the list of required field names. The agent collects exactly these
    fields — no invented names.
    """
    tenant_id, _, _ = _principal_from_context(context)
    catalog_id = str(
        arguments.get("catalog_id")
        or arguments.get("catalog_item_id") or "").strip()
    if not catalog_id:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": "catalog_id is required"}
    with _tracer.start_as_current_span(
        "uc08.tool.get_service_request_fields",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "uc08.catalog_item_id": catalog_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                fields = await _db.load_request_fields(
                    tenant_id=tenant_id, catalog_item_id=catalog_id, conn=conn)
            except CatalogItemNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc),
                        "display_text": (
                            f"I don't recognise that catalog item "
                            f"({catalog_id}).")}
        finally:
            await conn.close()
        required = [f["field_name"] for f in fields
                    if f.get("required") and f.get("field_name")]
        return {"ok": True, "catalog_id": catalog_id, "fields": fields,
                "required": required, "field_count": len(fields)}


async def create_service_request(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Open a Service Request and start fulfilment (runbook Playbook 3 step 6).

    Arguments:
      catalog_id — catalog item id.
      fields     — collected form values; keys MUST match the schema's
                   field_names. Every REQUIRED field must be present.
      requested_for — user the request is for (default: caller).
      quantity, idempotency_key — optional.

    Flow: validate required fields → open itsm.request (the SR) → fulfil
    (RITM + task DAG persisted) → dispatch execution over NATS. The
    confirmation gate is the agent's job before this call.
    """
    tenant_id, user_id, _ = _principal_from_context(context)
    trace_id = context.get("trace_id")
    catalog_id = str(
        arguments.get("catalog_id")
        or arguments.get("catalog_item_id") or "").strip()
    fields = dict(arguments.get("fields") or arguments.get("variables") or {})
    requested_for = str(arguments.get("requested_for") or user_id).strip()
    quantity = int(arguments.get("quantity") or 1)
    idempotency_key = arguments.get("idempotency_key")
    if not catalog_id:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": "catalog_id is required"}

    with _tracer.start_as_current_span(
        "uc08.tool.create_service_request",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "oneops.user_id": user_id,
                    "uc08.catalog_item_id": catalog_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            # 1. Validate the form against the catalog schema (rule §2.7).
            try:
                schema = await _db.load_request_fields(
                    tenant_id=tenant_id, catalog_item_id=catalog_id, conn=conn)
                catalog_name = await conn.fetchval(
                    "SELECT name FROM itsm.catalog_item "
                    "WHERE tenant_id=$1 AND catalog_item_id=$2",
                    tenant_id, catalog_id) or catalog_id
            except CatalogItemNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc),
                        "display_text": (
                            f"I don't recognise that catalog item "
                            f"({catalog_id}).")}
            missing = [
                f["field_name"] for f in schema
                if f.get("required")
                and not str(fields.get(f["field_name"], "")).strip()
            ]
            if missing:
                return {"ok": False, "error_code": "UC08_MISSING_FIELDS",
                        "error": f"missing required fields: {missing}",
                        "missing_fields": missing,
                        "display_text": (
                            "Before I can submit this I still need: "
                            f"{', '.join(missing)}.")}
            # 2. Open the parent SR.
            try:
                request_id = await _db.insert_request(
                    tenant_id=tenant_id, title=f"{catalog_name} request",
                    catalog_item_id=catalog_id, requested_for=requested_for,
                    requested_by=user_id, category=None, fields=fields,
                    conn=conn)
            except RequesterNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc),
                        "display_text": (
                            f"I couldn't submit this — “{requested_for}” isn't a "
                            "recognised user in this tenant. Pick a valid "
                            "requester and try again.")}
        finally:
            await conn.close()

    # 3. Fulfil — core opens its own connection via the provider.
    try:
        req = FulfillmentRequest(
            tenant_id=tenant_id, request_id=request_id,
            catalog_item_id=catalog_id, variables=fields,
            requested_for=requested_for, opened_by=user_id,
            quantity=quantity, idempotency_key=idempotency_key,
            trigger_type=TriggerType.CHAT,
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": f"invalid request: {exc}",
                "display_text": f"I couldn't submit that: {exc}"}
    try:
        outcome = await _core.fulfill_request(
            req, connection_provider=_connection_provider,
            trace_id=trace_id, actor=user_id)
    except DuplicateRequestError as exc:
        return {"ok": False, "error_code": exc.code, "error": str(exc),
                "request_id": request_id,
                "display_text": (
                    "An open request for this item already exists. "
                    f"{exc}")}
    except CatalogItemNotFoundError as exc:
        return {"ok": False, "error_code": exc.code, "error": str(exc),
                "display_text": (
                    f"I don't recognise that catalog item ({catalog_id}).")}

    # 4. Dispatch execution (fire-and-forget). No silent drop (rule §2.7):
    # the result states whether the DAG was kicked off.
    dispatched = False
    if _nats_client is not None and outcome.ritm_id:
        try:
            await _nats_dispatcher.dispatch_execute(
                nats=_nats_client, tenant_id=tenant_id,
                ritm_id=outcome.ritm_id, trace_id=trace_id)
            dispatched = True
        except Exception as exc:                          # noqa: BLE001
            _log.warning("uc08.tool.dispatch_failed",
                         tenant_id=tenant_id, ritm_id=outcome.ritm_id,
                         error=str(exc))

    payload = outcome.model_dump(mode="json")
    payload["ok"] = True
    payload["request_id"] = request_id
    payload["dispatched"] = dispatched
    payload["display_text"] = (
        f"Done — service request {request_id} is submitted and fulfilment "
        f"{outcome.ritm_id} has started."
        if dispatched else
        f"Service request {request_id} is submitted (fulfilment "
        f"{outcome.ritm_id} is queued).")
    return payload


async def update_service_request(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Update fields on an existing Service Request (runbook Playbook 2 step 2).

    Arguments:
      request_id — the SR id.
      fields     — the changed field values to merge (shallow JSONB merge).
    """
    tenant_id, _, _ = _principal_from_context(context)
    request_id = str(arguments.get("request_id") or "").strip()
    fields = dict(arguments.get("fields") or {})
    if not request_id:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": "request_id is required"}
    if not fields:
        return {"ok": False, "error_code": _BAD_REQUEST,
                "error": "fields (the changes to apply) is required",
                "display_text": "What would you like to change on it?"}
    with _tracer.start_as_current_span(
        "uc08.tool.update_service_request",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "oneops.request_id": request_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                updated = await _db.update_request_fields(
                    tenant_id=tenant_id, request_id=request_id,
                    field_changes=fields, conn=conn)
            except RequestNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc),
                        "display_text": (
                            f"That service request id doesn't exist "
                            f"({request_id}).")}
        finally:
            await conn.close()
        updated["ok"] = True
        updated["display_text"] = (
            f"Updated {request_id}: {', '.join(sorted(fields.keys()))}.")
        return updated


# ════════════════════════════════════════════════════════════════════════════
#  CONDUCTOR — request_catalog_item (runbook Playbook 3, chat entry).
#
#  This system runs one tool per turn deterministically (no LLM tool-loop), so
#  the multi-step catalog flow is a single conductor that SEQUENCES the 4 tools
#  with the Conversational Interrupt Protocol for the user pauses:
#
#    search (get_service_request_list)            ── runbook step 1
#      → interrupt_for_selection  (pick an item)  ── runbook step 2
#    fields (get_service_request_fields)          ── runbook step 3
#      → interrupt_for_input      (collect form)  ── runbook step 4  (only schema fields)
#      → interrupt_for_confirmation (review)      ── runbook step 5  (the SOLE approval gate)
#    create (create_service_request)              ── runbook step 6
#      → return the SR id                         ── runbook step 7
#
#  Fallback (no catalog match): graceful decline — offer to flag IT / rephrase,
#  NEVER fabricate an incident (incident-creation is a separate, unbuilt
#  capability). The agent carries `manages_own_approval: true` so the executor
#  skips its generic upfront approval — the step-5 confirmation here is the one
#  and only gate (runbook has exactly one confirmation).
#
#  Replay note: LangGraph re-executes this handler from the top on each resume,
#  replaying resolved interrupts from cache — so search()/get_fields() re-run
#  per resume (a bounded, idempotent cost). Optimisation (session memoisation)
#  is deferred; correctness + runbook fidelity first.
# ════════════════════════════════════════════════════════════════════════════


def _unwrap(answer: Any, key: str) -> Any:
    """Interrupt resume answers arrive as {key: value} from the frontend
    (e.g. {"selected": {...}}, {"fields": {...}}, {"confirmed": true}). Be
    lenient: accept the wrapped or the already-unwrapped shape."""
    if isinstance(answer, dict) and key in answer:
        return answer[key]
    return answer


def _query_from(arguments: dict[str, Any], context: dict[str, Any]) -> str:
    """The catalog query: the router threads the user text as query/
    user_message; fall back to the latest user turn in history."""
    q = str(arguments.get("query") or arguments.get("user_message") or "").strip()
    if q:
        return q
    for turn in reversed(context.get("conversation_history") or []):
        if (turn or {}).get("role") == "user" and turn.get("content"):
            return str(turn["content"]).strip()
    return ""


# Per-flow memo. LangGraph replays this whole handler from the top on every
# interrupt resume. Memoising the (idempotent) catalog search + schema + the AI
# field draft per session keeps replay cheap: no re-embedding the query,
# re-reading the form, or re-calling the LLM on each turn. Bounded +
# process-local; a memo miss (e.g. another worker) just re-fetches.
_FLOW_MEMO_CAP = 512
_search_memo: dict[str, dict[str, Any]] = {}
_fields_memo: dict[str, dict[str, Any]] = {}
_draft_memo: dict[str, dict[str, Any]] = {}

_EXTRACT_MODEL = os.environ.get("UC08_FIELD_EXTRACT_MODEL", "gpt-4o-mini")
_EXTRACT_TIMEOUT_S = float(os.environ.get("UC08_FIELD_EXTRACT_TIMEOUT_S", "30"))
# Max times the intake form is re-shown to fill missing required fields before
# giving up (a guard against an infinite loop if the user keeps submitting blank).
_MAX_FIELD_ROUNDS = int(os.environ.get("UC08_MAX_FIELD_ROUNDS", "4"))

_DRAFT_FIELDS_INSTRUCTION = """
You are the intake assistant for an IT SERVICE CATALOG. Your job is to PRE-FILL
a request form for a specific catalog item from the user's request, so the human
only has to review and adjust — fill in as much as you reasonably can.

You are given the catalog item (name + what it is for), the form fields (each
with a label, type, and any options), and the user's request. Use ALL of it:

- Understand the catalog item's PURPOSE and each field's LABEL/type to know what
  the field is asking for, then map the user's words to it INTELLIGENTLY even
  when phrased differently — a job title → a "role"/"position" field, a team or
  org name → a "department" field, a city/site → a "location" field, a product
  or model → the relevant item field, a quantity word → a "quantity" field, etc.
- Use general IT / service-catalog knowledge to interpret terms and pick the
  best-fitting value (e.g. map a stated need to the closest provided `options`).
- Be generous in what you infer from the request, BUT never FABRICATE identity
  or contact data the user did not give — names, emails, employee ids, phone
  numbers, asset tags, cost codes. If such a field is not in the request, "".

TYPES
- type=date   → ISO YYYY-MM-DD. Resolve relative dates ("next monday",
  "tomorrow", "in two weeks", "end of month") against the "Today is …" line at
  the top. No date stated → "".
- type=select → choose EXACTLY one of the provided options (the closest match to
  the user's intent), else "".
- type=email  → only a real email present in the request, else "".
- type=number → digits only.

Return ONLY a JSON object with one key per field_name listed. No prose. A field
you cannot fill confidently MUST be "" (an empty box is fine; a wrong guess is
not).
"""


def _memo_put(memo: dict[str, dict[str, Any]], key: str,
              val: dict[str, Any]) -> None:
    if len(memo) >= _FLOW_MEMO_CAP:
        memo.clear()
    memo[key] = val


_SEARCH_KEYWORDS_INSTRUCTION = """
You extract catalog SEARCH terms from a user's IT service request.

Return 2-4 SHORT phrases (1-4 words each) that capture WHAT the user wants from
the service catalog — the item, service, software, hardware, or access itself —
and nothing else. STRIP every specific: person names, dates, emails, quantities,
departments, job titles. Those specifics dilute the search and must not appear.

Examples:
- "onboard Jane Doe starting July 1 in Engineering, manager bob@x.com"
    → {"keywords": ["onboarding", "new employee onboarding", "onboard employee"]}
- "I need a MacBook for the new designer"
    → {"keywords": ["laptop", "macbook", "developer laptop"]}
- "please give Bob VPN access to the prod network"
    → {"keywords": ["vpn access", "remote access", "vpn"]}

Return ONLY a JSON object: {"keywords": ["...", "..."]}. No prose.
"""


async def _extract_search_keywords(
    *, query: str, tenant_id: str, user_id: str,
) -> list[str]:
    """Reduce a (possibly verbose) request to 2-4 catalog-search phrases via ONE
    gateway call (runbook Playbook 3 step 1). Stripping the specifics keeps the
    semantic match on the ITEM, not the person/date/email. Returns [] on any
    failure so the caller falls back to the raw query."""
    if _gateway is None or not query:
        return []
    from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
    from oneops.policy.composer import Profile, compose
    sys_prompt = compose(Profile.FEATURE_AGENT_JSON,
                         extra_sections=[_SEARCH_KEYWORDS_INSTRUCTION])
    with _tracer.start_as_current_span(
        "uc08.tool.extract_search_keywords",
        attributes={_ONEOPS_TENANT_ID: tenant_id},
    ):
        try:
            resp = await asyncio.wait_for(
                _gateway.call(LlmRequest(
                    messages=(LlmMessage(role="system", content=sys_prompt),
                              LlmMessage(role="user", content=query)),
                    model=_EXTRACT_MODEL, tenant_id=tenant_id, user_id=user_id,
                    temperature=0.0, max_tokens=120,
                    response_format=ResponseFormat.JSON)),
                timeout=_EXTRACT_TIMEOUT_S)
        except Exception as exc:                              # noqa: BLE001
            _log.warning("uc08.keyword_extract_failed",
                         tenant_id=tenant_id, error=str(exc)[:160])
            return []
        raw = (resp.content or "").strip().lstrip("`").rstrip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        kws = parsed.get("keywords") if isinstance(parsed, dict) else None
        if not isinstance(kws, list):
            return []
        return [str(k).strip() for k in kws[:4] if str(k).strip()]


async def _draft_field_values(
    *, query: str, schema: list[dict[str, Any]], tenant_id: str, user_id: str,
    catalog_name: str = "", catalog_description: str = "",
) -> dict[str, str]:
    """Draft the form values from the user's request via ONE gateway call
    (single egress, §2.5). The catalog item name + description give the model
    the SERVICE-CATALOG context so it maps the request to fields intelligently.
    Returns {field_name: value} for what it could infer; unknown fields are
    omitted (the human fills them). Never raises — a failure just yields an
    empty draft so the form renders blank."""
    if _gateway is None or not schema:
        return {}
    specs = [{"field_name": f.get("field_name"), "label": f.get("label"),
              "type": f.get("type") or "text", "options": f.get("options")}
             for f in schema if f.get("field_name")]
    from datetime import datetime

    from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
    from oneops.policy.composer import Profile, compose
    sys_prompt = compose(Profile.FEATURE_AGENT_JSON,
                         extra_sections=[_DRAFT_FIELDS_INSTRUCTION])
    # Anchor relative dates ("next monday", "tomorrow", "in two weeks") to the
    # actual current date — the model cannot otherwise resolve them.
    now = datetime.now(UTC)
    catalog_ctx = (f"Catalog item: {catalog_name}"
                   + (f" — {catalog_description}" if catalog_description else "")
                   + "\n\n") if catalog_name else ""
    user_msg = (f"Today is {now:%Y-%m-%d} ({now:%A}).\n\n"
                f"{catalog_ctx}"
                f"User request:\n{query}\n\n"
                f"Form fields:\n{json.dumps(specs, ensure_ascii=False)}")
    with _tracer.start_as_current_span(
        "uc08.tool.draft_field_values",
        attributes={_ONEOPS_TENANT_ID: tenant_id, "uc08.field_count": len(specs)},
    ):
        try:
            resp = await asyncio.wait_for(
                _gateway.call(LlmRequest(
                    messages=(LlmMessage(role="system", content=sys_prompt),
                              LlmMessage(role="user", content=user_msg)),
                    model=_EXTRACT_MODEL, tenant_id=tenant_id, user_id=user_id,
                    temperature=0.0, max_tokens=400,
                    response_format=ResponseFormat.JSON)),
                timeout=_EXTRACT_TIMEOUT_S)
        except Exception as exc:                              # noqa: BLE001
            _log.warning("uc08.field_draft_failed",
                         tenant_id=tenant_id, error=str(exc)[:160])
            return {}
        raw = (resp.content or "").strip().lstrip("`").rstrip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        valid = {s["field_name"] for s in specs}
        return {k: str(v).strip() for k, v in
                (parsed.items() if isinstance(parsed, dict) else [])
                if k in valid and v not in (None, "")}


async def request_catalog_item(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Guided new-service-request flow (runbook Playbook 3). One conductor that
    drives search → pick → fields (one at a time) → confirm → create across
    turns via the interrupt protocol. The LLM does not pick tools here — this is
    deterministic sequencing; the user makes the choices through the widgets."""
    # Lazy import — avoid an import cycle with the executor package at load.
    from oneops.executor.nodes import (
        interrupt_for_confirmation,
        interrupt_for_input,
        interrupt_for_selection,
    )

    tenant_id, _, _ = _principal_from_context(context)
    with _tracer.start_as_current_span(
        "uc08.tool.request_catalog_item",
        attributes={_ONEOPS_TENANT_ID: tenant_id},
    ):
        session_id = str(context.get("session_id") or "")
        # ── Step 1: search the catalog (has-form items only) ──────────────
        query = _query_from(arguments, context)
        if not query:
            return {"ok": True, "display_text":
                    "What would you like to request from IT?"}
        _skey = f"{tenant_id}:{session_id}:{query}"
        listing = _search_memo.get(_skey)
        if listing is None:
            # Runbook step 1: search on 2-4 keyword variations of the ask, not
            # the verbose request (names/dates/emails dilute the match). Fall
            # back to the raw query if extraction is unavailable.
            keywords = await _extract_search_keywords(
                query=query, tenant_id=tenant_id,
                user_id=str(context.get("user_id") or "")) or [query]
            listing = await get_service_request_list(
                {"service_catalogs": keywords}, context)
            _memo_put(_search_memo, _skey, listing)
        matches = listing.get("matches") or []

        # ── Fallback: no catalog match → graceful decline (no incident) ───
        if not listing.get("ok") or not matches:
            return {"ok": True, "outcome": "no_match",
                    "display_text": (
                        f"I couldn't find a catalog item matching “{query}”. "
                        "You could try different wording, or I can flag this to "
                        "the IT team to follow up. (Raising a formal incident "
                        "isn't available from here yet.)")}

        # ── Step 2: user picks an item ────────────────────────────────────
        selection = interrupt_for_selection(
            "Which of these would you like to request?",
            [{"id": m["catalog_id"], "label": m["name"],
              "description": (m.get("description") or "")[:120]}
             for m in matches],
            allow_none=True,
        )
        chosen = _unwrap(selection, "selected")
        if not chosen or not (isinstance(chosen, dict) and chosen.get("id")):
            return {"ok": True, "outcome": "cancelled", "display_text":
                    "No problem — let me know if you'd like to request "
                    "something else."}
        catalog_id = str(chosen["id"])
        catalog_label = str(chosen.get("label") or catalog_id)

        # ── Step 3: fetch the intake form for the chosen item ─────────────
        _fkey = f"{tenant_id}:{catalog_id}"
        fields_resp = _fields_memo.get(_fkey)
        if fields_resp is None:
            fields_resp = await get_service_request_fields(
                {"catalog_id": catalog_id}, context)
            if fields_resp.get("ok"):
                _memo_put(_fields_memo, _fkey, fields_resp)
        if not fields_resp.get("ok"):
            return {"ok": False,
                    "error_code": fields_resp.get("error_code", "UC08_ERROR"),
                    "display_text": fields_resp.get(
                        "display_text",
                        "I couldn't load that item's request form.")}
        schema = fields_resp.get("fields") or []

        # ── Step 4: AI DRAFTS the field values from the user's request, then
        #    presents them in ONE editable form (pre-filled, human edits any
        #    field). Runbook "draft → present → approve": the AI fills what it
        #    can from context; the human is never asked to retype what they
        #    already said, but stays in control of every value. ──────────────
        values: dict[str, Any] = {}
        if schema:
            _dkey = f"{tenant_id}:{session_id}:{catalog_id}"
            draft = _draft_memo.get(_dkey)
            if draft is None:
                draft = await _draft_field_values(
                    query=query, schema=schema, tenant_id=tenant_id,
                    user_id=str(context.get("user_id") or ""),
                    catalog_name=catalog_label,
                    catalog_description=str(chosen.get("description") or ""))
                _memo_put(_draft_memo, _dkey, draft)

            def _missing(vals: dict[str, Any]) -> list[dict[str, Any]]:
                return [f for f in schema if f.get("field_name")
                        and f.get("required")
                        and not str(vals.get(f["field_name"], "")).strip()]

            def _form(vals: dict[str, Any]) -> list[dict[str, Any]]:
                return [{"name": f["field_name"],
                         "label": f.get("label") or f["field_name"],
                         "type": f.get("type") or "text",
                         "required": bool(f.get("required")),
                         "value": vals.get(f["field_name"], ""),
                         **({"options": f["options"]} if f.get("options") else {})}
                        for f in schema if f.get("field_name")]

            # Re-prompt the form until every REQUIRED field is filled, rather
            # than letting the create fail and dead-end. The form is re-shown
            # pre-filled with what the user already entered, flagging what's
            # still missing — they fill the gap in place, never restarting.
            current: dict[str, Any] = dict(draft)
            for _round in range(_MAX_FIELD_ROUNDS):
                if _round > 0 and not _missing(current):
                    break
                if _round == 0:
                    prompt = (f"I've drafted these details for “{catalog_label}” "
                              "from your request — edit anything, then send:")
                else:
                    names = ", ".join(f.get("label") or f["field_name"]
                                      for f in _missing(current))
                    prompt = (f"Almost there — “{catalog_label}” still needs: "
                              f"{names}. Please complete and send:")
                answer = interrupt_for_input(prompt, _form(current))
                current = {**current, **(dict(_unwrap(answer, "fields") or {}))}
            values = current

        # ── Step 5: show the filled template + confirm before submit (the
        #    SOLE approval gate, runbook step 5 — the human's final approval). ─
        confirmation = interrupt_for_confirmation(
            {"Item": catalog_label,
             **{k: v for k, v in values.items() if str(v).strip()}},
            "create_service_request",
        )
        confirmed = _unwrap(confirmation, "confirmed")
        if not (confirmed is True or confirmed == "true"):
            return {"ok": True, "outcome": "cancelled", "display_text":
                    f"Okay, I won't submit the request for “{catalog_label}”."}

        # ── Step 6 + 7: create and confirm with the SR id ─────────────────
        return await create_service_request(
            {"catalog_id": catalog_id, "fields": values}, context)


# ── Entry point — fulfill_request ──────────────────────────────────────────


async def fulfill_request(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Catalog item fulfillment entry point.

    Both the portal route POST /api/uc08/fulfill and the chat path land
    here. Identical results regardless of trigger (rule §F).

    Arguments expected:
      request_id        — parent SR id (FK)
      catalog_item_id   — catalog template id
      variables         — dict of form field values (default: {})
      requested_for     — user id this fulfilment is for (default: caller)
      quantity          — int (default 1)
      idempotency_key   — caller-supplied retry token (default None)

    Context expected (set by the tool runner):
      tenant_id, user_id, role, trace_id (optional)
      trigger_type — 'portal' | 'chat' (defaults to 'chat')
    """
    tenant_id, user_id, _ = _principal_from_context(context)
    trace_id = context.get("trace_id")
    trigger = context.get("trigger_type") or "chat"

    with _tracer.start_as_current_span(
        "uc08.tool.fulfill_request",
        attributes={
            _ONEOPS_TENANT_ID: tenant_id,
            "oneops.user_id": user_id,
            "uc08.trigger_type": trigger,
        },
    ):
        try:
            req = FulfillmentRequest(
                tenant_id=tenant_id,
                request_id=str(arguments.get("request_id") or "").strip(),
                catalog_item_id=str(arguments.get("catalog_item_id") or "").strip(),
                variables=dict(arguments.get("variables") or {}),
                requested_for=str(
                    arguments.get("requested_for") or user_id).strip(),
                opened_by=user_id,
                quantity=int(arguments.get("quantity") or 1),
                idempotency_key=arguments.get("idempotency_key"),
                trigger_type=TriggerType(trigger),
            )
        except (TypeError, ValueError) as exc:
            return {
                "ok": False,
                "error_code": "UC08_BAD_REQUEST",
                "error": f"invalid request: {exc}",
                "display_text": f"I couldn't start that fulfillment: {exc}",
            }

        try:
            outcome = await _core.fulfill_request(
                req,
                connection_provider=_connection_provider,
                trace_id=trace_id,
                actor=user_id,
            )
        except DuplicateRequestError as exc:
            _log.info("uc08.tool.duplicate_blocked",
                      tenant_id=tenant_id, error=str(exc))
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"An open fulfillment request already exists for this "
                    f"user + catalog item. {exc}"
                ),
            }
        except CatalogItemNotFoundError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"I don't recognise that catalog item "
                    f"({req.catalog_item_id})."
                ),
            }
        except RequestNotFoundError as exc:
            return {
                "ok": False,
                "error_code": exc.code,
                "error": str(exc),
                "display_text": (
                    f"That Service Request id doesn't exist "
                    f"({req.request_id})."
                ),
            }
        return outcome.model_dump(mode="json")


# ── Read-only — get_fulfillment_status (DOC-09 §UC-8 8.6) ────────────────


async def get_fulfillment_status(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Live status for one RITM."""
    tenant_id, _, _ = _principal_from_context(context)
    ritm_id = str(arguments.get("ritm_id") or "").strip()
    if not ritm_id:
        return {
            "ok": False,
            "error_code": "UC08_BAD_REQUEST",
            "error": "ritm_id is required",
        }
    with _tracer.start_as_current_span(
        "uc08.tool.get_fulfillment_status",
        attributes={_ONEOPS_TENANT_ID: tenant_id, "uc08.ritm_id": ritm_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                status = await _db.get_status(
                    tenant_id=tenant_id, ritm_id=ritm_id, conn=conn,
                )
            except RequestItemNotFoundError as exc:
                return {
                    "ok": False,
                    "error_code": exc.code,
                    "error": str(exc),
                    "display_text": f"No fulfillment record found for {ritm_id}.",
                }
            done = status.tasks_by_state.get("done", 0)
            in_prog = status.tasks_by_state.get("in_progress", 0)
            pending = (
                status.tasks_by_state.get("pending", 0)
                + status.tasks_by_state.get("ready", 0)
            )
            failed = status.tasks_by_state.get("failed", 0)
            blocked = status.tasks_by_state.get("blocked", 0)
            display = (
                f"{status.ritm_id} ({status.catalog_item_id}) — "
                f"state={status.state.value}. "
                f"Tasks: {done}/{status.tasks_total} done, "
                f"{in_prog} in progress, {pending} pending"
            )
            if failed:
                display += f", {failed} failed"
            if blocked:
                display += f", {blocked} blocked"
            if status.pending_approvals:
                display += f". Awaiting approval: {', '.join(status.pending_approvals)}"
            display += "."
            payload = status.model_dump(mode="json")
            payload["display_text"] = display
            payload["ok"] = True
            return payload
        finally:
            await conn.close()


# ── Read-only — load_catalog_template ────────────────────────────────────


async def load_catalog_template(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Catalog template lookup."""
    tenant_id, _, _ = _principal_from_context(context)
    catalog_item_id = str(arguments.get("catalog_item_id") or "").strip()
    if not catalog_item_id:
        return {"ok": False, "error_code": "UC08_BAD_REQUEST",
                "error": "catalog_item_id is required"}
    with _tracer.start_as_current_span(
        "uc08.tool.load_catalog_template",
        attributes={_ONEOPS_TENANT_ID: tenant_id,
                    "uc08.catalog_item_id": catalog_item_id},
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            try:
                tmpl = await _db.load_catalog_template(
                    tenant_id=tenant_id,
                    catalog_item_id=catalog_item_id, conn=conn,
                )
            except CatalogItemNotFoundError as exc:
                return {"ok": False, "error_code": exc.code, "error": str(exc)}
            payload = tmpl.model_dump(mode="json")
            payload["ok"] = True
            return payload
        finally:
            await conn.close()


# ── Read-only — check_request_duplicate (DOC-09 §UC-8 8.7) ──────────────


async def check_request_duplicate(
    arguments: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any]:
    """Detect open RITM for (requested_for, catalog_item)."""
    tenant_id, _, _ = _principal_from_context(context)
    requested_for = str(arguments.get("requested_for_user_id") or "").strip()
    catalog_item_id = str(arguments.get("catalog_item_id") or "").strip()
    lookback_days = int(arguments.get("lookback_days") or 30)
    if not requested_for or not catalog_item_id:
        return {"ok": False, "error_code": "UC08_BAD_REQUEST",
                "error": "requested_for_user_id and catalog_item_id required"}
    with _tracer.start_as_current_span(
        "uc08.tool.check_request_duplicate",
        attributes={
            _ONEOPS_TENANT_ID: tenant_id,
            "uc08.requested_for": requested_for,
            "uc08.catalog_item_id": catalog_item_id,
        },
    ):
        cp = _connection_provider or _db.default_connection_provider
        conn = await cp()
        try:
            existing = await _db.find_open_duplicate(
                tenant_id=tenant_id, requested_for=requested_for,
                catalog_item_id=catalog_item_id,
                lookback_days=lookback_days, conn=conn,
            )
            return {
                "ok": True,
                "duplicate_found": existing is not None,
                "existing_ritm_id": existing,
            }
        finally:
            await conn.close()


__all__ = [
    # boot-time injections
    "set_connection_provider",
    "set_gateway",
    "set_nats_client",
    # the chat conductor (runbook Playbook 3 entry) + the 4 runbook tools
    "request_catalog_item",
    "get_service_request_list",
    "get_service_request_fields",
    "create_service_request",
    "update_service_request",
    # engine-facing helpers (not LLM-pickable tools)
    "fulfill_request",
    "get_fulfillment_status",
    "load_catalog_template",
    "check_request_duplicate",
]
