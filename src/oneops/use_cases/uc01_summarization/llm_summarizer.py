"""UC-1 LLM summariser — builds a `SummarizeFn` over the `LlmGateway`.

This is the **only place** UC-1 talks to a model. The handler
(`summarize_entity`) accepts an injected `SummarizeFn`; production wires it
to this factory at startup. Every model call goes through `LlmGateway` —
single egress, single place for redaction, retry, quota, fallback, cost
accounting.

Output contract (matches the UC-1 cross-service shape):

    {
      "summary": "<LLM-generated paragraph>",
      "key_details": { "Status": "...", "Priority": "...", ... },
      "model": "<model that served>",
      "usage": {"prompt_tokens": N, "completion_tokens": N, "cost_usd": F}
    }

`key_details` keys are humanised labels (`incident_id` → "Incident ID",
`ci_name` → "CI Name"). The mapping is registry data — declared once per
service in `oneops.use_cases._shared.field_labels`. Adding a new service
means a new entry in that data, not new code here.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import get_logger, get_tracer, increment
from oneops.policy.composer import Profile, compose
from oneops.use_cases._shared.field_labels import humanise_record
from oneops.use_cases.uc01_summarization.cache import SummaryCacheStore

_log = get_logger("oneops.use_cases.uc01.llm_summarizer")
_tracer = get_tracer("oneops.use_cases.uc01.llm_summarizer")


# UC-1's UC-specific extras appended to the platform policy profile.
# These are the rules ONLY this use case adds; the safety + grounding +
# anti-fabrication + JSON-output policy comes from `Profile.FEATURE_AGENT_JSON`
# via the platform `compose()` ([[feedback_policy_layer_mandatory]] —
# never hand-craft a system prompt). Kept byte-identical across calls so
# the provider's prompt cache (and ours) reuses the prefix.
_UC1_SUMMARY_INSTRUCTIONS = (
    "## UC-1 Summary Rules — STRUCTURED + CONTEXTUAL\n"
    "Produce a single descriptive paragraph (4-7 sentences) that reads\n"
    "naturally to an operator but follows a fixed information order so\n"
    "two calls on the same record produce a stable paragraph. The\n"
    "paragraph applies to ANY record type — incident, request, problem,\n"
    "change, asset, CMDB CI, knowledge article. Pick whichever fields\n"
    "the record exposes; omit slots that aren't present.\n"
    "\n"
    "Information order (one sentence per slot, in this order — skip a\n"
    "slot if the record has no data for it):\n"
    "\n"
    "1. IDENTITY — Open by naming the record's canonical id and a\n"
    "   one-clause description of WHAT it is, drawn from\n"
    "   short_description / title / summary / ci_name / name. Use\n"
    "   natural prose, not a bullet shape. Example: 'The VPN Gateway —\n"
    "   APAC is a critical network asset currently active in the\n"
    "   production environment, located in Mumbai-DC.'\n"
    "\n"
    "2. STATE + SEVERITY/CRITICALITY — Current status/state (open /\n"
    "   in_progress / resolved / closed / active / retired / approved /\n"
    "   …) plus priority / severity / risk / criticality where\n"
    "   applicable. Example: 'It is open with P2 priority and high\n"
    "   severity, impacting multiple floors of the HQ network.'\n"
    "\n"
    "3. OWNERSHIP — Who owns the record. Mention assignment group /\n"
    "   assigned-to / owner / requested-by / created-by / approved-by\n"
    "   — whichever apply. Example: 'It is assigned to GRP-NETOPS\n"
    "   (USR00003), with approvals recorded from USR00005 and\n"
    "   USR00006.'\n"
    "\n"
    "4. KEY OPERATIONAL ATTRIBUTES — Use 1-2 sentences for the\n"
    "   record-type specific attributes that actually matter to an\n"
    "   operator. Examples by type:\n"
    "     • Incident / request: category, subcategory, service, impact,\n"
    "       urgency, the symptom from work_notes / comments / customer\n"
    "       quote when available.\n"
    "     • Problem: known_error, root_cause, workaround.\n"
    "     • Change: type, risk_level, impact, planned start/end,\n"
    "       actual start/end, affected_cis.\n"
    "     • Asset / CMDB CI: ci_type, environment, location, attributes\n"
    "       (os, model, vendor, ip_address, version, …), depends_on.\n"
    "     • Knowledge article: category, tags, audience.\n"
    "   Pull the attributes from the record verbatim; never invent.\n"
    "\n"
    "5. LINKED RECORDS — List related/linked entities (related_incidents,\n"
    "   related_problem, related_change, related_ci_ids, depends_on,\n"
    "   affected_cis) as comma-separated ids inline. Skip the sentence\n"
    "   if no links exist. Example: 'It depends on CI0000008 and is\n"
    "   linked to PBM0003001 and CHG0004001.'\n"
    "\n"
    "6. TIMELINE — One closing sentence with the most operationally\n"
    "   significant date the record exposes (SLA due, planned change\n"
    "   window, resolved_at, created_at). Skip if not present.\n"
    "\n"
    "Hard rules:\n"
    "- Faithful: every fact must come from the supplied record. Never\n"
    "  invent fields. Absent fields are not mentioned.\n"
    "- No embellishment (no 'this is significant because…', no\n"
    "  'unfortunately', no 'fortunately'). Operator-grade plain prose.\n"
    "- Same record + same fields → same paragraph (this is paired with\n"
    "  temperature=0; stylistic variation defeats consistency).\n"
    "- Return STRICT JSON, no markdown fences:\n"
    '    {\"summary\": \"<paragraph>\"}\n'
    "- The caller has already filtered the record by data classification\n"
    "  and role — never refer to a field that is not in the supplied\n"
    "  record."
)


def build_summarize_fn(gateway: LlmGateway, *, model: str = "gpt-4o-mini"):
    """Return a `SummarizeFn` callable bound to this gateway + model.

    Production startup calls `set_summarize_llm(build_summarize_fn(gw))`.
    Tests substitute their own SummarizeFn directly — no need to go through
    this builder for in-process unit tests.
    """

    async def summarize(
        record: dict[str, Any], tenant_id: str, model_override: str,
        *, user_id: str = "",
    ) -> dict[str, Any]:
        chosen_model = (model_override or model).strip() or model

        # ── humanised key_details — the deterministic half of the contract.
        # Built BEFORE the LLM is called so we have a working response even
        # if the LLM call fails (loud failure is still preferable — caller
        # decides; here we surface both halves).
        key_details = humanise_record(record)

        # ── system prompt: composed from the platform policy profile.
        # FEATURE_AGENT_JSON adds OUTPUT_SCHEMA + OBSERVABILITY on top of
        # FEATURE_AGENT_WITH_TOOLS (registry-grounding, RBAC, anti-fabrication,
        # tenant guards, conversation-state, field-visibility). UC-1's own
        # rules ride as `extra_sections`. `context` carries the envelope.
        # The composer caches the static portion for byte-identical prefix
        # across calls (prompt-cache invariant).
        system_prompt = compose(
            Profile.FEATURE_AGENT_JSON,
            context={
                "tenant_id": tenant_id,
                "ticket_id": str(
                    record.get("incident_id") or record.get("request_id")
                    or record.get("problem_id") or record.get("change_id")
                    or record.get("asset_id") or record.get("ci_id")
                    or record.get("kb_id") or ""),
            },
            extra_sections=[_UC1_SUMMARY_INSTRUCTIONS],
        )

        # ── user message — the record fields. The LLM's job is the paragraph.
        record_json = json.dumps(
            record, default=str, ensure_ascii=False)
        user_message = (
            "Summarise this work record. The structured fields are provided; "
            "your job is the natural-language paragraph.\n\n"
            f"RECORD:\n{record_json}\n"
        )

        request = LlmRequest(
            messages=(
                # System block is the LARGE STABLE PORTION (compose()'s
                # static-cached 33k-char output for FEATURE_AGENT_JSON +
                # UC-1 extras). Marking `cache_control=True` lets the
                # provider serve subsequent identical prefixes from its
                # prompt cache — ~50-90% input-token savings.
                LlmMessage(role="system", content=system_prompt,
                           cache_control=True),
                # User message changes per record; never cached.
                LlmMessage(role="user", content=user_message),
            ),
            model=chosen_model,
            tenant_id=tenant_id,
            user_id=user_id or "",
            temperature=0.0,
            max_tokens=512,
            response_format=ResponseFormat.JSON,
        )
        try:
            response = await gateway.call(request)
        except Exception as exc:                       # noqa: BLE001 — boundary
            # Loud, typed failure surface for the handler — the
            # `summarize_entity` handler will see this raise and return its
            # own `outcome="llm_unavailable"`-shaped result rather than
            # propagating an exception through the executor.
            _log.warning("uc01.llm_summarizer.gateway_error",
                         tenant_id=tenant_id, error=str(exc)[:200])
            raise

        # Parse strict JSON; tolerate the model occasionally emitting plain
        # text (some providers ignore `response_format`).
        summary_text: str
        try:
            parsed = json.loads(response.content)
            summary_text = str(parsed.get("summary") or "").strip()
            if not summary_text:
                summary_text = response.content.strip()
        except json.JSONDecodeError:
            summary_text = response.content.strip()

        return {
            "summary": summary_text,
            "key_details": key_details,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "cost_usd": response.cost_usd,
            },
        }

    return summarize


# ── cache-aside wrapper (UC-1 contract E3) ──────────────────────────────


def _fingerprint(*, tenant_id: str, service_id: str, entity_id: str,
                 record: dict[str, Any]) -> str:
    """Deterministic, stable key. Same `(tenant, service, entity, content)`
    → same fingerprint, regardless of dict-key order, on every process.

    Includes `tenant_id` so two tenants never collide. Includes the full
    record content via its own hash so a row mutation invalidates the
    cache automatically — there is no "stale of unknown age" surface.

    `role` is intentionally NOT in the key: by the time this function runs,
    the record has already been redacted by the field policy for the
    caller's role, so two different roles see two different records and
    therefore two different fingerprints by construction.
    """
    record_canonical = json.dumps(
        record, sort_keys=True, default=str, ensure_ascii=False)
    record_hash = hashlib.sha256(record_canonical.encode("utf-8")).hexdigest()
    composite = f"{tenant_id}|{service_id}|{entity_id}|{record_hash}"
    return hashlib.sha256(composite.encode("utf-8")).hexdigest()[:32]


def _entity_id_from(record: dict[str, Any]) -> str:
    """Pick the canonical primary-key value from a record. Matches the
    same precedence the field-labels humaniser uses."""
    for key in ("incident_id", "request_id", "problem_id", "change_id",
                "asset_id", "ci_id", "kb_id"):
        if record.get(key):
            return str(record[key])
    return ""


def _service_id_from(record: dict[str, Any]) -> str:
    """Same logic as field_labels._detect_service — re-implemented here to
    avoid an extra import dance."""
    if "incident_id" in record: return "incident"
    if "request_id"  in record: return "request"
    if "problem_id"  in record: return "problem"
    if "change_id"   in record: return "change"
    if "asset_id"    in record: return "asset"
    if "ci_id"       in record and "ci_name" in record: return "cmdb_ci"
    if "kb_id"       in record: return "knowledge"
    return ""


def build_cached_summarize_fn(
    gateway: LlmGateway,
    *,
    cache_store: SummaryCacheStore,
    model: str = "gpt-4o-mini",
):
    """Wrap `build_summarize_fn` with a tenant-partitioned cache-aside.

    The cache-aside contract:

      1. Compute a content-stable fingerprint for the (tenant, service,
         entity, record) tuple.
      2. `cache_store.get(fingerprint=…, tenant_id=…)` — on **hit**, return
         the cached summary immediately and stamp `_cache.hit=True` +
         `_cache.age_s=<seconds>`. **No LLM call.** No tokens spent.
      3. On **miss**, call the underlying `SummarizeFn`, then `put` the
         result back under the same fingerprint. Stamp `_cache.hit=False`.
      4. If the cache itself fails for any reason (Dragonfly down, etc.),
         fall through to the LLM — degradation is "more cost, same answer",
         never "wrong answer". Logged loudly.

    The handler reads `_cache.hit` and propagates `cache_hit` + `cache_age_s`
    into its top-level structured output, so the frontend's
    `detectCacheStatus` can increment the "Cache hits" counter.
    """
    underlying = build_summarize_fn(gateway, model=model)

    async def cached_summarize(
        record: dict[str, Any], tenant_id: str, model_override: str,
        *, user_id: str = "",
    ) -> dict[str, Any]:
        service_id = _service_id_from(record)
        entity_id = _entity_id_from(record)
        if not (tenant_id and service_id and entity_id):
            # No safe key → bypass cache. Loud log so an operator can spot
            # the upstream issue; still produce an answer.
            _log.warning(
                "uc01.cache_aside.skip_no_key",
                tenant_id=tenant_id, service_id=service_id,
                entity_id=entity_id)
            out = await underlying(record, tenant_id, model_override,
                                    user_id=user_id)
            out["_cache"] = {"hit": False, "age_s": None, "reason": "no_key"}
            return out

        fingerprint = _fingerprint(
            tenant_id=tenant_id, service_id=service_id,
            entity_id=entity_id, record=record)

        with _tracer.start_as_current_span(
            "uc01.cache_aside.get",
            attributes={
                "oneops.tenant_id": tenant_id,
                "oneops.service_id": service_id,
                "oneops.entity_id": entity_id,
                "cache.fingerprint": fingerprint,
            },
        ) as span:
            cached = None
            try:
                cached = await cache_store.get(
                    fingerprint=fingerprint, tenant_id=tenant_id)
            except Exception as exc:                  # noqa: BLE001 — boundary
                # Cache failure must NOT corrupt the response — fall through
                # to the LLM. Loud, traced, logged.
                span.set_attribute("error", True)
                _log.warning("uc01.cache_aside.read_failed",
                             error=str(exc)[:200])
            if cached is not None:
                span.set_attribute("cache.hit", True)
                age_s = max(0, int(__import__("time").time()
                                   - float(cached.get("cached_at") or 0.0)))
                summary = cached.get("summary") or {}
                # Defensive: rebuild the wire shape exactly as the
                # underlying SummarizeFn returns it.
                out = {
                    "summary": summary.get("summary", ""),
                    "key_details": summary.get("key_details", {}),
                    "model": summary.get("model", "(cached)"),
                    "usage": summary.get("usage", {}),
                    "_cache": {"hit": True, "age_s": age_s,
                               "fingerprint": fingerprint},
                }
                _log.info("uc01.cache_aside.hit",
                          tenant_id=tenant_id, fingerprint=fingerprint,
                          age_s=age_s)
                # Metric: cache hit. Dashboard's "Cache hit ratio" panel
                # reads `ai_cache_hits_total` (Prometheus) — without this
                # increment, the panel never moves even when hits happen.
                increment("ai.cache.hits.total", cache_name="uc01_summary")
                return out
            span.set_attribute("cache.hit", False)
            # Counter for the miss path — paired with hits to compute ratio.
            increment("ai.cache.misses.total", cache_name="uc01_summary")

        # ── miss path: call the LLM, then write back ────────────────
        out = await underlying(record, tenant_id, model_override,
                                user_id=user_id)
        # `out` carries {summary, key_details, model, usage}. Persist
        # the whole dict so a subsequent hit reconstructs the shape
        # exactly. Tenant-partitioned by the cache store itself.
        try:
            await cache_store.put(
                fingerprint=fingerprint, tenant_id=tenant_id,
                summary=out)
        except Exception as exc:                      # noqa: BLE001 — boundary
            # Write-failure is non-fatal — the LLM result still goes back.
            _log.warning("uc01.cache_aside.write_failed",
                         error=str(exc)[:200])
        out["_cache"] = {"hit": False, "age_s": None,
                         "fingerprint": fingerprint}
        return out

    return cached_summarize


__all__ = ["build_summarize_fn", "build_cached_summarize_fn"]
