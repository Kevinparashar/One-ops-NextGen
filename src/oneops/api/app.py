"""FastAPI app — the chat + fast-path ingress.

Two POST routes serve the engine; one GET serves the demo frontend.

  * `POST /api/chat`            — natural-language ingress (router path)
  * `POST /api/fast/{uc_id}`    — button ingress (UI-declared intent)
  * `GET  /`                    — single-page HTML demo (chat + buttons)
  * `GET  /api/fast/{uc_id}/spec` — return the declared input schema so a
                                     frontend can render the right form

Both POSTs return the **same response contract** — `final_status`,
`final_response`, `step_results`, `session_id`, `request_id`, `trace_id`.
A test compares the two doors for one identical intent ⇒ identical shape.

The compiled executor + registry are built once at app start (lifespan) and
reused across requests — multi-user concurrent safety comes from the
stateless services + asyncpg pool + LangGraph's per-thread checkpointer.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, cast

from fastapi import FastAPI, HTTPException, Path, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from oneops.config import get_settings
from oneops.errors import NATSUnavailableError, OneOpsError
from oneops.executor.graph import build_executor_graph, resume_turn, run_turn
from oneops.executor.memory import NoopTrimmer, TokenBudgetTrimmer
from oneops.executor.step_runner import HandlerStepExecutor
from oneops.observability import (
    get_logger,
    get_tracer,
    langfuse_capture_content_enabled,
    redact_for_span,
    set_langfuse_io,
    set_langfuse_trace,
)
from oneops.observability.metrics import increment as _metric_inc
from oneops.registry.loader import load_registry
from oneops.router.fast_path import (
    FastPathDispatcher,
    FastPathError,
    FastPathRequest,
)
from oneops.router.router import Router
from oneops.session import InMemoryEventLog, InMemoryHotWindow, SessionEventStore
from oneops.session.profile_store import get_user_profile_store  # noqa: F401 - eager-load
from oneops.toolrunner.resolver import HandlerResolver

# Telemetry/HTTP literals → constants (sonar S1192).
_APPLICATION_X_NDJSON = "application/x-ndjson"

_log = get_logger("oneops.api")
_tracer = get_tracer("oneops.api")

DEFAULT_TENANT_FALLBACK = os.getenv("ONEOPS_DEV_DEFAULT_TENANT", "T001")
DEFAULT_USER_FALLBACK = os.getenv("ONEOPS_DEV_DEFAULT_USER", "oneops")
DEFAULT_ROLE_FALLBACK = os.getenv("ONEOPS_DEV_DEFAULT_ROLE", "service_desk_agent")


def _build_session_store() -> SessionEventStore:
    """Construct the process-wide `SessionEventStore`.

    Today: InMemory backends — survive the process lifetime, which is the
    durability surface a single-process FaaS dev / demo needs. Production
    is one env-flag away:

      * `ONEOPS_SESSION_BACKEND=postgres+dragonfly` → wire
        `PostgresEventLog(asyncpg_pool)` + `DragonflyHotWindow(client)`
        instead. The `SessionEventStore` Protocol seam does not change.

    Tenant isolation is structural — every `append` / `recent` takes
    `tenant_id`; the in-memory dict is keyed `(tenant_id, session_id)`,
    so two tenants never collide on a shared session id.
    """
    backend = os.getenv("ONEOPS_SESSION_BACKEND", "memory").strip().lower()
    if backend == "memory":
        cold = InMemoryEventLog()
        hot = InMemoryHotWindow()
        _log.info("oneops.api.session_store_selected", backend="memory",
                  durability="process-lifetime")
        return SessionEventStore(cold=cold, hot=hot)
    if backend == "dragonfly":
        # Demo / single-process FaaS durable: cold log + hot window both
        # live on Dragonfly. Survives uvicorn restart; survives multi-tab
        # reload. Production swaps cold log for Postgres via the same
        # Protocol seam — no caller change.
        from oneops.session.dragonfly_log import DragonflyEventLog
        from oneops.session.dragonfly_window import DragonflyHotWindow
        cold = DragonflyEventLog.from_settings()
        hot = DragonflyHotWindow.from_settings()
        _log.info("oneops.api.session_store_selected", backend="dragonfly",
                  durability="cluster-lifetime")
        return SessionEventStore(cold=cold, hot=hot)
    raise RuntimeError(
        f"ONEOPS_SESSION_BACKEND={backend!r} is not yet supported; "
        f"use 'memory' (default) or 'dragonfly'.")


def _build_llm_gateway():
    """Construct an `LlmGateway` over the LiteLLM proxy when the env is
    configured. Returns `None` when the proxy URL is absent — the status
    chip reports the honest state and `summarize_entity` falls through to
    its `outcome="llm_unavailable"` path.

    The gateway is shared process-wide. Quota + cost tracking are stamped
    per-tenant by the gateway itself."""
    base_url = os.getenv("LLM_GATEWAY_URL", "").strip()
    if not base_url:
        return None
    # Canonical env var names (match `.env`). Production deployments configure
    # these via the secret manager; no per-application aliases.
    api_key = os.getenv("LLM_GATEWAY_API_KEY", "").strip()
    try:
        from oneops.llm.gateway import LlmGateway
        from oneops.llm.transport import LiteLLMTransport
        transport = LiteLLMTransport(
            base_url=base_url, api_key=api_key,
            timeout_s=float(os.getenv("LLM_TIMEOUT_SECONDS", "60.0")))
        return LlmGateway(
            transport=transport,
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            fallback_model=os.getenv("LLM_FALLBACK_MODEL") or None,
        )
    except Exception as exc:                          # noqa: BLE001 — boundary
        _log.warning("oneops.api.llm_gateway_build_failed",
                     error=str(exc)[:200])
        return None


def _nats_connection_is_live() -> bool:
    """Return True when this process holds a live NATS connection.

    Topology-agnostic: works for the single-process demo, the split-
    role topology (ingress / graph_worker / agent_worker), and any
    future multi-replica deployment. The signal is the actual
    transport state (`NATSClient.is_connected`), not an inferred
    "I have an embedded worker" flag — those flags lie in split mode.

    Safe to call from request paths (sync, no I/O): we peek at the
    process-wide singleton's `is_connected` boolean. If no client has
    been constructed yet (cold boot or NATS disabled), returns False.
    """
    try:
        from oneops.adapters.nats_client import _client  # noqa: SLF001 — boundary
        return bool(_client and _client.is_connected)
    except Exception:                                       # noqa: BLE001 — boundary
        return False


def _build_focus_intent_classifier(gateway: Any):
    """Construct the focus-intent classifier with Dragonfly cache (when
    available) for repeat-phrase fast-paths. Falls back to no-cache if
    Dragonfly is not configured — classifier still works, just pays per call.
    """
    from oneops.router.intent_classifier import FocusIntentClassifier
    cache = None
    try:
        from oneops.use_cases.uc01_summarization.cache import get_summary_cache_store
        cache = get_summary_cache_store()
    except Exception:                                              # noqa: BLE001
        cache = None
    return FocusIntentClassifier(gateway=gateway, cache=cache)


def _build_time_filter_extractor(gateway: Any):
    """Construct the conditional TimeFilter extractor. Same cache pattern
    as the focus-intent classifier — Dragonfly when available, no-cache fall-
    back. Skipped entirely when the gateway is unavailable; callers see
    `time_filter` as an empty dict in context (which is correct: no scope)."""
    if gateway is None:
        return None
    from oneops.router.time_filter_extractor import TimeFilterExtractor
    cache = None
    try:
        from oneops.use_cases.uc01_summarization.cache import get_summary_cache_store
        cache = get_summary_cache_store()
    except Exception:                                                  # noqa: BLE001
        cache = None
    return TimeFilterExtractor(gateway=gateway, cache=cache)


def _build_conversation_trimmer(gateway: Any, model: str):
    """Construct the conversation trimmer for the executor (substrate G2).

    Production behaviour:
      * Gateway wired → `TokenBudgetTrimmer` with a real summariser that
        compresses the oldest-N turns into one synthetic system message.
        Token budget + keep-window come from env so operators tune
        without code changes.
      * No gateway → `NoopTrimmer`. The handler can't safely summarise
        without an LLM; refusing-loud (`ConversationTrimError`) would
        block local dev. NoopTrimmer keeps behaviour exactly as it is
        today for those paths.

    Env knobs (with defaults):
      * `CONVERSATION_MAX_TOKENS`   = 4000
      * `CONVERSATION_KEEP_TURNS`   = 10  (most-recent verbatim turns)
    """
    max_tokens = int(os.getenv("CONVERSATION_MAX_TOKENS", "4000"))
    keep_turns = int(os.getenv("CONVERSATION_KEEP_TURNS", "10"))
    if gateway is None:
        return NoopTrimmer()

    async def _summarise(
        messages: list[dict[str, str]], tenant_id: str,
    ) -> str:
        # Single LLM call → compresses oldest-prefix into a paragraph.
        # The gateway is the only egress (Component Spec C11), policy
        # composed (C15), tenant-scoped (C13).
        from oneops.llm import LlmMessage, LlmRequest, ResponseFormat
        from oneops.policy import Profile, compose

        rules = (
            "You compress an ITSM assistant's prior conversation into ONE "
            "short factual paragraph (≤120 words). Capture: which records "
            "were discussed, what the user asked about each, what the "
            "assistant answered. Drop greetings, repeats, and chit-chat. "
            "Never invent facts."
        )
        system_prompt = compose(Profile.INTERNAL_AGENT, extra_sections=[rules])
        # Serialise the prefix as a numbered transcript so the LLM has
        # clear structure.
        transcript = "\n".join(
            f"{i+1}. {m.get('role','')}: {m.get('content','')}"
            for i, m in enumerate(messages))
        try:
            resp = await gateway.call(LlmRequest(
                messages=(
                    LlmMessage("system", system_prompt, cache_control=True),
                    LlmMessage("user", transcript),
                ),
                model=model or "gpt-4o-mini",
                tenant_id=tenant_id or "_unknown",
                response_format=ResponseFormat.TEXT,
                request_id=""))
            return (resp.content or "").strip()
        except Exception as exc:                          # noqa: BLE001 — boundary
            _log.warning("oneops.api.trimmer_summary_failed",
                         error=str(exc)[:200])
            # Return empty → trimmer raises ConversationTrimError loudly
            # (per its contract). Caller surfaces a typed turn failure.
            return ""

    return TokenBudgetTrimmer(
        max_tokens=max_tokens,
        keep_last_turns=keep_turns,
        summariser=_summarise,
    )


# Per-UC phrasing for fast-path messages. Each entry maps an `inputs` dict
# to the natural-English message stored in the session log + shown on
# reload. Adding a new UC = one line; no code branch.
_FAST_PATH_PHRASING: dict[str, Any] = {
    "uc01_summarization":
        lambda inp: f"Summarize {inp.get('ticket_id') or '(no id)'}",
    "uc03_kb_lookup":
        lambda inp: f"Show knowledge article {inp.get('article_id') or '(no id)'}",
}


def _uc_display_name(uc_id: str, registry: Any = None) -> str:
    """Human-friendly use-case name that NEVER exposes the ``ucNN_`` wire prefix.

    Single source of truth is the registry agent's ``name`` (minus a trailing
    " Agent"), so display labels stay consistent with the catalogue. Falls back
    to deriving the name from the uc_id when the registry/agent is unavailable —
    e.g. ``uc02_similar_tickets`` -> "Similar Tickets". The ``uc_id`` itself is a
    stable contract (routes/registry ids) and is left untouched; this only
    affects what a human reads.
    """
    if registry is not None:
        agent = registry.agents.get_optional(uc_id)
        name = (getattr(agent, "name", "") or "").strip()
        if name.endswith(" Agent"):
            name = name[: -len(" Agent")].strip()
        if name:
            return name
    parts = (uc_id or "").split("_")
    tail = " ".join(parts[1:]) if len(parts) > 1 else (uc_id or "")
    return tail.title() if tail else "request"


def _humanise_fast_path_request(uc_id: str, inputs: dict[str, Any],
                                registry: Any = None) -> str:
    """Build the user-facing message stored in the session log when a turn
    enters via the fast-path button. Falls back to a generic shape for any
    UC that hasn't declared phrasing yet — using the descriptive use-case name
    (never the raw ``ucNN_`` id)."""
    phraser = _FAST_PATH_PHRASING.get(uc_id)
    if phraser is not None:
        return phraser(inputs)
    first_value = next(iter((inputs or {}).values()), "")
    label = _uc_display_name(uc_id, registry) if uc_id else "request"
    return f"Run {label}: {first_value}".rstrip(": ")


def _summarizer_is_wired() -> bool:
    """`True` once a `SummarizeFn` has been registered via
    `set_summarize_llm`. Until E2 ships this stays `False`; the UI's status
    chip surfaces the honest state — never claims wired when it isn't."""
    try:
        from oneops.use_cases.uc01_summarization.tools import _get_summarize_fn
        return _get_summarize_fn() is not None
    except Exception:                       # noqa: BLE001 — health probe
        return False


# ── request / response shapes ───────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4_000)
    session_id: str | None = None
    # Pre-routed dispatch: when a caller (e.g. a team manager's member-selector)
    # has already chosen the agent(s), the executor SKIPS the LLM router and runs
    # them directly. Bounded to cap the surface; ids with no active record are
    # dropped downstream (never invent). None/empty → normal routing.
    forced_agent_ids: list[str] | None = Field(default=None, max_length=16)
    # Conversational Interrupt Protocol: set by the frontend when the user
    # responds to a paused interrupt. interrupt_resume=True signals that
    # interrupt_answer should be forwarded to the waiting LangGraph node via
    # Command(resume=answer). Both fields are None on ordinary turns.
    interrupt_resume: bool | None = None
    interrupt_answer: dict[str, Any] | None = None


# Bounds on the free-form fast-path `inputs` dict. Real fast-path inputs are a
# handful of short scalar fields (e.g. {"ticket_id": "INC0001001"}); these caps
# are deliberately generous so no legitimate request is rejected, while closing
# the DoS surface of an arbitrarily large / deeply-nested payload reaching the
# downstream validators, SQL, and embeddings (audit P1-5 / P1-6).
_MAX_FAST_PATH_INPUT_KEYS = 50
_MAX_FAST_PATH_INPUT_DEPTH = 6
_MAX_FAST_PATH_INPUT_BYTES = 64 * 1024


def _extract_interrupt_payload(out: Any) -> dict[str, Any] | None:
    """A turn paused by the Conversational Interrupt Protocol. With a
    checkpointer configured, LangGraph does NOT raise GraphInterrupt — it
    RETURNS the state with `__interrupt__` populated (a sequence of Interrupt
    objects, each carrying `.value`). Normalise that into the typed payload
    dict the frontend renders, or None when the turn did not pause."""
    pending = out.get("__interrupt__") if isinstance(out, dict) else None
    if not pending:
        return None
    val = pending[0] if isinstance(pending, (list, tuple)) else pending
    if hasattr(val, "value"):
        inner = val.value
        return inner if isinstance(inner, dict) else {"value": inner}
    return val if isinstance(val, dict) else {"value": val}


# Identify the knowledge and fulfilment agents by registry DATA, not literal ids
# (storage-agnostic: JSON today, itsm.agent in prod). The KB suffix matches the
# convention the router's _floor_dispatch already uses; the fulfilment agent is
# found by its intent_family (a column on the agent record).
_KB_AGENT_SUFFIX = "_kb_lookup"
_FULFILMENT_INTENT_FAMILY = "fulfillment_orchestrate"


def _active_fulfilment_agent_id(registry: Any) -> str | None:
    """The active fulfilment/request agent id, derived from its registry
    intent_family. None when none is active or the registry is unavailable."""
    if registry is None:
        return None
    try:
        for agent in registry.agents.list_active():
            if getattr(agent, "intent_family", "") == _FULFILMENT_INTENT_FAMILY:
                return agent.id
    except Exception:                                             # noqa: BLE001
        return None
    return None


def _build_service_request_offer(
    out: Any, registry: Any, message: str,
) -> dict[str, Any] | None:
    """Self-service-first, two-turn offer. When a turn was answered SOLELY by the
    knowledge (KB) agent — the ambiguous "is this a how-to or a request?" default
    lands here — offer to raise a service request. Choosing it PINS the fulfilment
    agent via `forced_agent_ids` (so it can never loop back to KB) and passes the
    ORIGINAL query, which the forced path threads as the catalog-search seed — so
    the conductor runs its FULL flow (search → pick → form → create), not the
    empty "what would you like to request?" opener. Returns None when the turn was
    not a sole-KB answer or no fulfilment agent is active; never raises."""
    if not isinstance(out, dict):
        return None
    if str(out.get("final_status") or "").lower() not in ("executed", "partial"):
        return None
    agents = {str((s or {}).get("agent_id") or "")
              for s in (out.get("step_results") or [])}
    agents.discard("")
    if not agents or not all(a.endswith(_KB_AGENT_SUFFIX) for a in agents):
        return None
    fulfil_id = _active_fulfilment_agent_id(registry)
    if not fulfil_id:
        return None
    return {
        "kind": "service_request_offer",
        "prompt": ("If that didn't resolve it, I can raise a service request to "
                   "get it actioned for you."),
        "options": [
            {"label": "Raise a service request", "value": "yes",
             "forced_agent_ids": [fulfil_id], "message": message},
            {"label": "No, thanks", "value": "no"},
        ],
    }


def _nesting_depth(value: Any, _depth: int = 1) -> int:
    """Max container-nesting depth of a JSON-like value (scalars = 0)."""
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, (list, tuple)):
        children = value
    else:
        return _depth - 1
    return max((_nesting_depth(c, _depth + 1) for c in children), default=_depth)


class FastPathPostRequest(BaseModel):
    """Caller-supplied structured input for a fast-path UC. The dispatcher
    validates this against the UC's declared `fast_path.input_fields`."""

    inputs: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None

    @field_validator("inputs")
    @classmethod
    def _bound_inputs(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject pathologically large/deep payloads with a clean 422 before
        they reach downstream processing (no behavior change for real inputs)."""
        if len(value) > _MAX_FAST_PATH_INPUT_KEYS:
            raise ValueError(
                f"inputs has too many keys "
                f"(max {_MAX_FAST_PATH_INPUT_KEYS})")
        if _nesting_depth(value) > _MAX_FAST_PATH_INPUT_DEPTH:
            raise ValueError(
                f"inputs is nested too deeply "
                f"(max depth {_MAX_FAST_PATH_INPUT_DEPTH})")
        size = len(json.dumps(value, default=str).encode("utf-8"))
        if size > _MAX_FAST_PATH_INPUT_BYTES:
            raise ValueError(
                f"inputs is too large "
                f"(max {_MAX_FAST_PATH_INPUT_BYTES} bytes)")
        return value


class TurnResponse(BaseModel):
    """Canonical contract returned by BOTH doors. Frontend renders it the
    same way regardless of how the turn was entered."""

    door: str                            # "chat" | "fast_path"
    final_status: str
    final_response: str
    step_results: list[dict[str, Any]]
    session_id: str
    request_id: str
    trace_id: str | None = None
    latency_ms: int
    # Conversational Interrupt Protocol: non-None when the executor paused
    # mid-turn waiting for user input. Frontend renders the appropriate widget
    # and sends interrupt_resume=True + interrupt_answer on the next turn.
    interrupt: dict[str, Any] | None = None


# ── envelope construction ───────────────────────────────────────────────


class _RequestShim:
    """Minimal duck-type for the bits `_run` reads off a Request — just
    `.app` so we can reach `app.state`. The WebSocket route uses this
    shim so the per-frame inner code is BYTE-IDENTICAL to the HTTP path
    (zero behaviour drift between the two transports)."""

    __slots__ = ("app",)

    def __init__(self, app: Any) -> None:
        self.app = app


def _principal_from_headers(request: Request) -> tuple[str, str, str]:
    """Dev-mode auth: pick tenant / user / role from request headers, with
    safe fallbacks for the demo. Production swaps this for the JWT claims
    parsed upstream by AWS API Gateway / Cognito."""
    h = request.headers
    tenant_id = (h.get("x-tenant-id") or DEFAULT_TENANT_FALLBACK).strip()
    user_id = (h.get("x-user-id") or DEFAULT_USER_FALLBACK).strip()
    role = (h.get("x-role") or DEFAULT_ROLE_FALLBACK).strip()
    return tenant_id, user_id, role


def _new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:18]


def _new_session_id() -> str:
    return "sess_" + uuid.uuid4().hex[:18]


# ── lifespan: build graph + registry once ───────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Build the engine once at startup. The compiled graph is stateless;
    every per-request invocation gets its own thread_id."""
    t0 = time.monotonic()
    # Load `.env` once if present — uvicorn doesn't read it automatically and
    # the status chips depend on env vars (POSTGRES_URL, LLM_GATEWAY_URL,
    # NATS_URL, OTEL_EXPORTER_OTLP_ENDPOINT) to report honest config state.
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=False)
    except ImportError:
        pass
    registry = load_registry("registries/v2")
    # Production-grade lifecycle visibility (2026-05-31). One log line per kind
    # at boot — operators see "active=4 deprecated=0 retired=0 draft=0" in
    # journalctl + Tempo. Every subsequent activate/deprecate/retire transition
    # emits a `registry.lifecycle.transition` event on the same logger.
    try:
        registry.emit_boot_lifecycle_log()
    except Exception:                                                  # noqa: BLE001
        pass
    # ── LLM Gateway built FIRST so disambiguator selection is a
    # construction-time decision (not a post-hoc private-attr patch).
    gateway = _build_llm_gateway()
    chosen_model = os.getenv("LLM_DEFAULT_MODEL", "gpt-4o-mini").strip()

    # Router substrate. Stage-2 retrieval is the inverted-index lexical one
    # (G3 load-tested at 1000 agents); stage-4 disambiguation rides the LLM
    # when the gateway is up — that's the layer that survives misspellings,
    # paraphrases, casual grammar. Without the gateway, the deterministic
    # ThresholdDisambiguator is the safe fallback.
    from oneops.authz.service import AuthzService
    from oneops.router.disambiguation import (
        LlmDisambiguator,
        ThresholdDisambiguator,
    )
    from oneops.router.glossary import Glossary
    from oneops.router.retrieval import LexicalRetriever
    retriever = LexicalRetriever(registry)
    # Stage-2 retriever selection. Default = lexical (reads the file registry,
    # zero infra). Flag ONEOPS_ROUTER_RETRIEVER=pgvector switches to DB-backed
    # kNN over ai.embeddings_agent (retrieve-then-decide). Additive + reversible:
    # any setup failure logs the reason and falls back to lexical, never blocking
    # startup (§2.7). Query is embedded through the gateway (§2.5).
    app.state.router_retriever_pool = None
    # Default = pgvector (DB-backed agent embeddings) — proven non-regressive vs
    # lexical and ~2.4x better on unseen/paraphrased queries; required for ITOM
    # scale. Falls back to lexical automatically if the DB/gateway is absent
    # (set ONEOPS_ROUTER_RETRIEVER=lexical to force the in-process retriever).
    if os.getenv("ONEOPS_ROUTER_RETRIEVER", "pgvector").strip().lower() in ("pgvector", "db"):
        try:
            if gateway is None:
                raise RuntimeError("ONEOPS_ROUTER_RETRIEVER=pgvector requires the LLM gateway")
            import asyncpg as _asyncpg

            from oneops.router.retrieval import (
                GatewayEmbedder,
                PgVectorRetriever,
                configure_hnsw_connection,
            )
            # init= tunes pgvector HNSW (iterative_scan + ef_search) on every
            # pooled connection so the filtered ANN query is correct at scale.
            _emb_pool = await _asyncpg.create_pool(
                os.environ["POSTGRES_URL"], min_size=1, max_size=4,
                init=configure_hnsw_connection)
            app.state.router_retriever_pool = _emb_pool
            from oneops.router.route_cache import build_query_embedding_cache
            _emb_cache = build_query_embedding_cache()
            retriever = PgVectorRetriever(
                registry,
                embedder=GatewayEmbedder(gateway, cache=_emb_cache),
                pool=_emb_pool)
            _log.info("oneops.api.router_retriever", kind="pgvector",
                      table="ai.embeddings_agent")
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("oneops.api.router_retriever_fallback",
                         error=str(exc)[:160], note="falling back to lexical retriever")
            retriever = LexicalRetriever(registry)
    else:
        _log.info("oneops.api.router_retriever", kind="lexical")
    glossary = Glossary.from_file()
    authz = AuthzService.create()
    if gateway is not None:
        # Abstain gate config (wrong-agent guard, ITOM-scale). Unset = OFF
        # (no ITSM regression). Set ONEOPS_ROUTER_ABSTAIN_MIN_SCORE (e.g. 0.45
        # for pgvector cosine) + optionally _MIN_MARGIN to refuse-and-clarify
        # on weak/ambiguous matches instead of guessing among look-alikes.
        _abstain_score = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
        disambiguator = LlmDisambiguator(
            gateway, model=chosen_model, registry=registry,
            abstain_min_score=(float(_abstain_score) if _abstain_score else None),
            abstain_min_margin=float(
                os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_MARGIN", "0.0") or "0.0"))
        # Rewriter selection — when the LLM gateway is up, use the LLM
        # rewriter (resolves pronouns / back-references against
        # conversation_history). Without it, multi-turn "what is the
        # priority of it" cannot work — entity carries no anchor.
        from oneops.router.rewrite import LlmRewriter
        rewriter = LlmRewriter(gateway, model=chosen_model)
        # Decomposer selection — LLM-backed so compound messages
        # ("summarize INC0001001 and INC0001002") split into N atomic
        # sub-queries. v4 product shape: a single message can carry N
        # sub-queries routed to N UCs (single_engine_multi_subquery).
        # Without it, the router collapses every compound message to one
        # sub-query and only the first entity gets served.
        from oneops.router.decompose import LlmDecomposer
        decomposer = LlmDecomposer(gateway, model=chosen_model)
        # Latency (RCA 2026-06-09): when ONEOPS_ROUTER_MERGE_DECOMPOSE_REWRITE
        # is on, inject an LlmUnifiedSplitter — ONE LLM call that does
        # reference-resolution + splitting, replacing the decompose call + the
        # speculative rewrite. Default off ⇒ unchanged two-call path.
        from oneops.router.decompose import (
            LlmUnifiedSplitter,
            merge_decompose_rewrite_enabled,
        )
        unified_splitter = (
            LlmUnifiedSplitter(gateway, model=chosen_model)
            if merge_decompose_rewrite_enabled() else None)
    else:
        from oneops.router.decompose import PassthroughDecomposer
        from oneops.router.rewrite import PassthroughRewriter
        disambiguator = ThresholdDisambiguator()
        rewriter = PassthroughRewriter()
        decomposer = PassthroughDecomposer()
        unified_splitter = None
    from oneops.router.route_cache import build_route_decision_cache
    _route_cache = build_route_decision_cache()
    router = Router(
        registry=registry, glossary=glossary, retriever=retriever,
        disambiguator=disambiguator, authz=authz,
        rewriter=rewriter, decomposer=decomposer,
        unified_splitter=unified_splitter,
        route_cache=_route_cache)
    _log.info("oneops.api.route_cache_wired",
              backend=(type(_route_cache).__name__ if _route_cache else "off"),
              unified_split=(unified_splitter is not None))
    dispatcher = FastPathDispatcher(registry)

    # ── HandlerResolver — registry of tool handlers ────────────────────
    # Production swap (FaaS deployment): handlers register from their own
    # Lambda packages. Here we use the in-process module-import path; the
    # resolver's `import` fallback walks `module:function` refs, so every
    # tool we declared in registries/v2/tools/*.json wires up automatically.
    resolver = HandlerResolver()
    local_step_executor = HandlerStepExecutor(registry=registry, resolver=resolver)
    # AGENT_TRANSPORT=nats routes step dispatch through `oneops.agent.<id>`
    # so agent-to-agent traffic shows up on the bus (and in nats logs).
    # Local (in-process) is the default for tests + single-process dev.
    agent_transport = os.getenv("AGENT_TRANSPORT", "local").strip().lower()
    if agent_transport == "nats":
        from oneops.executor.nats_step_executor import NatsStepExecutor
        step_executor = NatsStepExecutor()
    else:
        step_executor = local_step_executor

    # ── SessionEventStore — durable conversation memory ───────────────
    # InMemory backends survive the process lifetime, which is what a
    # single-process FaaS dev / demo needs. Production swaps the cold log
    # for `PostgresEventLog` and the hot window for `DragonflyHotWindow`
    # via env (the same Protocol seams) — no caller changes.
    session_store = _build_session_store()

    # ── LLM Gateway — E2: wire `summarize_entity` to the live LLM ────
    # The gateway is the single egress for every model call (quota,
    # redaction, retry, fallback, cost). Today we wire it for UC-1; future
    # UCs that need LLM calls reuse the same instance.
    if gateway is not None:
        from oneops.executor.boundary import LlmBoundaryResponder
        from oneops.use_cases.uc01_summarization.cache import (
            get_summary_cache_store,
        )
        from oneops.use_cases.uc01_summarization.llm_summarizer import (
            build_cached_summarize_fn,
        )
        from oneops.use_cases.uc01_summarization.tools import (
            set_summarize_llm,
        )
        # Cache-aside: tenant-partitioned summary cache wraps the gateway
        # call. Repeat queries → cache hit → zero LLM cost, ~5ms latency.
        set_summarize_llm(build_cached_summarize_fn(
            gateway, cache_store=get_summary_cache_store(),
            model=chosen_model))
        # UC-2 per-result discriminator label LLM (rule §UC-2 trust UX,
        # 2026-05-31). One batched call per UC-2 request; both button and
        # chat paths inherit it because both land in `tools.find_similar_
        # entities` → `core.find_similar`.
        from oneops.use_cases.uc02_similar_tickets.tools import (
            set_discriminator_llm,
        )
        set_discriminator_llm(gateway, chosen_model)
        # Field-read LLM — extracts which Key Detail labels the user is
        # asking for on a follow-up question ("what is the priority of
        # it?"). Returns [] for full-summary or unrelated messages, so
        # the handler falls through to summarise.
        from oneops.use_cases.uc01_summarization.field_read import (
            LlmFieldReadExtractor,
            set_field_read_llm,
        )
        _fr = LlmFieldReadExtractor(gateway, model=chosen_model)
        async def _field_read_call(msg, labels, tenant, model, *, user_id=""):
            return await _fr.extract(msg, labels, tenant, model,
                                     user_id=user_id)
        set_field_read_llm(_field_read_call)
        # Embedding-based field matcher (Stage 3, 2026-05-29). Runs
        # BEFORE the LLM extractor. Pure semantic — replaces the
        # keyword `_SYNONYMS` table with cosine similarity against
        # field descriptions. Fail-OPEN: any embedding error and the
        # turn falls through to the LLM extractor above.
        from oneops.use_cases.uc01_summarization.field_embedder import (
            build_field_embedder,
            set_field_embedder,
        )
        set_field_embedder(build_field_embedder(gateway))
        # Entity elicitation (slot-filling) — the gateway powers contextual
        # reply resolution ("my last ticket"). Wired here so the executor's
        # flag-gated gate has an egress; unset on shutdown below.
        from oneops.executor.entity_elicitation import set_elicitation_gateway
        set_elicitation_gateway(gateway)
        # Conversational boundary — classifies non-routed turns and emits
        # the right reply per category. Out-of-scope literal is enforced
        # server-side. Disambiguator is already an LlmDisambiguator (set
        # at router construction above), so all three LLM-backed seams
        # are wired in one consistent place.
        app.state.gateway = gateway
        app.state.boundary = LlmBoundaryResponder(
            gateway, model=chosen_model)
        # UC-3 KB query-embedding fn — semantic-search path. Uses the
        # same gateway egress as chat calls (per-tenant cost, OTel
        # spans, retries, LiteLLM proxy routing). The 1536-d default
        # matches the dimensionality stored on `itsm.kb_knowledge`.
        from oneops.use_cases.uc03_kb_lookup.kb_embed import (
            build_cached_embed_fn,
            set_kb_embed_fn,
        )
        embed_model = os.getenv(
            "LLM_EMBED_MODEL", "text-embedding-3-large").strip()
        embed_dims_env = os.getenv("LLM_EMBED_DIMENSIONS", "1536").strip()
        embed_dims = int(embed_dims_env) if embed_dims_env else None
        set_kb_embed_fn(build_cached_embed_fn(
            gateway, model=embed_model, dimensions=embed_dims))
        # Phase 5b faithfulness gate — scores (query, composed answer)
        # via embedding cosine similarity. The handler thresholds
        # against `UC03_MIN_ANSWER_RELEVANCE_SCORE` and falls back to
        # CASE B when the answer drifts from the query subject.
        from oneops.use_cases.uc03_kb_lookup.kb_embed import (
            build_relevance_scorer,
            set_kb_relevance_scorer,
        )
        set_kb_relevance_scorer(build_relevance_scorer(
            gateway, model=embed_model, dimensions=embed_dims))
        # UC-3 grounded answer composer (Phase 3, CASE A/B). After
        # search_kb returns hits, the composer writes a faithful,
        # citation-bearing reply — never fabricated, explicit "no
        # match" path for CASE B. Same gateway egress (OTel + cost +
        # retries + LiteLLM proxy).
        from oneops.use_cases.uc03_kb_lookup.answer_composer import (
            LlmAnswerComposer,
            set_kb_answer_composer,
        )
        set_kb_answer_composer(LlmAnswerComposer(gateway, model=chosen_model))
        # Stage-1 conversation-control gate (pre-router). Same gateway
        # egress → OTel + cost tracking + retries + LiteLLM proxy. The
        # gate caches verdicts in Dragonfly so repeat greetings/thanks
        # cost zero tokens.
        from oneops.conversation.control_gate import (
            LlmControlClassifier,
            set_control_classifier,
        )
        # Control gate runs on a STRONGER model than the reranker default: it's
        # a nuanced classification (enterprise IT how-to vs off-domain), and
        # gpt-4o-mini over-refused how-to (wifi/macbook/teams/slack) as
        # out_of_scope — gpt-4o scored 100/100 on the control-gate eval vs
        # mini's 89/100. Tiny call (max 8 tokens), 7-day Dragonfly-cached, so
        # the cost/latency impact is minimal. Override via env.
        _control_gate_model = (
            os.getenv("ONEOPS_CONTROL_GATE_MODEL", "gpt-4o").strip()
            or chosen_model)
        set_control_classifier(
            LlmControlClassifier(gateway, model=_control_gate_model))
        _log.info("oneops.api.llm_gateway_wired",
                  model=chosen_model, embed_model=embed_model,
                  cache_aside=True,
                  conversational_boundary=True,
                  llm_disambiguator=True,
                  kb_embed=True)
    else:
        app.state.gateway = None
        app.state.boundary = None
        _log.info("oneops.api.llm_gateway_skipped",
                  reason="LLM_GATEWAY_URL not configured")

    # ── Checkpointer selection ──────────────────────────────────────────
    # LANGGRAPH_CHECKPOINTER=postgres → durable AsyncPostgresSaver against
    # a DEDICATED database (ADR-0004). Any other value (or unset) → the
    # in-memory `InMemorySaver` baked into `build_executor_graph`.
    # Production-grade: a misconfigured Postgres URL is a HARD boot
    # failure, never a silent fallback to MemorySaver (that would mask
    # data loss on the first restart).
    checkpointer_mode = (os.getenv("LANGGRAPH_CHECKPOINTER", "memory")
                         .strip().lower())
    checkpointer = None
    app.state.checkpointer_mode = checkpointer_mode
    if checkpointer_mode == "postgres":
        from oneops.executor.graph import build_postgres_checkpointer
        try:
            checkpointer = await build_postgres_checkpointer()
            _log.info("oneops.api.checkpointer_selected",
                      backend="postgres", durable=True)
        except Exception as exc:                          # noqa: BLE001 — boot gate
            raise RuntimeError(
                f"LANGGRAPH_CHECKPOINTER=postgres but the checkpoint "
                f"database is unreachable / setup failed: {exc}. "
                f"Either fix LANGGRAPH_POSTGRES_URL or set "
                f"LANGGRAPH_CHECKPOINTER=memory."
            ) from exc
    else:
        _log.info("oneops.api.checkpointer_selected",
                  backend="memory", durable=False)

    graph = build_executor_graph(
        router=router,
        registry=registry,
        step_executor=step_executor,
        # AuthZ is wired so `builtin:authz_recheck` (G5) actually fires —
        # UC-1 declares it in `before_invocation`. Without this wiring the
        # hook would HookError loud (refused-silent-skip).
        authz_service=authz,
        # Session store: conversation history persists across turns under
        # the same `session_id`. The frontend keeps that id in localStorage,
        # so reload preserves the conversation.
        session_store=session_store,
        # Conversational boundary (when LLM is wired); falls back to the
        # deterministic responder otherwise.
        boundary=getattr(app.state, "boundary", None),
        # Conversation trimmer (G2): bounds history per turn so long
        # sessions don't grow the LLM prompt without limit. Production
        # uses `TokenBudgetTrimmer` with an LLM-backed summariser when
        # the gateway is wired; falls back to NoopTrimmer when there's
        # no LLM (tests / pre-init startup).
        conversation_trimmer=_build_conversation_trimmer(gateway, chosen_model),
        # Durable checkpointer when postgres mode is on; else builder
        # falls back to InMemorySaver.
        checkpointer=checkpointer,
        # Focus-intent classifier — small LLM call that drops focus on
        # explicit topic-search turns BEFORE the disambiguator runs. Enabled
        # by env flag so it can be turned off without code change. When the
        # gateway is unwired we never instantiate it; behaviour falls back
        # to legacy focus-carry.
        focus_intent_classifier=(
            _build_focus_intent_classifier(gateway)
            if gateway is not None
            and os.getenv("FOCUS_INTENT_CLASSIFIER_ENABLED", "true").strip().lower() != "false"
            else None
        ),
        # Conditional TimeFilter extractor — fires only when the plan
        # contains an agent whose registry record sets
        # `consumes_time_filter: true` (currently UC-2 only). Env-gated so
        # operators can disable without code changes.
        time_filter_extractor=(
            _build_time_filter_extractor(gateway)
            if os.getenv("TIME_FILTER_EXTRACTOR_ENABLED", "true").strip().lower() != "false"
            else None
        ),
    )
    app.state.session_store = session_store
    app.state.graph = graph
    app.state.registry = registry
    app.state.dispatcher = dispatcher

    # ── Worker role + invoker mode (multi-tenant split topology) ────────
    # WORKER_ROLE selects which NATS subscriptions this process attaches:
    #   all          - single-process demo: graph worker + every agent
    #                  worker co-located (the original behaviour).
    #   ingress      - public HTTP/WebSocket entry only. Publishes to
    #                  NATS, holds NO worker subscriptions. /api/chat
    #                  routes through nats_invoke.
    #   graph_worker - graph orchestrator only. Subscribes to
    #                  oneops.request.chat; dispatches to agent subjects.
    #   agent_worker - one specific agent. Requires AGENT_ID env to
    #                  name the registry agent (e.g. uc01_summarization).
    # Production topology runs one container per role; ingress can scale
    # independently of graph_worker which can scale independently of each
    # agent_worker. NATS queue groups load-balance replicas within a role.
    worker_role = os.getenv("WORKER_ROLE", "all").strip().lower()
    invoker_mode = os.getenv("UC_INVOKER_MODE", "local").strip().lower()
    app.state.worker_role = worker_role
    app.state.invoker_mode = invoker_mode
    app.state.graph_worker = None

    # When this process publishes to NATS (any role except a hypothetical
    # pure-local one), require the connection up front so a misconfigured
    # cluster surfaces at boot, never at first turn.
    if invoker_mode == "nats" or worker_role in ("graph_worker", "agent_worker"):
        from oneops.adapters.nats_client import get_nats_client
        try:
            await get_nats_client()
        except Exception as exc:                          # noqa: BLE001 — boot gate
            raise RuntimeError(
                f"NATS is unreachable at boot: {exc}. "
                f"Either start NATS at NATS_URL or set "
                f"UC_INVOKER_MODE=local + WORKER_ROLE=all for a local "
                f"single-process demo."
            ) from exc

    # Graph-worker subscription — attached when WORKER_ROLE includes it.
    if worker_role in ("all", "graph_worker") and invoker_mode == "nats":
        from oneops.workers.graph_worker import GraphWorker
        worker = GraphWorker(graph)
        await worker.start()
        app.state.graph_worker = worker
        _log.info("oneops.api.graph_worker_attached", role=worker_role)
    elif worker_role == "ingress":
        _log.info("oneops.api.graph_worker_skipped",
                  role=worker_role,
                  note="ingress publishes only; graph_worker is a separate process")
    else:
        _log.info("oneops.api.invoker_mode_selected", mode=invoker_mode)

    # Agent-worker subscriptions — attached when WORKER_ROLE includes them.
    app.state.agent_workers = []
    if agent_transport == "nats":
        if worker_role == "all":
            from oneops.workers.agent_worker import AgentWorker
            active_agents = registry.agents.list_active()
            for agent in active_agents:
                w = AgentWorker(agent.id, local_step_executor)
                await w.start()
                app.state.agent_workers.append(w)
            _log.info("oneops.api.agent_workers_attached",
                      role=worker_role,
                      agents=[a.id for a in active_agents])
        elif worker_role == "agent_worker":
            from oneops.workers.agent_worker import AgentWorker
            target_agent_id = (os.getenv("AGENT_ID") or "").strip()
            if not target_agent_id:
                raise RuntimeError(
                    "WORKER_ROLE=agent_worker requires AGENT_ID env to be "
                    "set to a registered agent id (e.g. uc01_summarization).")
            agent_record = registry.agents.get_optional(target_agent_id)
            if agent_record is None or agent_record.status.value != "active":
                raise RuntimeError(
                    f"AGENT_ID={target_agent_id!r} is not an active agent "
                    f"in the registry; cannot start agent_worker.")
            w = AgentWorker(target_agent_id, local_step_executor)
            await w.start()
            app.state.agent_workers.append(w)
            _log.info("oneops.api.agent_workers_attached",
                      role=worker_role, agents=[target_agent_id])
        elif worker_role in ("ingress", "graph_worker"):
            _log.info("oneops.api.agent_workers_skipped",
                      role=worker_role,
                      note="agent workers run as their own processes")
    else:
        _log.info("oneops.api.agent_transport_selected", transport="local")

    # ── UC-5 Triage wiring (Phase 3b — executor-only propose) ───────────
    # Propose runs on the MAIN executor (registry tools, like every other UC);
    # the bespoke runner/graph were retired. The NATS triage worker now serves
    # the DECIDE (apply) hop only.
    app.state.uc05_agent = None
    try:
        if gateway is not None:
            from oneops.api.uc05_routes import (
                get_ticket_store as _uc05_get_store,
            )

            # Store selection: default JsonFixtureStore (demo data); opt into the
            # real Postgres-backed store (itsm.incident / itsm.request reads +
            # triage-apply writes) with UC05_TICKET_STORE=postgres. Set BEFORE any
            # _uc05_get_store() call so the executor handlers + the NATS decide
            # worker share the same backend.
            if os.getenv("UC05_TICKET_STORE", "").strip().lower() == "postgres":
                from oneops.api.uc05_routes import (
                    set_ticket_store as _uc05_set_ticket_store,
                )
                from oneops.use_cases.uc05_triage.stores import DbStore
                _uc05_set_ticket_store(DbStore())
                _log.info("oneops.api.uc05_store_selected", backend="postgres")

            async def _uc05_conn_provider():
                import asyncpg
                pg_url = os.getenv("POSTGRES_URL")
                if not pg_url:
                    raise RuntimeError(
                        "POSTGRES_URL not set for Triage (UC-5) handlers")
                return await asyncpg.connect(pg_url)

            # Wire the registry-dispatched UC-5 tool handlers — the executor
            # dispatches these for propose (check → assign ∥ prio → assemble).
            from oneops.use_cases.uc05_triage.handlers import (
                set_uc05_connection_provider as _uc05_set_cp,
            )
            from oneops.use_cases.uc05_triage.handlers import (
                set_uc05_gateway as _uc05_set_gw,
            )
            from oneops.use_cases.uc05_triage.handlers import (
                set_uc05_ticket_store as _uc05_set_store,
            )
            _uc05_set_gw(gateway)
            _uc05_set_cp(_uc05_conn_provider)
            _uc05_set_store(_uc05_get_store())
            _log.info("oneops.api.uc05_handlers_wired")

            # /api/uc05/propose → MAIN executor (the only propose path).
            from oneops.api.uc05_routes import (
                set_executor_propose_runner as _uc05_set_exec_runner,
            )
            from oneops.use_cases.uc05_triage.executor_runner import (
                make_executor_propose_runner,
            )
            _uc05_set_exec_runner(make_executor_propose_runner(graph))
            _log.info("oneops.api.uc05_executor_propose_enabled")

            # Start the NATS triage DECIDE worker (apply path) — propose does NOT
            # go over NATS; it runs on the executor above. If NATS is down the
            # route falls back to in-process apply (graceful).
            if invoker_mode == "nats":
                try:
                    from oneops.adapters.nats_client import get_nats_client
                    from oneops.api.uc05_routes import (
                        set_decide_dispatcher as _uc05_set_decide_dispatcher,
                    )
                    from oneops.use_cases.uc05_triage.agent import TriageAgent
                    from oneops.use_cases.uc05_triage.nats_dispatcher import (
                        dispatch_decide as _uc05_dispatch_decide,
                    )
                    nats_client = await get_nats_client()
                    agent = TriageAgent(
                        nats=nats_client, store=_uc05_get_store(),
                    )
                    await agent.start()
                    app.state.uc05_agent = agent

                    async def _decide_over_nats(*, proposal, proposal_id, choice,
                                                actor_user_id, final_values):
                        return await _uc05_dispatch_decide(
                            nats=nats_client, proposal=proposal,
                            proposal_id=proposal_id, choice=choice,
                            actor_user_id=actor_user_id,
                            final_values=final_values,
                        )

                    _uc05_set_decide_dispatcher(_decide_over_nats)
                    _log.info("oneops.api.uc05_agent_started", dispatch="nats")
                except Exception as exc:                          # noqa: BLE001
                    _log.warning("oneops.api.uc05_agent_failed",
                                  error=str(exc)[:160])
    except Exception as exc:                                      # noqa: BLE001
        _log.warning("oneops.api.uc05_wiring_failed",
                      error=str(exc)[:160])

    # ── UC-8 Fulfillment NATS agent wiring ──────────────────────────────
    # Starts the fulfilment-engine worker (runs the task DAG over NATS).
    # UC-8 is chat-only; the worker is triggered by the chat
    # create_service_request tool (Step 2). Graceful skip if NATS is down.
    try:
        import os as _os

        import asyncpg as _asyncpg

        from oneops.adapters.nats_client import get_nats_client
        from oneops.use_cases.uc08_fulfillment import tools as _uc08_tools
        from oneops.use_cases.uc08_fulfillment.adapters.inprocess import (
            InProcessIntegrationAdapter,
        )
        from oneops.use_cases.uc08_fulfillment.agent import (
            UC8FulfillmentAgent,
        )
        from oneops.use_cases.uc08_fulfillment.executor import execute_plan

        nats_client_uc08 = await get_nats_client()

        async def _uc08_cp():
            return await _asyncpg.connect(_os.environ["POSTGRES_URL"])

        uc08_agent = UC8FulfillmentAgent(
            nats=nats_client_uc08,
            execute_plan=execute_plan,
            connection_provider=_uc08_cp,
            adapter_factory=InProcessIntegrationAdapter,
        )
        await uc08_agent.start()
        app.state.uc08_agent = uc08_agent
        # The fulfilment engine worker (runs the task DAG). Its trigger is the
        # chat create_service_request tool (Step 2), not the removed REST route.
        app.state.uc08_nats = nats_client_uc08
        # Wire the 4 chat catalog tools: the embedding gateway powers
        # get_service_request_list (single egress, §2.5); the NATS client lets
        # create_service_request dispatch fulfilment to the worker above.
        _uc08_tools.set_gateway(gateway)
        _uc08_tools.set_nats_client(nats_client_uc08)
        _log.info("oneops.api.uc08_agent_started", dispatch="nats",
                  chat_tools="wired")
    except Exception as exc:                                      # noqa: BLE001
        _log.warning("oneops.api.uc08_wiring_failed",
                      error=str(exc)[:160])

    # ── UC-2 Similar Tickets runner wiring ──────────────────────────────
    # Mirror of UC-5: in-process by default, NATS-swappable later. Same
    # find_similar() backs both the button route and (later) chat handler,
    # which is the structural guarantee that results are identical.
    try:
        from oneops.api.uc02_routes import (
            set_result_cache as _uc02_set_result_cache,
        )
        from oneops.api.uc02_routes import (
            set_similar_runner as _uc02_set_runner,
        )
        from oneops.use_cases.uc02_similar_tickets.core import (
            find_similar as _uc02_find_similar,
        )

        async def _uc02_conn_provider():
            import asyncpg
            pg_url = os.getenv("POSTGRES_URL")
            if not pg_url:
                raise RuntimeError(
                    "POSTGRES_URL not set for Similar Tickets (UC-2) runner")
            return await asyncpg.connect(pg_url)

        # Read the discriminator gateway/model wired earlier in lifespan so
        # the button-route path gets the same per-result trust labels as the
        # chat path (which goes through `tools.find_similar_entities`).
        # Use module-attribute lookup (not `from ... import x`) so a later
        # `set_discriminator_llm` re-bind would still be picked up.
        from oneops.use_cases.uc02_similar_tickets import (
            tools as _uc02_tools_module,
        )

        async def _uc02_runner(**kwargs):
            return await _uc02_find_similar(
                connection_provider=_uc02_conn_provider,
                discriminator_gateway=_uc02_tools_module._discriminator_gateway,
                discriminator_model=_uc02_tools_module._discriminator_model,
                **kwargs,
            )

        _uc02_set_runner(_uc02_runner)

        # Wire the Dragonfly result cache if the chat-turn cache backend
        # is the Dragonfly one — reuse the same client. Falls back to
        # in-memory dict otherwise.
        ctc = getattr(app.state, "chat_turn_cache", None)
        # Cache will be wired AFTER chat_turn_cache builds below; set None now
        # and revisit. The order below is intentional — UC-2 cache binds after.
        _uc02_set_result_cache(getter=None, putter=None)
        _log.info("oneops.api.uc02_runner_attached")
    except Exception as exc:                                       # noqa: BLE001
        _log.warning("oneops.api.uc02_wiring_failed",
                      error=str(exc)[:160])

    # Turn-level chat-response cache (semantic, no keywords). On a cache
    # hit, /api/chat short-circuits the full routing pipeline and returns
    # the prior response. Default in-memory; set CHAT_TURN_CACHE_BACKEND=
    # dragonfly for production durable cache.
    try:
        from oneops.api.chat_turn_cache import build_cache as _build_chat_cache
        app.state.chat_turn_cache = _build_chat_cache()
        # Surface TTL in boot log so operators verify what's active without
        # grepping env-files. Defensive: pull from the live cache instance.
        _ttl_s = getattr(app.state.chat_turn_cache, "_ttl", None)
        _log.info("oneops.api.chat_turn_cache_wired",
                  backend=type(app.state.chat_turn_cache).__name__,
                  ttl_seconds=_ttl_s,
                  ttl_minutes=(_ttl_s // 60) if isinstance(_ttl_s, int) else None)

        # ── Semantic turn cache (cross-session consistency, 2026-06-02) ──
        # Standalone chat queries get a deterministic embedding-similarity
        # cache so the SAME query returns the SAME answer regardless of the
        # routing pipeline's LLM non-determinism (the documented "temp=0 is
        # only mostly deterministic" problem). Reuses the chat-turn cache's
        # Dragonfly client + the wired KB embedder; disabled gracefully if
        # either is unavailable (the turn just runs the pipeline as before).
        app.state.semantic_cache = None
        try:
            from oneops.api.semantic_turn_cache import SemanticTurnCache
            from oneops.use_cases.uc03_kb_lookup.kb_embed import get_kb_embed_fn
            _sem_redis = getattr(app.state.chat_turn_cache, "_redis", None)
            _sem_embed = get_kb_embed_fn()
            if _sem_redis is not None and _sem_embed is not None:
                _sem_ttl = int(os.getenv("SEMANTIC_TURN_CACHE_TTL_S", "600"))
                app.state.semantic_cache = SemanticTurnCache(
                    redis=_sem_redis, embed=_sem_embed, ttl_seconds=_sem_ttl)
                _log.info("oneops.api.semantic_turn_cache_wired",
                          ttl_seconds=_sem_ttl)
            else:
                _log.info("oneops.api.semantic_turn_cache_skipped",
                          have_redis=_sem_redis is not None,
                          have_embed=_sem_embed is not None)
        except Exception as exc:                               # noqa: BLE001
            _log.warning("oneops.api.semantic_turn_cache_wire_failed",
                         error=str(exc)[:160])

        # UC-2 result cache rides on the same backend (key prefix
        # 'uc02:sim:' keeps it disjoint from chat-turn entries).
        try:
            ctc = app.state.chat_turn_cache
            from oneops.api.uc02_routes import (
                set_result_cache as _uc02_set_result_cache,
            )
            _uc02_set_result_cache(getter=ctc.get, putter=ctc.put)
            _log.info("oneops.api.uc02_cache_wired",
                      backend=type(ctc).__name__)
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("oneops.api.uc02_cache_bind_failed",
                          error=str(exc)[:160])
    except Exception as exc:                                       # noqa: BLE001
        _log.warning("oneops.api.chat_turn_cache_failed",
                      error=str(exc)[:160])
        app.state.chat_turn_cache = None

    # Embedding refresh workers are now SEPARATE per-service processes (one queue
    # + one worker per service, no shared lane / no head-of-line blocking). They
    # live in database/<service>/worker.py and run on their own:
    #     python database/incident/worker.py
    #     python database/agent/worker.py    ... etc.
    # The API process therefore does NOT start an in-process worker. The
    # attribute is kept (None) so the shutdown path stays a no-op.
    app.state.embedding_worker = None
    _log.info("oneops.api.embedding_worker_external",
              note="per-service workers run as database/<service>/worker.py processes")

    # Dashboard priming: emit one `ai.agent.runs.total{agent_id=<id>}` sample
    # per registered agent at boot so the Grafana "Active agents" counter
    # reflects the REGISTRY truth (4 agents wired today) instead of only
    # those that happened to fire within the last 30-minute window. Without
    # this, idle UCs (UC-3, UC-5) silently drop off the dashboard between
    # demos.
    #
    # We emit +1 (not +0) because the Prometheus exporter only materialises
    # a time-series when the counter has been incremented; `add(0)` is a
    # no-op in many SDKs. The +1 per boot adds an immaterial 4 samples per
    # restart — not worth a special-case filter.
    try:
        for _a in registry.agents.list_active():
            _metric_inc("ai.agent.runs.total", 1,
                        agent_id=_a.id, tenant_id="_boot",
                        source="dashboard_priming",
                        status="boot")
    except Exception:                                                  # noqa: BLE001
        pass

    _log.info(
        "oneops.api.ready",
        boot_ms=int((time.monotonic() - t0) * 1000),
        active_agents=registry.active_agent_count(),
    )
    try:
        yield
    finally:
        # Stop UC-5 triage agent if it was started.
        try:
            if getattr(app.state, "uc05_agent", None) is not None:
                await app.state.uc05_agent.stop()
                _log.info("oneops.api.uc05_agent_stopped")
        except Exception:
            pass

        # Close the UC-5 ticket-store pool if a DbStore opened one (no-op for
        # the JSON fixture store, which has no close()).
        try:
            from oneops.api.uc05_routes import get_ticket_store as _uc05_gs
            _uc05_store = _uc05_gs()
            if hasattr(_uc05_store, "close"):
                await _uc05_store.close()  # type: ignore[func-returns-value]
                _log.info("oneops.api.uc05_store_closed")
        except Exception as exc:                # noqa: BLE001 — shutdown discipline
            _log.warning("oneops.api.uc05_store_close_failed", error=str(exc))

        # Stop embedding refresh worker if it was started.
        try:
            if getattr(app.state, "embedding_worker", None) is not None:
                await app.state.embedding_worker.stop()
                _log.info("oneops.api.embedding_worker_stopped")
        except Exception:
            pass

        # Close the router retriever pool if the pgvector retriever opened one.
        try:
            if getattr(app.state, "router_retriever_pool", None) is not None:
                await app.state.router_retriever_pool.close()
                _log.info("oneops.api.router_retriever_pool_closed")
        except Exception:
            pass

        # Close the asyncpg pool if a Postgres ticket store opened one.
        # Best-effort: skip on any failure so shutdown stays clean.
        try:
            from oneops.use_cases._shared.ticket_store import (
                get_ticket_store,
            )
            store = get_ticket_store()
            if hasattr(store, "close"):
                await store.close()  # type: ignore[func-returns-value]
        except Exception as exc:                # noqa: BLE001 — shutdown discipline
            _log.warning("oneops.api.shutdown_cleanup_failed", error=str(exc))
        # Close the LLM transport's HTTP client if we opened it.
        try:
            gw = getattr(app.state, "gateway", None)
            if gw is not None:
                transport = getattr(gw, "_transport", None)
                if transport is not None and hasattr(transport, "aclose"):
                    await transport.aclose()
        except Exception as exc:                # noqa: BLE001 — shutdown discipline
            _log.warning("oneops.api.llm_shutdown_cleanup_failed",
                         error=str(exc))
        # Drain the embedded GraphWorker and close NATS connection.
        try:
            worker = getattr(app.state, "graph_worker", None)
            if worker is not None:
                await worker.stop()
            for aw in getattr(app.state, "agent_workers", []) or []:
                await aw.stop()
            # Close the checkpointer's owned Postgres pool, if any.
            try:
                graph_obj = getattr(app.state, "graph", None)
                ckp = getattr(graph_obj, "checkpointer", None) if graph_obj else None
                owned_pool = getattr(ckp, "_owned_pool", None)
                if owned_pool is not None:
                    await owned_pool.close()
            except Exception as exc:                # noqa: BLE001 — shutdown discipline
                _log.warning("oneops.api.checkpointer_pool_close_failed",
                             error=str(exc))
            if getattr(app.state, "invoker_mode", "local") == "nats":
                from oneops.adapters.nats_client import shutdown_nats_client
                await shutdown_nats_client()
        except Exception as exc:                # noqa: BLE001 — shutdown discipline
            _log.warning("oneops.api.nats_shutdown_cleanup_failed",
                         error=str(exc))


# ── app factory ─────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    return build_app()


def build_app() -> FastAPI:
    app = FastAPI(
        title="OneOps API",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )

    # CORS — open in dev (any origin) so a separately-hosted frontend or a
    # local SDK can call the engine. Production tightens this to the known
    # origins list at the upstream API Gateway layer.
    cors_origins_env = os.getenv("ONEOPS_CORS_ORIGINS", "*").strip()
    cors_origins = (
        ["*"] if cors_origins_env == "*"
        else [o.strip() for o in cors_origins_env.split(",") if o.strip()]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id", "x-trace-id"],
    )

    # ── UC-5 Triage routes ────────────────────────────────────────────
    # Section J — mounted on the same FastAPI app that serves UC-1 / UC-3
    # so the existing frontend (with role + tenant switcher in headers)
    # can call /api/uc05/* without any new auth wiring.
    from oneops.api.uc05_routes import router as _uc05_router
    app.include_router(_uc05_router)

    # ── UC-2 Similar Tickets — button + chat both land at find_similar() ───
    from oneops.api.uc02_routes import router as _uc02_router
    app.include_router(_uc02_router)

    # UC-8 Catalog Fulfillment is CHAT-ONLY (2026-06-09): the bespoke REST/
    # button routes (uc08_routes.py) + button frontend were removed. UC-8 is
    # reached via the conversational router (card-driven routing) and runs
    # through its 4 chat tools + the fulfilment engine; there is no button path.
    #
    # EXCEPTION — the APPROVE action is NON-chat by runbook design ("the IT team
    # handles it on the request"), so it is a small endpoint, NOT a chat tool and
    # NOT a catalog request route. Inert unless a request was parked for approval.
    from oneops.api.uc08_approval_routes import router as _uc08_appr_router
    app.include_router(_uc08_appr_router)

    # ── frontend ──────────────────────────────────────────────────────
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def _no_store_static(request: Request, call_next):
        """Force browsers to always re-fetch JS/CSS. Without this a tab can
        keep running a stale `app.js` after a frontend deploy (the cause of
        'I don't see the new UI' — the SPA never re-downloads its bundle).
        `no-store` makes every reload pull the current asset."""
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _index() -> HTMLResponse:
        """Serve index.html with cache-busted asset URLs.

        Production fix: browsers heuristically cache `.css`/`.js` for hours
        when only `Last-Modified` / `ETag` are sent. After a frontend code
        change (e.g. the 2026-05-30 UC-2 display_text render fix), the user
        kept seeing the OLD bundle even after Ctrl+Shift+R. We stamp each
        static URL with the file's mtime so any edit produces a different
        URL → browser bypasses cache automatically. No template engine,
        no service worker; a regex pass per page-load is cheap (~0.1 ms).

        Also send `Cache-Control: no-cache` on the index itself so the
        injected version stamps are picked up immediately.
        """
        import re as _re

        index_path = os.path.join(static_dir, "index.html")

        def _read_index() -> str:
            with open(index_path, encoding="utf-8") as f:
                return f.read()

        # Read off the event loop (sonar S7493 — no sync open() in async path).
        html = await asyncio.to_thread(_read_index)

        def _stamp(match: _re.Match[str]) -> str:
            attr, url = match.group(1), match.group(2)
            # Skip absolute URLs and already-stamped ones.
            if url.startswith(("http://", "https://", "//")) or "?" in url:
                return match.group(0)
            # /static/foo.js → static/foo.js → on-disk path.
            rel = url.lstrip("/").removeprefix("static/")
            path = os.path.join(static_dir, rel)
            try:
                mtime = int(os.path.getmtime(path))
            except OSError:
                return match.group(0)
            return f'{attr}="{url}?v={mtime}"'

        # Stamp <script src="/static/...">, <link href="/static/...">,
        # and <img src="/static/...">. Anything not under /static/ is left
        # alone (CDN URLs, data-URIs, etc.).
        html = _re.sub(
            r'(src|href)="(/static/[^"?]+)"',
            _stamp,
            html,
        )
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    # ── health ────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def _health() -> dict[str, Any]:
        return {
            "status": "ok",
            "active_agents": app.state.registry.active_agent_count(),
            "fast_path_eligible": [
                a.id for a in app.state.registry.agents.list_active()
                if a.fast_path is not None and a.fast_path.enabled
            ],
        }

    # ── subsystem config / status (frontend status strip) ─────────────
    @app.get("/api/config")
    async def _config() -> dict[str, Any]:
        from oneops.use_cases._shared.ticket_store import get_ticket_store
        from oneops.use_cases.uc01_summarization.cache import (
            get_summary_cache_store,
        )
        cache_backend = type(get_summary_cache_store()).__name__
        ticket_backend = type(get_ticket_store()).__name__
        otel_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        llm_gateway_url = os.getenv("LLM_GATEWAY_URL", "").strip()
        postgres_url = os.getenv("POSTGRES_URL", "").strip()
        nats_url = os.getenv("NATS_URL", "").strip()
        # `enabled` here means "the wire is configured" — not "the service
        # is alive". A live health probe is a separate, slower endpoint.
        return {
            "cache": {
                "enabled": True,
                "backend": cache_backend,
                "summarizer_wired":
                    _summarizer_is_wired(),
            },
            "otel": {
                "enabled": bool(otel_endpoint),
                "endpoint": otel_endpoint or None,
                "in_memory_spans": True,         # always — we open spans even
                                                # when no exporter is configured
            },
            "llm_gateway": {
                # The gateway HTTP URL is in env; whether `set_summarize_llm`
                # has been called is the more honest signal — it's the seam
                # the UC handler actually uses.
                "configured": bool(llm_gateway_url),
                "url": llm_gateway_url or None,
                "summarizer_wired": _summarizer_is_wired(),
            },
            "postgres": {
                "configured": bool(postgres_url),
                "backend_in_use": ticket_backend,
            },
            "nats": {
                "configured": bool(nats_url),
                "url": nats_url or None,
                # Production-grade truth: the chip is green when this
                # process holds a live NATS connection. Works for every
                # topology — single-process demo, split-roles, future
                # multi-replica — because the question being answered
                # is "is this ingress actually talking to NATS right
                # now?", which is a transport-state fact, not a
                # deployment-shape assumption.
                "wired_into_ingress": _nats_connection_is_live(),
            },
            "session": {
                "wired": getattr(app.state, "session_store", None) is not None,
                "backend": (type(getattr(app.state, "session_store", None)).__name__
                            if getattr(app.state, "session_store", None) else "none"),
                "durable_across_reload": True,   # localStorage on the frontend
            },
        }

    # ── identity option lists (sidebar dropdowns) ────────────────────
    @app.get("/api/identity-options")
    async def _identity_options() -> dict[str, Any]:
        # Tenants + users + roles are real values from the seeded DB and
        # the role registry — declared as data so they swap without code.
        # If POSTGRES_URL is set we *could* read distinct tenants live; for
        # the demo we hardcode the canonical set and let the user type a
        # custom value via the `custom` option.
        return {
            "tenants": ["T001", "T002", "T003"],
            "users": ["oneops", "u_demo", "u_admin", "u_viewer"],
            # Three canonical demo roles. Maps to UC-1's audience list
            # (`service_desk_agent` and `manager` are in-audience for
            # incident summarisation; `end_user` is OUT — exercises the
            # authz-recheck deny path cleanly).
            "roles": [
                "service_desk_agent",
                "manager",
                "end_user",
            ],
            "defaults": {
                "tenant": "T001",
                "user": "oneops",
                "role": "service_desk_agent",
            },
        }

    # ── session history (frontend rehydrates on reload) ───────────────
    @app.get("/api/session/{session_id}/history")
    async def _session_history(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: Request = None,             # type: ignore[assignment]
    ) -> dict[str, Any]:
        store = getattr(app.state, "session_store", None)
        if store is None:
            return {"events": []}
        tenant_id, _user_id, _role = _principal_from_headers(request)
        # Tenant binding is mandatory — the store keys events under
        # `(tenant_id, session_id)`; a mismatched tenant simply sees an
        # empty history, never another tenant's transcript.
        try:
            events = await store.recent(tenant_id, session_id)
        except Exception as exc:                  # noqa: BLE001 — boundary
            _log.warning("oneops.api.session_history_failed",
                         session_id=session_id, error=str(exc)[:200])
            return {"events": []}
        return {
            "session_id": session_id,
            "events": [
                {"role": e.turn_role, "content": e.content,
                 "turn_index": e.turn_index,
                 "occurred_at_unix_ms": e.occurred_at_unix_ms}
                for e in events
            ],
        }

    # ── fast-path spec discovery (frontend renders forms from this) ───
    @app.get("/api/fast/{uc_id}/spec", responses={404: {"description": "Not found"}})
    async def _fast_path_spec(uc_id: Annotated[str, Path(min_length=1)]) -> dict[str, Any]:
        spec = app.state.dispatcher.describe(uc_id)
        if spec is None:
            raise HTTPException(
                status_code=404,
                detail=f"use case {uc_id!r} does not expose a fast-path entry")
        return {
            "uc_id": uc_id,
            "display_name": _uc_display_name(uc_id, app.state.registry),
            "primary_tool_id": spec.primary_tool_id,
            "input_fields": [
                {"name": f.name, "type": f.type, "required": f.required,
                 "description": f.description,
                 "auto_derive_from": f.auto_derive_from}
                for f in spec.input_fields
            ],
        }

    # ── session lifecycle (server-owned create / list / delete) ───────
    # Server is the source of truth: every session_id is minted via
    # `POST /api/sessions`. Client localStorage is a cache of the
    # currently-selected id; it can be deleted without losing the
    # conversation (the server still has it until the idle TTL
    # expires) and a stale cached id is rejected on `GET`.
    @app.post("/api/sessions")
    async def _session_create(request: Request) -> dict[str, Any]:
        from oneops.session.lifecycle import get_lifecycle
        tenant_id, user_id, _role = _principal_from_headers(request)
        meta = await get_lifecycle().create(
            tenant_id=tenant_id, user_id=user_id)
        if meta is None:
            # Lifecycle store unreachable — mint a transient id so the
            # chat path still works. The session won't appear in the
            # sidebar list until Dragonfly recovers, but no functional
            # break for the user (graceful degradation).
            sid = _new_session_id()
            return {"session_id": sid, "transient": True}
        return meta.to_dict()

    @app.get("/api/sessions")
    async def _session_list(request: Request,
                            limit: int = 20) -> dict[str, Any]:
        from oneops.session.lifecycle import get_lifecycle
        tenant_id, user_id, _role = _principal_from_headers(request)
        rows = await get_lifecycle().list_for_user(
            tenant_id=tenant_id, user_id=user_id, limit=max(1, min(50, limit)))
        return {"sessions": [m.to_dict() for m in rows]}

    @app.get("/api/sessions/{session_id}", responses={404: {"description": "Not found"}})
    async def _session_get(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: Request = None,                    # type: ignore[assignment]
    ) -> dict[str, Any]:
        from oneops.session.lifecycle import get_lifecycle
        tenant_id, _user_id, _role = _principal_from_headers(request)
        meta = await get_lifecycle().get(
            tenant_id=tenant_id, session_id=session_id)
        if meta is None:
            raise HTTPException(status_code=404,
                                detail="session not found or expired")
        return meta.to_dict()

    @app.delete("/api/sessions/{session_id}")
    async def _session_delete(
        session_id: Annotated[str, Path(min_length=1, max_length=64)],
        request: Request = None,                    # type: ignore[assignment]
    ) -> dict[str, Any]:
        from oneops.session.lifecycle import get_lifecycle
        tenant_id, _user_id, _role = _principal_from_headers(request)
        ok = await get_lifecycle().delete(
            tenant_id=tenant_id, session_id=session_id)
        # Best effort: also remove the conversation log so a re-create
        # with the same id (theoretically impossible — uuid4) wouldn't
        # see ghost events. The hot window is implicitly purged when
        # there is no cold log to rebuild from.
        store = getattr(app.state, "session_store", None)
        if store is not None:
            try:
                # Append a terminal "closed" sentinel? — no, just leave the
                # cold log to TTL out naturally per retention policy. The
                # metadata removal above is what makes it invisible.
                pass
            except Exception:
                pass
        return {"deleted": ok, "session_id": session_id}

    # ── chat door (natural language) ─────────────────────────────────
    @app.post("/api/chat")
    async def _chat(req: ChatRequest, request: Request) -> TurnResponse:
        from oneops.api.chat_turn_cache import (
            cache_key as _chat_cache_key,
        )
        from oneops.api.chat_turn_cache import (
            should_cache as _chat_should_cache,
        )
        from oneops.session.lifecycle import get_lifecycle
        tenant_id, user_id, role = _principal_from_headers(request)
        request_id = _new_request_id()
        lifecycle = get_lifecycle()
        # Symmetric session-id ownership (production fix 2026-05-28):
        #   * Client sent a session_id → lookup. If found, use as-is.
        #     If unknown (first turn of a new session, or expired), ADOPT
        #     the client's id by passing it to lifecycle.create(). The
        #     lifecycle validates structural format and either adopts or
        #     falls back to a fresh-mint; the caller always learns the
        #     actual id via the response.
        #   * Client sent none → mint (auto-create new session).
        # The pre-fix behaviour (server-owned, replace-on-unknown) broke
        # multi-turn for any client that doesn't pre-create sessions via
        # POST /api/sessions — every turn became a fresh session, the
        # rewriter saw empty history, follow-ups misrouted to UC-3.
        # Adoption fixes this without breaking the frontend flow
        # (frontend still POSTs /api/sessions first; lifecycle.get finds
        # it on subsequent /api/chat calls).
        client_sid = (req.session_id or "").strip()
        if client_sid:
            existing = await lifecycle.get(
                tenant_id=tenant_id, session_id=client_sid)
            if existing is None:
                fresh = await lifecycle.create(
                    tenant_id=tenant_id, user_id=user_id,
                    title=(req.message or "")[:120],
                    session_id=client_sid,                  # adopt if safe
                )
                session_id = (fresh.session_id if fresh is not None
                              else client_sid)
            else:
                session_id = client_sid
        else:
            fresh = await lifecycle.create(
                tenant_id=tenant_id, user_id=user_id,
                title=(req.message or "")[:120])
            session_id = (fresh.session_id if fresh is not None
                          else _new_session_id())
        # ── Turn-level cache check (semantic, no keywords) ───────────────
        # Key hashes (tenant + user + role + session + normalized message).
        # Different session → different key → no focus leak between users.
        # On hit: short-circuit the full pipeline (decomposer + rewriter +
        # focus classifier + disambiguator + UC handler) and return the
        # prior turn's response. TTL bounds staleness against ticket edits.
        cache = getattr(app.state, "chat_turn_cache", None)
        ckey = _chat_cache_key(
            tenant_id=tenant_id, user_id=user_id, role=role,
            session_id=session_id, message=req.message or "",
        )
        if cache is not None:
            try:
                cached = await cache.get(tenant_id=tenant_id, key=ckey)
            except Exception:                                     # noqa: BLE001
                cached = None
            if cached is not None:
                # Bump request_id so OTel still distinguishes this turn
                # from the original; the body is the cached one. Strict
                # Pydantic TurnResponse rejects extras, so we only set
                # fields in the schema.
                cached["request_id"] = request_id
                _metric_inc("ai.chat_turn_cache.hits.total", 1,
                            tenant_id=tenant_id)
                _log.info("oneops.api.chat_turn_cache_hit",
                          tenant_id=tenant_id, session_id=session_id,
                          request_id=request_id)
                with suppress(Exception):
                    await lifecycle.touch(
                        tenant_id=tenant_id, session_id=session_id,
                        user_id=user_id,
                        title=(req.message or "")[:120], bump_turn_count=True)
                # Observability: a cached chat turn is still one trace.
                _emit_cache_hit_span(
                    door="chat", tenant_id=tenant_id, user_id=user_id,
                    session_id=session_id, request_id=request_id,
                    message=req.message or "", cached=cached)
                return TurnResponse(**cached)

        # ── Conversational Interrupt Protocol — resume path ───────────────
        # When the frontend sends interrupt_resume=True, the user is answering
        # a paused interrupt. Resume the checkpointed LangGraph state instead
        # of starting a fresh turn. The pending interrupt record stored in the
        # turn cache (key __interrupt__:{session_id}) is cleared on resume so
        # subsequent turns start fresh.
        if req.interrupt_resume and req.interrupt_answer is not None:
            _ikey = f"__interrupt__{session_id}"
            _pending: dict[str, Any] | None = None
            if cache is not None:
                with suppress(Exception):
                    _pending = await cache.get(
                        tenant_id=tenant_id, key=_ikey)
            if _pending is not None:
                # Resume on the EXACT thread the paused turn ran on (stored when
                # it interrupted) — not session_id, so independent turns keep
                # their own per-request threads and don't cross-contaminate.
                _paused_thread = (str(_pending.get("thread"))
                                  if isinstance(_pending, dict)
                                  and _pending.get("thread") else session_id)
                # Clear the pending-interrupt marker before resuming — a resume
                # that fails mid-flight must not leave a stale marker that
                # causes the next turn to also be treated as a resume.
                with suppress(Exception):
                    if hasattr(cache, "delete"):
                        await cache.delete(tenant_id=tenant_id, key=_ikey)
                    else:
                        # Overwrite with empty sentinel (TTL=1s); works on any
                        # ChatTurnCache impl including InMemory.
                        await cache.put(
                            tenant_id=tenant_id, key=_ikey, value={})
                graph = request.app.state.graph
                import asyncio as _aio
                _s = get_settings()
                try:
                    out = await _aio.wait_for(
                        resume_turn(
                            graph, req.interrupt_answer,
                            config={"configurable": {"thread_id": _paused_thread}}),
                        timeout=_s.turn_timeout_seconds)
                except Exception:                             # noqa: BLE001
                    out = {}
                # The resumed turn may pause AGAIN (a multi-step flow: pick →
                # fields → confirm). Detect the next interrupt from the returned
                # state and re-arm the marker so the following turn resumes too.
                _intr2 = _extract_interrupt_payload(out)
                if _intr2 is not None and cache is not None:
                    with suppress(Exception):
                        await cache.put(
                            tenant_id=tenant_id, key=_ikey,
                            value={"interrupt": _intr2,
                                   "thread": _paused_thread})
                with suppress(Exception):
                    await lifecycle.touch(
                        tenant_id=tenant_id, session_id=session_id,
                        user_id=user_id,
                        title=(req.message or "")[:120], bump_turn_count=True)
                _resp = (_intr2.get("prompt", _intr2.get("question", ""))
                         if _intr2 else str(out.get("final_response") or ""))
                return TurnResponse(
                    door="chat",
                    final_status=("interrupted" if _intr2
                                  else str(out.get("final_status") or "executed")),
                    final_response=_resp,
                    step_results=list(out.get("step_results") or []),
                    session_id=session_id,
                    request_id=request_id,
                    trace_id=None,
                    latency_ms=0,
                    interrupt=_intr2,
                )

        envelope: dict[str, Any] = {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "message": req.message,
        }
        if req.forced_agent_ids:
            envelope["forced_agent_ids"] = list(req.forced_agent_ids)
        response = await _run(request, envelope, door="chat",
                              thread_id=session_id, session_id=session_id)
        # Slide the idle TTL and bump turn_count + title on every
        # successful turn. Failures are non-fatal — the chat reply is
        # already produced.
        with suppress(Exception):
            await lifecycle.touch(
                tenant_id=tenant_id, session_id=session_id, user_id=user_id,
                title=(req.message or "")[:120], bump_turn_count=True)

        # ── Write to turn cache on a successful, useful response ──────────
        # Refusals, clarifications, and tiny replies are deliberately not
        # cached so the next turn re-runs the pipeline.
        if cache is not None:
            _metric_inc("ai.chat_turn_cache.misses.total", 1,
                        tenant_id=tenant_id)
            try:
                payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
                if _chat_should_cache(payload):
                    await cache.put(tenant_id=tenant_id, key=ckey, value=payload)
                    _metric_inc("ai.chat_turn_cache.writes.total", 1,
                                tenant_id=tenant_id)
            except Exception:                                     # noqa: BLE001
                pass

        return response

    async def _stream_turn(request: Request, envelope: dict[str, Any], *,
                           door: str):
        """Run one turn and yield NDJSON live events, then a final payload.

        Shared by the chat and fast-path (button) streaming doors so both
        animate identically. Emits:
          {"type":"turn_start"} → {"type":"tool_start"|"tool_done"}* →
          {"type":"final","payload": <TurnResponse>}
        Best-effort event sink keyed by request_id; the executor path is
        unchanged whether or not anyone is streaming.
        """
        import asyncio as _asyncio
        import json as _json

        from oneops.observability.event_sink import close_sink, open_sink
        from oneops.session.lifecycle import get_lifecycle

        rid = str(envelope.get("request_id") or "")
        sid = str(envelope.get("session_id") or "")
        tenant_id = str(envelope.get("tenant_id") or "")
        role = str(envelope.get("role") or "")
        message = str(envelope.get("message") or "")

        def _line(obj: dict[str, Any]) -> str:
            return _json.dumps(obj, default=str) + "\n"

        # ── Semantic turn cache — cross-session consistency for standalone
        # chat queries. A hit returns the IDENTICAL prior answer with no
        # pipeline run, so LLM non-determinism cannot flip the result.
        # Scoped to the chat door + standalone queries (no pronoun/record id)
        # so focus context never leaks. Best-effort.
        from oneops.api.chat_turn_cache import should_cache as _should_cache
        from oneops.api.semantic_turn_cache import is_standalone as _is_standalone
        sem = getattr(request.app.state, "semantic_cache", None)
        # A resume turn (mid-interrupt-flow) must NEVER hit the semantic cache —
        # it has to run resume_turn against the live checkpoint, not return a
        # cached standalone answer. Excluding it keeps the UC-8 flow on the
        # resume path instead of falling back through the control gate.
        sem_eligible = (door == "chat" and sem is not None
                        and not envelope.get("interrupt_resume")
                        and _is_standalone(message))
        if sem_eligible:
            cached = await sem.get(tenant_id=tenant_id, role=role, query=message)
            if cached is not None:
                cached = dict(cached)
                cached["request_id"] = rid
                cached["session_id"] = sid
                yield _line({"type": "turn_start", "request_id": rid,
                             "session_id": sid})
                yield _line({"type": "final", "payload": cached})
                return

        q = open_sink(rid)
        task = _asyncio.ensure_future(
            _run(request, envelope, door=door, thread_id=sid, session_id=sid))
        try:
            yield _line({"type": "turn_start", "request_id": rid,
                         "session_id": sid})
            while True:
                getter = _asyncio.ensure_future(q.get())
                done, _pending = await _asyncio.wait(
                    {getter, task}, return_when=_asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield _line(getter.result())
                    continue
                getter.cancel()
                while not q.empty():                       # flush buffered
                    yield _line(q.get_nowait())
                try:
                    resp = task.result()
                    payload = (resp.model_dump()
                               if hasattr(resp, "model_dump") else dict(resp))
                except Exception as exc:                   # noqa: BLE001
                    # Log the real cause internally; the client gets an opaque
                    # message + request_id (no internal exception text leaks on
                    # the streaming path either — P0-3 / Batch C-3).
                    _log.warning("oneops.api.stream_turn_failed",
                                 door=door, request_id=rid,
                                 error=str(exc)[:200])
                    payload = {
                        "door": door, "final_status": "failed",
                        "final_response": (
                            "The assistant ran into an error. Please try "
                            f"again. (request_id={rid})"),
                        "step_results": [], "session_id": sid,
                        "request_id": rid, "trace_id": None, "latency_ms": 0}
                with suppress(Exception):
                    await get_lifecycle().touch(
                        tenant_id=str(envelope.get("tenant_id") or ""),
                        session_id=sid,
                        user_id=str(envelope.get("user_id") or ""),
                        title=str(envelope.get("message") or "")[:120],
                        bump_turn_count=True)
                # Store the answer for future semantically-equivalent
                # standalone queries (consistency). Only successful, useful
                # responses are cached (same predicate as the turn cache).
                if sem_eligible and _should_cache(payload):
                    with suppress(Exception):
                        await sem.put(tenant_id=tenant_id, role=role,
                                      query=message, response=payload)
                yield _line({"type": "final", "payload": payload})
                break
        finally:
            close_sink(rid)
            if not task.done():
                task.cancel()

    @app.post("/api/chat/stream")
    async def _chat_stream(req: ChatRequest, request: Request):
        """Live streaming variant of /api/chat (NDJSON).

        Emits one JSON object per line as the turn executes:
          {"type":"turn_start", ...}
          {"type":"tool_start", agent_id, tool_id, action}   ← tool now running
          {"type":"tool_done",  agent_id, tool_id, status, latency_ms}
          {"type":"final", "payload": <full TurnResponse>}
        Turn-cache is intentionally bypassed so the user always sees the
        agents + tools do the work. Same handler/pipeline as /api/chat, so
        the final payload is identical — only the transport differs.
        """
        from fastapi.responses import StreamingResponse

        from oneops.session.lifecycle import get_lifecycle

        tenant_id, user_id, role = _principal_from_headers(request)
        request_id = _new_request_id()
        lifecycle = get_lifecycle()
        client_sid = (req.session_id or "").strip()
        if client_sid:
            existing = await lifecycle.get(
                tenant_id=tenant_id, session_id=client_sid)
            if existing is None:
                fresh = await lifecycle.create(
                    tenant_id=tenant_id, user_id=user_id,
                    title=(req.message or "")[:120], session_id=client_sid)
                session_id = (fresh.session_id if fresh is not None
                              else client_sid)
            else:
                session_id = client_sid
        else:
            fresh = await lifecycle.create(
                tenant_id=tenant_id, user_id=user_id,
                title=(req.message or "")[:120])
            session_id = (fresh.session_id if fresh is not None
                          else _new_session_id())

        envelope: dict[str, Any] = {
            "request_id": request_id, "tenant_id": tenant_id,
            "session_id": session_id, "user_id": user_id, "role": role,
            "message": req.message,
        }
        # Pre-routed dispatch: a caller (e.g. the "Raise a service request" offer
        # button) already chose the agent(s). Forward so `route` SKIPS the LLM
        # router and runs them directly — parity with the non-stream /api/chat.
        # Without this the stream door silently re-routed (KB → offer → loop).
        if req.forced_agent_ids:
            envelope["forced_agent_ids"] = list(req.forced_agent_ids)
        # Forward an interrupt reply so the browser's widget answer RESUMES the
        # paused flow (pick → fields → confirm → create) instead of starting a
        # new turn. `_run` routes these to resume_turn.
        if req.interrupt_resume and req.interrupt_answer is not None:
            envelope["interrupt_resume"] = True
            envelope["interrupt_answer"] = req.interrupt_answer
        return StreamingResponse(
            _stream_turn(request, envelope, door="chat"),
            media_type=_APPLICATION_X_NDJSON)

    # ── chat door (WebSocket) ─────────────────────────────────────────
    # Production topology (per architecture plan):
    #   Browser ── WebSocket ──> AWS API Gateway (WebSocket API) ──>
    #   Ingress (this) ── NATS ──> graph_worker ── NATS ──>
    #   agent_worker ── NATS ──> ingress ── WS frame ──> Browser
    #
    # Production-grade details:
    #   * Identity bound at HANDSHAKE only (headers from API Gateway).
    #     Subsequent frames cannot change tenant / user / role — the
    #     server keeps the principal in the connection scope. Defence
    #     against the "envelope spoofing" class.
    #   * Per-frame request_id; the user can pipeline several queries
    #     on one socket and get matching replies (correlation id in the
    #     reply payload). NATS request/reply already round-trips this.
    #   * Heartbeat: rely on the WebSocket protocol's PING/PONG, kept
    #     alive by uvicorn's default 20s timeout. Closed sockets are
    #     dropped server-side; the browser opens a new one on next
    #     turn (frontend reconnect logic).
    #   * Graceful degradation: NATSUnavailableError / TimeoutError on
    #     the per-frame round-trip surface as a typed `service_degraded`
    #     response frame — never closes the socket on a transient
    #     failure, just sends the message back to the browser.
    #   * Backpressure: `await ws.send_json(...)` is awaited per frame;
    #     a slow browser blocks the per-connection loop, not the whole
    #     ingress (one task per socket).
    @app.websocket("/ws/chat")
    async def _ws_chat(ws: WebSocket) -> None:
        # Bind identity at connect — never per-frame. Same fallback rules
        # as the HTTP path so the demo works without auth headers.
        h = ws.headers
        tenant_id = (h.get("x-tenant-id") or DEFAULT_TENANT_FALLBACK).strip()
        user_id = (h.get("x-user-id") or DEFAULT_USER_FALLBACK).strip()
        role = (h.get("x-role") or DEFAULT_ROLE_FALLBACK).strip()
        await ws.accept()
        _log.info("oneops.ws.connected",
                  tenant_id=tenant_id, user_id=user_id, role=role)
        try:
            while True:
                # One frame = one chat turn. Browser sends:
                #   {"message": "...", "session_id": "..."}
                # Server replies with the same TurnResponse shape the HTTP
                # path returns — frontend renders identically.
                frame = await ws.receive_json()
                if not isinstance(frame, dict):
                    await ws.send_json({
                        "door": "chat",
                        "final_status": "invalid_request",
                        "final_response": "Each frame must be a JSON object.",
                        "step_results": [], "session_id": "",
                        "request_id": "", "trace_id": None, "latency_ms": 0,
                    })
                    continue

                message = str(frame.get("message") or "").strip()
                if not message:
                    await ws.send_json({
                        "door": "chat",
                        "final_status": "invalid_request",
                        "final_response": "`message` is required.",
                        "step_results": [], "session_id": "",
                        "request_id": "", "trace_id": None, "latency_ms": 0,
                    })
                    continue

                request_id = _new_request_id()
                session_id = (str(frame.get("session_id") or "").strip()
                              or _new_session_id())
                envelope: dict[str, Any] = {
                    "request_id": request_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "user_id": user_id,
                    "role": role,
                    "message": message,
                }
                try:
                    reply = await _run(
                        # `_run` uses the request only for `app.state` and
                        # OTel context; the WS connection's scope provides
                        # the same. We pass a small shim that exposes
                        # `app.state` so the helper stays unchanged.
                        # _RequestShim duck-types the bits _run reads (.app);
                        # cast keeps the type-checker honest (sonar S5655).
                        cast(Request, _RequestShim(ws.app)), envelope, door="chat",
                        thread_id=session_id, session_id=session_id)
                except Exception as exc:                  # noqa: BLE001
                    _log.warning("oneops.ws.turn_failed", error=str(exc)[:200])
                    reply = TurnResponse(
                        door="chat",
                        final_status="service_degraded",
                        final_response=("That request couldn't complete. "
                                         "Please try again."),
                        step_results=[], session_id=session_id,
                        request_id=request_id, trace_id=None, latency_ms=0,
                    )

                # Pydantic v2: .model_dump() — single source of truth so
                # WS frames are byte-identical to HTTP responses.
                await ws.send_json(reply.model_dump())
        except WebSocketDisconnect:
            _log.info("oneops.ws.disconnected",
                      tenant_id=tenant_id, user_id=user_id)
        except Exception as exc:                          # noqa: BLE001
            _log.warning("oneops.ws.error", error=str(exc)[:200])
            try:
                await ws.close(code=1011)
            except Exception:                             # noqa: BLE001
                pass

    # ── fast-path door (button-shaped, UC-declared) ──────────────────
    @app.post("/api/fast/{uc_id}")
    async def _fast_path(
        uc_id: Annotated[str, Path(min_length=1)],
        req: FastPathPostRequest | None = None,
        request: Request = None,                # type: ignore[assignment]
    ) -> TurnResponse:
        tenant_id, user_id, role = _principal_from_headers(request)
        request_id = _new_request_id()
        session_id = ((req and req.session_id) or _new_session_id()).strip()
        inputs = (req.inputs if req else {}) or {}

        # ── API-edge cache (shared Dragonfly via chat_turn_cache) ──────────
        # Matches /api/chat's pattern — return the cached response from the
        # last identical call, short-circuiting the dispatcher + executor +
        # handler entirely. Brings fast-path warm-hit latency from ~500ms
        # down into the ~10ms band, matching UC-2 button and chat.
        import hashlib
        import json as _json
        edge_cache = getattr(app.state, "chat_turn_cache", None)
        ekey = None
        if edge_cache is not None:
            from oneops.api.cache_version import PIPELINE_CACHE_VERSION as _PV
            _inputs_canon = _json.dumps(inputs, sort_keys=True, default=str)
            _raw = (
                f"fp:{uc_id}\x1f{tenant_id}\x1f{user_id}\x1f{role}\x1f"
                f"{_inputs_canon}\x1fv={_PV}"
            )
            ekey = "fastpath:" + hashlib.sha256(_raw.encode()).hexdigest()[:32]
            try:
                cached = await edge_cache.get(tenant_id=tenant_id, key=ekey)
            except Exception:                                       # noqa: BLE001
                cached = None
            if cached is not None:
                cached["request_id"] = request_id
                cached["cache_hit"] = True
                _metric_inc("ai.fast_path.edge_cache.hits.total", 1,
                            tenant_id=tenant_id, uc_id=uc_id)
                _log.info("oneops.api.fast_path.edge_cache_hit",
                          uc_id=uc_id, tenant_id=tenant_id,
                          request_id=request_id)
                # Observability: a cached button press is still one trace.
                _emit_cache_hit_span(
                    door="fast_path", tenant_id=tenant_id, user_id=user_id,
                    session_id=session_id, request_id=request_id,
                    message=_humanise_fast_path_request(
                        uc_id, inputs, app.state.registry),
                    cached=cached)
                return TurnResponse(**cached)

        try:
            dispatch = app.state.dispatcher.dispatch(FastPathRequest(
                uc_id=uc_id, inputs=inputs))
        except FastPathError as exc:
            # Surface dispatcher rejections as a normal clarification turn.
            # Raw HTTP 400 with internal text ("fast-path requires fields:
            # ['service_id']") leaks implementation detail to the user and
            # never matches the "system should validate and respond per
            # contract" rule. A bare-digit / missing-prefix id from the
            # button lands here.
            _log.info("fast_path.input_clarification",
                      uc_id=uc_id, error=str(exc)[:200])
            primary = next(iter(inputs.values()), "") if inputs else ""
            primary_text = f" '{primary}'" if primary else ""
            return TurnResponse(
                door="fast_path",
                request_id=request_id,
                trace_id=None,
                session_id=session_id,
                final_status="clarification",
                final_response=(
                    "I couldn't act on that input"
                    + primary_text
                    + ". Please provide a complete record id "
                    "(for example INC0001001, REQ0002001, PBM0003001, "
                    "CHG0004001) and try again."),
                step_results=[],
                router_diagnostics=[],
                latency_ms=0,
                summarizer_wired=_summarizer_is_wired(),
                cache_hit=None,
                cache_age_s=None,
            )
        # Serialise the plan into the shape the executor's state holds.
        plan_state = [
            {"step_id": s.step_id, "agent_id": s.agent_id,
             "parameters": dict(s.parameters), "depends_on": list(s.depends_on)}
            for s in dispatch.plan.steps
        ]
        envelope: dict[str, Any] = {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            # A human-readable synthetic message — what the user effectively
            # asked when they pressed the button. This is what appears in
            # the conversation log and on reload, so it must read naturally
            # ("Summarize INC0001001"), never as debug junk.
            "message": _humanise_fast_path_request(
                uc_id, inputs, app.state.registry),
            # Pre-built plan + explicit fast-path stamp — see executor.graph
            # entry-branch contract.
            "plan": plan_state,
            "route_outcome": "routed",
            "entry_mode": "fast_path",
        }
        response = await _run(request, envelope, door="fast_path",
                              thread_id=request_id, session_id=session_id)

        # ── Edge-cache write (only on successful, useful responses) ────────
        if edge_cache is not None and ekey is not None:
            _metric_inc("ai.fast_path.edge_cache.misses.total", 1,
                        tenant_id=tenant_id, uc_id=uc_id)
            try:
                payload = response.model_dump(mode="json")
                # Reuse the chat-turn `should_cache` predicate — same rules
                # (skip refusals/clarifications/empty).
                from oneops.api.chat_turn_cache import (
                    should_cache as _should_cache,
                )
                if _should_cache(payload):
                    await edge_cache.put(
                        tenant_id=tenant_id, key=ekey, value=payload)
                    _metric_inc("ai.fast_path.edge_cache.writes.total", 1,
                                tenant_id=tenant_id, uc_id=uc_id)
            except Exception:                                       # noqa: BLE001
                pass
        return response

    @app.post("/api/fast/{uc_id}/stream")
    async def _fast_path_stream(
        uc_id: Annotated[str, Path(min_length=1)],
        req: FastPathPostRequest | None = None,
        request: Request = None,                # type: ignore[assignment]
    ):
        """Live streaming variant of the fast-path button (NDJSON).

        Same event shape as /api/chat/stream, so the button animates its
        agents + tools identically. Edge-cache is bypassed so the work is
        always visibly executed.
        """
        import json as _json

        from fastapi.responses import StreamingResponse

        tenant_id, user_id, role = _principal_from_headers(request)
        request_id = _new_request_id()
        session_id = ((req and req.session_id) or _new_session_id()).strip()
        inputs = (req.inputs if req else {}) or {}

        try:
            dispatch = app.state.dispatcher.dispatch(
                FastPathRequest(uc_id=uc_id, inputs=inputs))
        except FastPathError as exc:
            _log.info("fast_path.stream.input_clarification",
                      uc_id=uc_id, error=str(exc)[:200])
            primary = next(iter(inputs.values()), "") if inputs else ""
            primary_text = f" '{primary}'" if primary else ""
            payload = TurnResponse(
                door="fast_path", request_id=request_id, trace_id=None,
                session_id=session_id, final_status="clarification",
                final_response=(
                    "I couldn't act on that input" + primary_text +
                    ". Please provide a complete record id (for example "
                    "INC0001001, REQ0002001, PBM0003001, CHG0004001) and "
                    "try again."),
                step_results=[], router_diagnostics=[], latency_ms=0,
                summarizer_wired=_summarizer_is_wired(),
                cache_hit=None, cache_age_s=None,
            ).model_dump()

            async def _clarify():
                yield _json.dumps({"type": "turn_start",
                                   "request_id": request_id,
                                   "session_id": session_id}) + "\n"
                yield _json.dumps({"type": "final", "payload": payload},
                                  default=str) + "\n"

            return StreamingResponse(_clarify(),
                                     media_type=_APPLICATION_X_NDJSON)

        plan_state = [
            {"step_id": s.step_id, "agent_id": s.agent_id,
             "parameters": dict(s.parameters), "depends_on": list(s.depends_on)}
            for s in dispatch.plan.steps
        ]
        envelope: dict[str, Any] = {
            "request_id": request_id, "tenant_id": tenant_id,
            "session_id": session_id, "user_id": user_id, "role": role,
            "message": _humanise_fast_path_request(
                uc_id, inputs, app.state.registry),
            "plan": plan_state, "route_outcome": "routed",
            "entry_mode": "fast_path",
        }
        return StreamingResponse(
            _stream_turn(request, envelope, door="fast_path"),
            media_type=_APPLICATION_X_NDJSON)

    return app


# ── shared turn driver ──────────────────────────────────────────────────


def _emit_cache_hit_span(
    *, door: str, tenant_id: str, user_id: str, session_id: str,
    request_id: str, message: str, cached: dict[str, Any],
) -> None:
    """Emit a one-node Langfuse trace for an edge-cache HIT so a cached turn
    (chat OR button) is still visible in the Agent Graph — otherwise repeat
    requests vanish from observability. Best-effort; never raises; the content
    is redacted + content-gated exactly like the live path."""
    try:
        with _tracer.start_as_current_span(
            "oneops.api.turn",
            attributes={"oneops.door": door, "oneops.tenant_id": tenant_id,
                        "oneops.user_id": user_id, "oneops.cache_hit": True},
        ) as span:
            set_langfuse_trace(
                span, tenant_id=tenant_id, user_id=user_id,
                session_id=session_id, request_id=request_id,
                name="oneops.query", input=message)
            span.set_attribute(
                "oneops.final_status",
                str(cached.get("final_status") or "cached"))
            if langfuse_capture_content_enabled():
                span.set_attribute(
                    "langfuse.trace.output",
                    redact_for_span(cached.get("final_response")))
    except Exception:                                       # noqa: BLE001
        pass


async def _run(
    request: Request, envelope: dict[str, Any], *,
    door: str, thread_id: str, session_id: str,
) -> TurnResponse:
    """Drive one turn — either in-process (default) or over NATS
    (when UC_INVOKER_MODE=nats). Same envelope, same TurnResponse shape;
    only the transport differs."""
    t0 = time.monotonic()
    mode = getattr(request.app.state, "invoker_mode", "local")
    # The checkpoint thread this turn runs on (request-scoped by default; a
    # resume reuses the paused turn's thread — set in the in-process path).
    graph_thread = str(envelope.get("request_id") or "")
    try:
        with _tracer.start_as_current_span(
            "oneops.api.turn",
            attributes={
                "oneops.door": door,
                "oneops.tenant_id": envelope["tenant_id"],
                "oneops.user_id": envelope.get("user_id", ""),
                "oneops.invoker_mode": mode,
            },
        ) as span:
            set_langfuse_io(span, input=envelope.get("message", ""))
            _s = get_settings()
            if mode == "nats":
                from oneops.api.nats_invoker import nats_invoke
                # The worker computes latency_ms / trace_id of its own
                # leg; we still wrap the whole HTTP turn under
                # `oneops.api.turn` so the end-to-end span captures the
                # NATS round-trip too.
                reply = await asyncio.wait_for(
                    nats_invoke(envelope, timeout_s=_s.turn_timeout_seconds),
                    timeout=_s.turn_nats_outer_timeout_seconds)
                trace_id = (reply.get("trace_id")
                            or (format(span.get_span_context().trace_id, "032x")
                                if span.get_span_context().trace_id else None))
                set_langfuse_io(span, output=reply.get("final_response"))
                return TurnResponse(
                    door=door,
                    final_status=str(reply.get("final_status") or ""),
                    final_response=str(reply.get("final_response") or ""),
                    step_results=list(reply.get("step_results") or []),
                    session_id=session_id,
                    request_id=envelope["request_id"],
                    trace_id=trace_id,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                )

            # in-process path (default)
            graph = request.app.state.graph
            # Each INDEPENDENT turn runs on its OWN checkpoint thread (the
            # request id) so it never resumes the previous turn's plan/results
            # — cross-turn memory comes from the session store, not the
            # checkpointer (ADR-0004). Using session_id as the thread made every
            # turn-2+ continue turn-1's completed graph state, so the new
            # message inherited the prior turn's agent. A turn that PAUSED
            # stored its thread under the session; a resume reuses EXACTLY that
            # thread to continue the same paused flow.
            _is_resume = bool(envelope.get("interrupt_resume")
                              and envelope.get("interrupt_answer") is not None)
            graph_thread = str(envelope["request_id"])
            _cache0 = getattr(request.app.state, "chat_turn_cache", None)
            if _is_resume and _cache0 is not None:
                with suppress(Exception):
                    _pend = await _cache0.get(
                        tenant_id=envelope.get("tenant_id", ""),
                        key=f"__interrupt__{session_id}")
                    if isinstance(_pend, dict) and _pend.get("thread"):
                        graph_thread = str(_pend["thread"])
            _cfg = {"configurable": {"thread_id": graph_thread}}
            if _is_resume:
                out = await asyncio.wait_for(
                    resume_turn(graph, envelope["interrupt_answer"],
                                config=_cfg),
                    timeout=_s.turn_timeout_seconds,
                )
            else:
                out = await asyncio.wait_for(
                    run_turn(graph, envelope, config=_cfg),
                    timeout=_s.turn_timeout_seconds,
                )
            set_langfuse_io(span, output=out.get("final_response"))
            trace_id = format(span.get_span_context().trace_id, "032x") \
                if span.get_span_context().trace_id else None
    except TimeoutError:
        # Soft degradation — a typed turn response, not HTTP 504.
        # Production should not surface raw transport timeouts as 5xx;
        # the user sees a clean retryable message and the trace carries
        # the timeout for ops.
        _log.warning("oneops.api.turn_timeout", door=door)
        return TurnResponse(
            door=door,
            final_status="service_degraded",
            final_response=(
                "The assistant is taking longer than expected. "
                "Please try that again in a moment."),
            step_results=[],
            session_id=session_id,
            request_id=envelope["request_id"],
            trace_id=None,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    except NATSUnavailableError as exc:
        # Transport degradation surfaces as a typed turn response too —
        # retries already exhausted by `resilient_call`, or the circuit
        # breaker is OPEN. The user sees a clean retryable message; the
        # logged warning carries the breaker / exhaustion reason for ops.
        _log.warning("oneops.api.turn_degraded",
                     door=door, reason=str(exc)[:200])
        return TurnResponse(
            door=door,
            final_status="service_degraded",
            final_response=(
                "The assistant is temporarily unavailable. "
                "Please try again shortly."),
            step_results=[],
            session_id=session_id,
            request_id=envelope["request_id"],
            trace_id=None,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
    except OneOpsError as exc:
        # Log the real error internally; return an opaque message + request id so
        # the client/support can correlate without leaking internals (P0-3).
        req_id = envelope.get("request_id", "")
        _log.warning("oneops.api.turn_failed", door=door,
                     request_id=req_id, error_code=getattr(exc, "code", ""),
                     error=str(exc)[:200])
        raise HTTPException(
            status_code=500,
            detail=f"engine failure (request_id={req_id})") from exc
    except Exception as exc:                  # noqa: BLE001 — boundary
        # ── Conversational Interrupt Protocol — interrupt capture ─────────
        # LangGraph raises GraphInterrupt (subclass of Exception) when a
        # node calls interrupt(). Intercept here, persist in the turn
        # cache keyed by session_id, and return a typed TurnResponse with
        # final_status="interrupted" so the frontend can render the
        # appropriate widget. All other exceptions fall through to the
        # HTTP 500 path below.
        from langgraph.errors import GraphInterrupt as _GraphInterrupt
        if isinstance(exc, _GraphInterrupt):
            _interrupts = exc.args[0] if exc.args else ()
            _payload: dict[str, Any] = {}
            if _interrupts:
                _val = _interrupts[0]
                _payload = (_val.value
                            if hasattr(_val, "value") else dict(_val)
                            if isinstance(_val, dict) else {"value": _val})
            _log.info(
                "oneops.api.interrupt_captured",
                door=door, session_id=session_id,
                kind=_payload.get("kind", "unknown"))
            _cache = getattr(request.app.state, "chat_turn_cache", None)
            if _cache is not None:
                with suppress(Exception):
                    await _cache.put(
                        tenant_id=envelope.get("tenant_id", ""),
                        key=f"__interrupt__{session_id}",
                        value={"interrupt": _payload, "thread": graph_thread})
            _clarification = _payload.get(
                "prompt", _payload.get("question", "Input required."))
            # Persist the paused turn (user message + clarification) so the
            # conversation history is complete even when a turn ASKS instead of
            # completing — the in-graph `persist` node never runs on interrupt.
            # Dedup in the helper keeps resume from double-writing the message.
            _store = getattr(request.app.state, "session_store", None)
            if _store is not None:
                with suppress(Exception):
                    from oneops.executor.nodes import append_turn_events
                    await append_turn_events(
                        _store, envelope.get("tenant_id", ""), session_id,
                        user_message=envelope.get("message", "") or "",
                        assistant_message=_clarification)
            return TurnResponse(
                door=door,
                final_status="interrupted",
                final_response=_clarification,
                step_results=[],
                session_id=session_id,
                request_id=envelope["request_id"],
                trace_id=None,
                latency_ms=int((time.monotonic() - t0) * 1000),
                interrupt=_payload,
            )
        req_id = envelope.get("request_id", "")
        _log.warning("oneops.api.turn_failed",
                     door=door, request_id=req_id, error=str(exc)[:200])
        raise HTTPException(
            status_code=500,
            detail=f"engine failure (request_id={req_id})") from exc
    # Interrupt protocol (checkpointer path): ainvoke RETURNED a paused state
    # rather than raising — surface the interrupt the same way as the raised
    # path above so the frontend renders the widget and can resume.
    _intr = _extract_interrupt_payload(out)
    if _intr is not None:
        _log.info("oneops.api.interrupt_captured", door=door,
                  session_id=session_id, kind=_intr.get("kind", "unknown"))
        _cache = getattr(request.app.state, "chat_turn_cache", None)
        if _cache is not None:
            with suppress(Exception):
                await _cache.put(
                    tenant_id=envelope.get("tenant_id", ""),
                    key=f"__interrupt__{session_id}",
                    value={"interrupt": _intr, "thread": graph_thread})
        _clar = _intr.get("prompt", _intr.get("question", "Input required."))
        # Persist the paused turn (user message + clarification) so the
        # conversation history is complete even when the turn ASKS instead of
        # completing — same as the raised-interrupt path above. Resume-safe via
        # the helper's user-dedup.
        _store = getattr(request.app.state, "session_store", None)
        if _store is not None and not _is_resume:
            with suppress(Exception):
                from oneops.executor.nodes import append_turn_events
                await append_turn_events(
                    _store, envelope.get("tenant_id", ""), session_id,
                    user_message=envelope.get("message", "") or "",
                    assistant_message=_clar)
        return TurnResponse(
            door=door,
            final_status="interrupted",
            final_response=_clar,
            step_results=[],
            session_id=session_id,
            request_id=envelope["request_id"],
            trace_id=trace_id,
            latency_ms=int((time.monotonic() - t0) * 1000),
            interrupt=_intr,
        )
    # Self-service-first: a sole-KB answer carries a "raise a service request?"
    # offer (rendered as buttons; choosing it re-dispatches to fulfilment). Best
    # effort — a failure here must never break the answer.
    _sr_offer = None
    with suppress(Exception):
        _sr_offer = _build_service_request_offer(
            out, getattr(request.app.state, "registry", None),
            envelope.get("message", "") or "")
    return TurnResponse(
        door=door,
        final_status=out.get("final_status") or "",
        final_response=out.get("final_response") or "",
        step_results=list(out.get("step_results") or []),
        session_id=session_id,
        request_id=envelope["request_id"],
        trace_id=trace_id,
        latency_ms=int((time.monotonic() - t0) * 1000),
        interrupt=_sr_offer,
    )


__all__ = ["build_app", "create_app", "TurnResponse"]
