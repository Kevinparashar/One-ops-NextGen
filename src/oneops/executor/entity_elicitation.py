"""Entity elicitation — slot-filling for a missing required entity reference.

When a query needs a record but names none ("summarize my ticket", "similar
tickets"), the selected tool's required entity-shaped parameter (`ticket_id`)
is unbound. Rather than dispatch-and-fail, the executor asks the user which
record they mean (the conversational interrupt protocol) and resolves their
reply here.

The reply is rarely a clean id. People say "my last ticket", "the previous
one", "the VPN one". Resolution is layered, LLM-led, and grounded on the user's
OWN records — never a phrase/keyword catalog (§2.1), never a hardcoded id:

  1. literal id      → the data-driven `EntityIdNormalizer` (`id_prefix→service`
                       from service-schema). Deterministic, no LLM.
  2. contextual      → the LLM picks the single record the reply refers to from
                       the user's recent candidates, judging by MEANING
                       (recency / title topic / type / status / direct id). §2.2.
  3. unresolved      → surfaced as such; the orchestrator asks again rather than
                       guess (§2.7 no silent default).

This module is pure orchestration + a thin gateway adapter, both injectable so
unit tests run with zero infrastructure (a fake picker, a small normalizer).
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import get_logger, get_tracer, increment

_log = get_logger(__name__)
_tracer = get_tracer(__name__)

# A picker maps (reply, candidates) → the chosen candidate id, or "" when the
# reply identifies no single record. Injected so resolution is testable without
# a gateway; the production implementation is `CandidatePicker.pick`.
Picker = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a user's reply to a concrete record.

    `method` records HOW it resolved (literal | llm | none) for telemetry;
    `reason` carries the not-resolved cause for the operator + the re-ask."""

    resolved: bool
    ticket_id: str = ""
    service_id: str = ""
    method: str = "none"
    reason: str = ""


async def resolve_reply(
    *,
    reply: str,
    candidates: list[dict[str, Any]],
    normalizer: Any,
    pick: Picker,
) -> Resolution:
    """Resolve a clarification reply to a single record (layered, LLM-led).

    `normalizer` is an `EntityIdNormalizer` (literal-id layer); `candidates`
    are the user's recent records (`ticket_id`/`service_id`/`title`/`status`)
    the LLM `pick` grounds on. Never raises — an unresolved reply is a value,
    not an exception."""
    text = (reply or "").strip()
    if not text:
        return Resolution(resolved=False, reason="empty reply")

    # Layer 1 — a literal id wins outright: deterministic, no LLM, and the
    # service comes from the same data-driven prefix map the router uses.
    extracted = normalizer.extract(text)
    if extracted.has_entities:
        e = extracted.entities[0]
        return Resolution(resolved=True, ticket_id=e.entity_id,
                          service_id=e.service_id, method="literal")

    # Layers 2/3 — a contextual reply ("last one", "the VPN ticket") is
    # resolved against the user's OWN records by the LLM. With nothing to
    # ground on, do not guess.
    if not candidates:
        return Resolution(resolved=False,
                          reason="no recent records to resolve against")
    try:
        chosen = (await pick(text, candidates) or "").strip()
    except Exception as exc:                                       # noqa: BLE001
        _log.warning("entity_elicitation.pick_failed", error=str(exc)[:160])
        return Resolution(resolved=False, reason="resolver error")
    if not chosen:
        return Resolution(resolved=False,
                          reason="reply did not identify a known record")
    by_id = {str(c.get("ticket_id")): c for c in candidates}
    match = by_id.get(chosen)
    if match is None:
        # The model returned something outside the grounded set — reject it
        # rather than act on a fabricated id (§2.7).
        return Resolution(resolved=False,
                          reason="resolver chose an id outside the candidate set")
    return Resolution(resolved=True, ticket_id=str(match["ticket_id"]),
                      service_id=str(match.get("service_id") or ""),
                      method="llm")


# ── LLM candidate picker (principle-based prompt, no phrase catalog) ─────────

# Principle, not phrasebook: the model resolves by the MEANING of the reply
# against real candidate data. It enumerates no phrases ("last" → …) and hard-
# codes no ids — so it generalises to any wording in production (§2.1/§2.2).
_PICK_SYSTEM_PROMPT = """You identify which work record a user means in a \
follow-up reply.

You are given the user's reply and a numbered list of candidate records the \
user is a party to, most-recent first — each with an id, type, short title, \
and status.

Choose the SINGLE candidate the reply points to, judging by the MEANING of the \
reply against the candidates' attributes:
  • recency or position  — "the latest", "the last one", "the one before that"
  • topic                — words in the reply that match a record's title
  • type or status       — "my open incident", "the pending request"
  • direct reference     — the id itself
Resolve by understanding the reply, not by matching keywords.

Output strict JSON only, no prose:
  {"ticket_id": "<the exact id of the chosen candidate>"}   when one candidate \
clearly matches, or
  {"ticket_id": null}                                       when the reply is \
ambiguous, matches several, or matches none.

Never output an id that is not in the list. When unsure, output null — the \
assistant will ask again rather than act on a guess."""


def _render_candidates(candidates: list[dict[str, Any]]) -> str:
    """One compact line per candidate — the grounded set the model chooses
    from. Order is preserved (callers pass most-recent-first)."""
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(
            f'{i}. id={c.get("ticket_id", "")} '
            f'type={c.get("service_id", "")} '
            f'status={c.get("status", "")} '
            f'title="{c.get("title", "")}"'
        )
    return "\n".join(lines)


def build_pick_messages(
    reply: str, candidates: list[dict[str, Any]],
) -> tuple[LlmMessage, ...]:
    """System (cacheable principle) + user (the reply + grounded candidates)."""
    user_block = (
        f"User reply: {reply.strip()}\n\n"
        f"Candidates (most recent first):\n{_render_candidates(candidates)}"
    )
    return (
        LlmMessage("system", _PICK_SYSTEM_PROMPT, cache_control=True),
        LlmMessage("user", user_block),
    )


def parse_pick(raw: str, candidates: list[dict[str, Any]]) -> str:
    """Strict parse of the picker's JSON → a candidate id, or "" when null /
    malformed / outside the grounded set. Never raises (§2.7)."""
    try:
        doc = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    chosen = doc.get("ticket_id")
    if not isinstance(chosen, str) or not chosen.strip():
        return ""
    chosen = chosen.strip()
    valid = {str(c.get("ticket_id")) for c in candidates}
    return chosen if chosen in valid else ""


class CandidatePicker:
    """Gateway-backed `Picker` — turns a contextual reply + candidates into a
    chosen id via one structured LLM call. Degrades to "" (no pick) on any
    gateway/parse failure; never raises."""

    def __init__(self, *, gateway: LlmGateway, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model

    async def pick(
        self, reply: str, candidates: list[dict[str, Any]],
        *, tenant_id: str = "", user_id: str = "",
    ) -> str:
        if not reply or not candidates:
            return ""
        with _tracer.start_as_current_span(
            "executor.entity_elicitation.pick",
            attributes={"oneops.tenant_id": tenant_id,
                        "llm.model": self._model,
                        "oneops.candidate_count": len(candidates)},
        ) as span:
            try:
                resp = await self._gateway.call(LlmRequest(
                    messages=build_pick_messages(reply, candidates),
                    model=self._model, tenant_id=tenant_id, user_id=user_id,
                    response_format=ResponseFormat.JSON,
                    max_tokens=40, temperature=0.0,
                ))
                chosen = parse_pick(resp.content or "", candidates)
            except Exception as exc:                                  # noqa: BLE001
                _log.warning("entity_elicitation.pick_call_failed",
                             error=str(exc)[:160])
                span.set_attribute("error", True)
                return ""
            span.set_attribute("oneops.resolved", bool(chosen))
            return chosen


# ── Orchestrator: detect-already-done → ask → resolve → bind ────────────────
#
# `step_runner` detects the missing required entity slot (it owns the
# entity-shaped param set) and calls `maybe_elicit_entity` with the param name.
# This module then fetches the user's recent candidates, raises the
# conversational interrupt, and on resume resolves the reply into bindings.

# Gateway is injected at app startup (mirrors `set_ticket_store`). When unset
# (no-infra executor, a missed wiring), the picker is a no-op and only the
# literal-id layer resolves — the feature degrades, never crashes.
_gateway: LlmGateway | None = None
_normalizer_cache: Any = None


def set_elicitation_gateway(gateway: LlmGateway | None) -> None:
    """Wire the process gateway used for contextual reply resolution."""
    global _gateway
    _gateway = gateway


class _NullPicker:
    """Picker used when no gateway is wired — resolves nothing contextual."""

    async def pick(self, reply: str, candidates: list[dict[str, Any]],
                   *, tenant_id: str = "", user_id: str = "") -> str:
        return ""


def _get_normalizer() -> Any:
    """Process-wide `EntityIdNormalizer` (data-driven `id_prefix→service`)."""
    global _normalizer_cache
    if _normalizer_cache is None:
        from oneops.router.entity_id import EntityIdNormalizer
        _normalizer_cache = EntityIdNormalizer.from_registry_file()
    return _normalizer_cache


def _get_picker() -> Any:
    return CandidatePicker(gateway=_gateway) if _gateway is not None else _NullPicker()


def _clarification_interrupt(question: str, hints: list[str]) -> Any:
    """Pause the turn with an open-ended clarification. Mirrors the payload
    contract of `executor.nodes.interrupt_for_clarification` — called directly
    (not imported) to keep this module free of an executor.nodes import cycle.
    Returns `{"answer": <text>}` on resume."""
    from langgraph.types import interrupt  # local: avoid import-time cost
    return interrupt({"kind": "user_clarification",
                      "question": question, "hints": hints})


def _answer_text(answer: Any) -> str:
    """Pull the user's free-text reply out of the resume payload, whatever
    shape the frontend sent (a string, `{answer}`, or a selected option)."""
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, dict):
        for k in ("answer", "value", "text", "reply", "ticket_id", "id"):
            v = answer.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _build_question(candidates: list[dict[str, Any]]) -> str:
    """The clarification text. General and record-agnostic — it invites either
    an id or a description, and never enumerates parsing phrases (§2.1)."""
    if candidates:
        return ("Which record do you mean? You can tell me its id, or describe "
                "it — for example “my last ticket” or “the open "
                "one”.")
    return ("Which record should I use? Please tell me its id "
            "(for example an incident or request number).")


async def elicit_entity(
    *,
    param_name: str,
    service_param: str,
    context: dict[str, Any],
    store: Any,
    normalizer: Any,
    picker: Any,
    interrupt_fn: Callable[[str, list[str]], Any] = _clarification_interrupt,
) -> dict[str, str] | None:
    """Ask the user which record they mean, resolve their reply, and return the
    parameter bindings to merge — or `None` when the reply can't be resolved
    (the handler then surfaces its own graceful 'needs an id' message; never a
    silent guess, §2.7).

    Raises `GraphInterrupt` on the FIRST pass (the question). LangGraph replays
    this call on resume, when `interrupt_fn` returns the user's reply instead.
    """
    tenant_id = str(context.get("tenant_id") or "")
    user_id = str(context.get("user_id") or "")

    # Candidates power both the hint chips and the contextual resolver. A read
    # failure must not block the ask — degrade to no candidates (id still works).
    candidates: list[dict[str, Any]] = []
    try:
        candidates = await store.list_recent_for_user(
            tenant_id=tenant_id, user_id=user_id, limit=5)
    except Exception as exc:                                       # noqa: BLE001
        _log.warning("entity_elicitation.recent_read_failed", error=str(exc)[:160])

    hints = [str(c.get("ticket_id")) for c in candidates[:3] if c.get("ticket_id")]
    # FIRST pass: this raises GraphInterrupt and the turn pauses. On resume the
    # same call returns the reply, and execution continues below.
    answer = interrupt_fn(_build_question(candidates), hints)

    reply = _answer_text(answer)

    async def _pick(r: str, c: list[dict[str, Any]]) -> str:
        return await picker.pick(r, c, tenant_id=tenant_id, user_id=user_id)

    res = await resolve_reply(reply=reply, candidates=candidates,
                              normalizer=normalizer, pick=_pick)

    increment("ai.elicitation.outcome",
              tenant_id=tenant_id,
              method=res.method if res.resolved else "unresolved")

    if not res.resolved:
        _log.info("entity_elicitation.unresolved", reason=res.reason,
                  reply=reply[:80])
        return None

    bindings = {param_name: res.ticket_id}
    if service_param and res.service_id:
        bindings[service_param] = res.service_id
    _log.info("entity_elicitation.resolved", method=res.method,
              ticket_id=res.ticket_id, service_id=res.service_id)
    return bindings


async def maybe_elicit_entity(
    *,
    param_name: str,
    service_param: str,
    context: dict[str, Any],
    interrupt_fn: Callable[[str, list[str]], Any] = _clarification_interrupt,
) -> dict[str, str] | None:
    """High-level entrypoint for the step runner: builds the default
    dependencies (process ticket store, normalizer, gateway-backed picker) and
    runs the elicitation. Thin so the step runner stays a one-liner."""
    from oneops.use_cases._shared.ticket_store import get_ticket_store
    return await elicit_entity(
        param_name=param_name, service_param=service_param, context=context,
        store=get_ticket_store(), normalizer=_get_normalizer(),
        picker=_get_picker(), interrupt_fn=interrupt_fn)


__all__ = [
    "Resolution",
    "Picker",
    "resolve_reply",
    "build_pick_messages",
    "parse_pick",
    "CandidatePicker",
    "set_elicitation_gateway",
    "elicit_entity",
    "maybe_elicit_entity",
]
