"""UC-1 LLM summariser — builds a `SummarizeFn` over the `LlmGateway`.

This is the **only place** UC-1 talks to a model. The handler
(`summarize_entity`) accepts an injected `SummarizeFn`; production wires it
to this factory at startup. Every model call goes through `LlmGateway` —
single egress, single place for redaction, retry, quota, fallback, cost
accounting.

Output contract (matches the UC-1 cross-service shape):

    {
      "summary": "<compact markdown — a status line, a 3-4 line grounded\n
                   narrative, then 2-3 dated bullets when the record has\n
                   work-notes/comments/milestones>",
      "key_details": { "Status": "...", "Priority": "...", ... },
      "model": "<model that served>",
      "usage": {"prompt_tokens": N, "completion_tokens": N, "cost_usd": F}
    }

The `summary` is the SINGLE user-facing block (the frontend hides the raw
`key_details` list for the summary outcome). `key_details` is still produced
here — it feeds the cache fingerprint and the deterministic fallback — but it
is not rendered to the user as a key/value dump.

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
from oneops.use_cases._shared.field_labels import (
    _detect_service,
    _label_for,
    humanise_record,
)
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
    "## UC-1 Summary Rules — STRUCTURED OUTPUT (assembled deterministically)\n"
    "You provide grounded CONTENT as structured JSON; the system assembles\n"
    "the final user-facing layout from it. You do NOT format markdown,\n"
    "choose bullet characters, or decide whether a section appears — the\n"
    "code does that from the data you return. This keeps the layout\n"
    "identical for every record type and impossible to break.\n"
    "\n"
    "Return STRICT JSON with EXACTLY these three keys (no markdown fences):\n"
    "\n"
    '  {\n'
    '    \"status_line\": { <label>: <value>, ... },\n'
    '    \"narrative\":   \"<2-3 short grounded sentences>\",\n'
    '    \"key_updates\": [ \"<line>\", ... ]\n'
    '  }\n'
    "\n"
    "status_line — a COMPACT object of AT MOST 3-5 HEADLINE facts the user\n"
    "  needs at a glance: the record's lifecycle state, its current stage /\n"
    "  phase WHEN the record tracks one (e.g. a service request's approval\n"
    "  or fulfillment stage), its overall priority / severity / risk /\n"
    "  criticality, and its primary owner or assignee. Pick the ones that\n"
    "  genuinely apply. KEY each entry by the record's exact field name\n"
    "  (e.g. 'status', 'stage', 'priority', 'assigned_to') with its value\n"
    "  FROM THE RECORD — the system renders the human label, so you do not\n"
    "  format labels. This is NOT a field dump — do NOT include the id,\n"
    "  title,\n"
    "  category, location, dates, technical specs, or other secondary\n"
    "  attributes here; those belong in the narrative if they matter.\n"
    "  Include a label ONLY when its value is present and non-empty; if\n"
    "  none apply, return an empty object {}. Never invent a value, never\n"
    "  emit 'N/A', 'none', or 'unknown'.\n"
    "\n"
    "narrative — 2-3 plain factual sentences (<= ~60 words), drawn ONLY\n"
    "  from the record: what the record is / what happened, what is\n"
    "  currently pending, and any linked/related records named inline by\n"
    "  id. Terse, no padding, no restating the status_line.\n"
    "\n"
    "key_updates — an array of 0-3 short strings, the chronological /\n"
    "  decision highlights. Populate it ONLY from real content the record\n"
    "  carries: prior progress notes or comments (paraphrase each as\n"
    "  '<date>: <one line>'), or a change's approval/schedule milestones,\n"
    "  or a problem's root-cause / workaround / known-error findings. If\n"
    "  the record carries NONE of these (e.g. an asset, a CI, or a ticket\n"
    "  with no notes), return an empty array []. NEVER add a line that\n"
    "  announces absence ('no comments', 'none', 'N/A'); just return [].\n"
    "  Never paste raw note JSON, author/record ids, flags, or a verbatim\n"
    "  customer quote — paraphrase.\n"
    "\n"
    "Anti-hallucination hard rules (non-negotiable):\n"
    "- Every value must be grounded in the supplied record. NEVER invent,\n"
    "  guess, infer, or add context that is not literally present.\n"
    "- No vague filler ('it seems', 'likely', 'appears to', 'may have'),\n"
    "  no embellishment ('unfortunately', 'critically important'), no\n"
    "  advice or next-step opinions. Operator-grade plain facts only.\n"
    "- An absent field is simply omitted — never described as missing,\n"
    "  unknown, or N/A, anywhere in the output.\n"
    "- The caller already filtered the record by data classification and\n"
    "  role — never refer to a field that is not in the supplied record.\n"
    "- Same record + same fields => same JSON (paired with temperature=0)."
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
            max_tokens=900,
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

        # ── Parse the model's STRUCTURED output ──────────────────────────
        # The layout is then assembled deterministically by `_assemble_
        # summary` — the model never controls markdown / section presence,
        # so empty sections and absence-filler are impossible by construction.
        # Back-compat: a model that ignores the structured schema and emits
        # the legacy {"summary": "..."} or plain text still renders.
        summary_text: str
        try:
            parsed = json.loads(response.content)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict) and any(
                k in parsed for k in ("status_line", "narrative", "key_updates")):
            summary_text = _assemble_summary(parsed, _detect_service(record))
        elif isinstance(parsed, dict) and "summary" in parsed:
            summary_text = str(parsed.get("summary") or "").strip()
        elif parsed is None:
            # Provider ignored response_format and returned plain text.
            summary_text = (response.content or "").strip()
        else:
            summary_text = ""

        # ── Robustness guard — the format MUST NOT break ─────────────────
        # If nothing usable came back (empty structure / blank text), do NOT
        # surface a blank bubble and do NOT fabricate prose. Fall back to a
        # deterministic, fully-grounded summary built from the registry-
        # humanised projection. Guarantees a stable, non-empty,
        # hallucination-free summary on every call.
        if not summary_text.strip():
            summary_text = _deterministic_summary(key_details)
            _log.warning("uc01.llm_summarizer.empty_output_fallback",
                         tenant_id=tenant_id)

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


def _assemble_summary(parsed: dict[str, Any], service_id: str = "") -> str:
    """Deterministically assemble the user-facing markdown from the model's
    STRUCTURED output.

    The layout — not the model — decides what renders, so the output is
    consistent for every record type and cannot break:
      * status_line  → '**<label>**: <value>' joined by '  ·  ', present
                        labels only (empty/blank values dropped). The model
                        keys each entry by the record's field name; the
                        canonical human label is applied HERE via the
                        registry (`_label_for`) so casing is consistent
                        ('assigned_to' → 'Assigned To') regardless of what
                        the model echoed.
      * narrative    → verbatim paragraph.
      * key_updates  → a '**Key updates**' heading + markdown '- ' bullets,
                        rendered ONLY when the array has real content; an
                        empty array yields NO heading and NO bullets, so an
                        empty section or 'no comments' filler is impossible.

    Fully generic: it iterates whatever fields/strings the model returned and
    derives labels from the registry — no hardcoded field names — so
    renamed/added/removed fields just flow through (the no-static rule).
    """
    blocks: list[str] = []

    status = parsed.get("status_line")
    if isinstance(status, dict):
        parts = [
            f"**{_label_for(str(field).strip(), service_id)}**: {str(value).strip()}"
            for field, value in status.items()
            if value not in (None, "", [], {}) and str(value).strip()
            and str(field).strip()
        ]
        if parts:
            blocks.append("  ·  ".join(parts))

    narrative = str(parsed.get("narrative") or "").strip()
    if narrative:
        blocks.append(narrative)

    updates = parsed.get("key_updates")
    if isinstance(updates, list):
        bullets = [
            "- " + str(u).strip().lstrip("-•* ").strip()
            for u in updates
            if str(u).strip()
        ][:3]
        if bullets:
            blocks.append("**Key updates**\n" + "\n".join(bullets))

    return "\n\n".join(blocks).strip()


def _deterministic_summary(key_details: dict[str, Any]) -> str:
    """Grounded, no-LLM fallback summary — used only when the model returns
    nothing usable.

    Driven ENTIRELY by the registry-humanised `key_details` projection (the
    same data-driven mapping the cache and UI use). It hardcodes NO field
    names, so a field that is added, renamed, or deleted in the registry
    flows through automatically — there is no static catalog here to drift.
    Every value is verbatim from the record, so it cannot hallucinate, and
    the result is never blank.
    """
    rendered = {
        str(label): str(value)
        for label, value in (key_details or {}).items()
        if value not in (None, "", [], {})
    }
    if not rendered:
        return "No summarisable details are available for this record."
    return "  ·  ".join(f"**{label}**: {value}"
                        for label, value in rendered.items())


# ── cache-aside wrapper (UC-1 contract E3) ──────────────────────────────


def _fingerprint(*, tenant_id: str, service_id: str, entity_id: str,
                 record: dict[str, Any]) -> str:
    """Deterministic, stable key. Same `(tenant, service, entity, content)`
    → same fingerprint, regardless of dict-key order, on every process.

    Includes `tenant_id` so two tenants never collide. Includes the full
    record content via its own hash so a row mutation invalidates the
    cache automatically — there is no "stale of unknown age" surface.

    Includes `HUMANISE_RECORD_VERSION` so any change to the render-side
    filter (`_HIDDEN`) or label map auto-invalidates every cached row —
    this is the production fix for the stale-cache-after-code-change
    class of bugs (2026-05-30 leak of `search_tsv` + `content_hash_*`).

    `role` is intentionally NOT in the key: by the time this function runs,
    the record has already been redacted by the field policy for the
    caller's role, so two different roles see two different records and
    therefore two different fingerprints by construction.
    """
    from oneops.use_cases._shared.field_labels import HUMANISE_RECORD_VERSION

    record_canonical = json.dumps(
        record, sort_keys=True, default=str, ensure_ascii=False)
    record_hash = hashlib.sha256(record_canonical.encode("utf-8")).hexdigest()
    composite = (
        f"{tenant_id}|{service_id}|{entity_id}|"
        f"{record_hash}|render={HUMANISE_RECORD_VERSION}"
    )
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
