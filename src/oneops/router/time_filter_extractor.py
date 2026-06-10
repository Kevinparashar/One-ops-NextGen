"""TimeFilterExtractor — LLM-driven structured extraction of time windows.

Sibling to `FocusIntentClassifier`. Same gateway, same cache, same defensive
fall-back semantics. Runs conditionally — only when the router's survivor set
contains an agent whose catalog entry sets `consumes_time_filter: true`.

Per rule §2.1 the LLM is the parser; we do NOT add `dateparser`,
`parsedatetime`, or any natural-language date library. The LLM emits a JSON
shape that maps 1:1 to `oneops.uc_common.TimeFilter`; the Pydantic boundary
rejects malformed output, and we degrade to `None` (no filter) instead of
guessing — that's the §2.7 "no silent defaults" rule.

Production discipline:
  • Cost ~$0.00005 per call on gpt-4o-mini; Dragonfly-cached after first hit.
  • Span-event telemetry for the two ambiguous cases we explicitly track:
      time_filter.unresolved_reference  — "since the outage" without history
      time_filter.year_inferred_past    — emitted by the schema validator
  • Always returns either a valid `TimeFilter` or `None`; never raises.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from typing import Any

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import get_logger, get_tracer
from oneops.uc_common import TimeFilter

# Telemetry literals → constants (sonar S1192).
_ROUTER_TIME_FILTER_OUTCOME = "router.time_filter.outcome"

_log = get_logger(__name__)
_tracer = get_tracer(__name__)

_CACHE_TTL_S = 24 * 3600


# Built into the prompt rather than appended dynamically so the cached
# system-message stays stable across turns (Anthropic / OpenAI prompt caching).
# The "today" anchor IS dynamic (changes daily), so it goes in the user
# message — keeps the system prompt cache-key stable for the whole day.
_SYSTEM_PROMPT = """You extract a structured time window from one chat message. \
The user is asking an ITSM AI assistant about tickets, incidents, or KB \
articles. Decide whether the message references TIME, and if so, emit a JSON \
object describing the window.

Output strict JSON only. No prose. One of these shapes:

  Nothing about time:
    {"time_filter": null}

  Relative window (rolling):
    {"time_filter": {
      "relative_days": <int 1..3650>,
      "label": "<the user's literal phrase>"
    }}

  Absolute window:
    {"time_filter": {
      "start_date": "YYYY-MM-DD",   // optional
      "end_date":   "YYYY-MM-DD",   // optional
      "label":      "<the user's literal phrase>"
    }}

Mapping rules (apply LITERALLY — do not invent dates):

  • "last week"              → relative_days=7
  • "past 7 days"            → relative_days=7
  • "last 30 days"           → relative_days=30
  • "past month" | "recent"  → relative_days=30, label echoes the user's phrase
  • "last quarter"           → relative_days=90, label="last quarter"
  • "last year"              → relative_days=365
  • "older than 6 months"    → end_date = today minus 180 days, no start_date
  • "since May 1"            → start_date=YYYY-05-01 of CURRENT year
  • "in 2025"                → start_date=2025-01-01, end_date=2025-12-31
  • "between March and May"  → both start_date and end_date set, current year
  • "this month"             → start_date=first day of current month

Hard rules:

  • relative_days is mutually exclusive with start_date/end_date — pick ONE.
  • If the reference is contextual and CANNOT be resolved from the message \
alone (e.g. "since the outage", "around when I filed the first one") and the \
user has not stated a date, emit \
    {"time_filter": null, "unresolved_phrase": "<the literal phrase>"}
    Do NOT guess — let the orchestrator clarify.
  • NEVER invent a date that is not derivable from the message. When in \
doubt, emit time_filter: null.
  • DO NOT default to "recent" or "last 30 days" if the user did not mention \
time. The empty case is null, not a window.
"""


def _today_iso() -> str:
    return date.today().isoformat()


def _cache_key(message: str, today: str) -> str:
    norm = (message or "").strip().lower()
    h = hashlib.sha256(f"{today}|{norm}".encode()).hexdigest()[:16]
    return f"router:time_filter:{h}"


def _detect_year_inferred(payload: Mapping, today: date) -> bool:
    """True when a start/end date is more than 7 days in the future. The
    TimeFilter validator rolls such dates back a year; callers use this to emit
    the `time_filter.year_inferred_past` span event."""
    for key in ("start_date", "end_date"):
        v = payload.get(key)
        if isinstance(v, str):
            try:
                if (date.fromisoformat(v) - today).days > 7:
                    return True
            except ValueError:
                continue
    return False


def _parse_response(
    raw: str,
) -> tuple[TimeFilter | None, str | None, bool]:
    """Strict parse → (filter_or_None, unresolved_phrase_or_None, year_inferred).

    `year_inferred` is True when the LLM emitted a date more than 7 days in
    the future and the TimeFilter validator rolled it back a year. Callers
    use it to emit the `time_filter.year_inferred_past` span event.

    Returns (None, None, False) on any malformed input — never raises.
    """
    try:
        doc: Mapping = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return None, None, False

    if not isinstance(doc, Mapping):
        return None, None, False

    unresolved = doc.get("unresolved_phrase")
    if not isinstance(unresolved, str):
        unresolved = None

    payload = doc.get("time_filter")
    if payload is None:
        return None, unresolved, False
    if not isinstance(payload, Mapping):
        return None, unresolved, False

    # Detect future anchors BEFORE validation so the year-inference event is
    # emitted even though the schema silently corrects it.
    year_inferred = _detect_year_inferred(payload, date.today())

    try:
        return TimeFilter(**payload), unresolved, year_inferred
    except Exception:                                                  # noqa: BLE001
        # Schema rejected (mutex violation, malformed date, out-of-range
        # relative_days). Degrade to no filter — rule §2.7 says surface NOT
        # the LLM's bad guess.
        return None, unresolved, False


def _record_time_filter_span(
    span: Any, tf: TimeFilter | None, unresolved: str | None, year_inferred: bool,
) -> None:
    """Operator-visibility span events/attributes for one extraction: an
    unresolved relative phrase, a future-date year roll-back, and the outcome
    (none/present + the filter's own otel attrs)."""
    if unresolved:
        span.add_event(
            "time_filter.unresolved_reference",
            attributes={"phrase": unresolved[:80]},
        )
        span.set_attribute("router.time_filter.unresolved", True)
    if year_inferred and tf is not None:
        span.add_event(
            "time_filter.year_inferred_past",
            attributes={
                "start_date": tf.start_date.isoformat() if tf.start_date else "",
                "end_date": tf.end_date.isoformat() if tf.end_date else "",
                "label": (tf.label or "")[:80],
            },
        )
    if tf is None:
        span.set_attribute(_ROUTER_TIME_FILTER_OUTCOME, "none")
    else:
        span.set_attribute(_ROUTER_TIME_FILTER_OUTCOME, "present")
        for k, v in tf.otel_attrs().items():
            if v is not None:
                span.set_attribute(k, v)


class TimeFilterExtractor:
    """Small LLM-driven extractor with Dragonfly caching.

    Always returns either a validated `TimeFilter` or `None`. Failures
    (gateway errors, malformed JSON, schema rejection) degrade to `None` and
    log a warning — they never break the caller.
    """

    def __init__(
        self, *, gateway: LlmGateway, cache=None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self._gateway = gateway
        self._cache = cache
        self._model = model

    async def extract(
        self, *, message: str, tenant_id: str, user_id: str = "",
    ) -> TimeFilter | None:
        if not message or not message.strip():
            return None

        today = _today_iso()
        ck = _cache_key(message, today)

        # ── Cache fast-path ────────────────────────────────────────────
        hit, cached_tf = await self._cache_lookup(ck)
        if hit:
            return cached_tf

        with _tracer.start_as_current_span(
            "router.time_filter.extract",
            attributes={
                "oneops.tenant_id": tenant_id,
                "llm.model": self._model,
            },
        ) as span:
            user_block = (
                f"Today's date: {today}\n"
                f"Message: {message}"
            )
            try:
                response = await self._gateway.call(LlmRequest(
                    messages=(
                        LlmMessage("system", _SYSTEM_PROMPT, cache_control=True),
                        LlmMessage("user", user_block),
                    ),
                    model=self._model,
                    tenant_id=tenant_id, user_id=user_id,
                    response_format=ResponseFormat.JSON,
                    max_tokens=200, temperature=0.0,
                ))
                tf, unresolved, year_inferred = _parse_response(
                    response.content or "")
            except Exception as exc:                                   # noqa: BLE001
                _log.warning("router.time_filter.extract_failed",
                             error=str(exc)[:160])
                span.set_attribute(_ROUTER_TIME_FILTER_OUTCOME, "error")
                return None

            # ── Span events (operator visibility) ────────────────────
            _record_time_filter_span(span, tf, unresolved, year_inferred)

            # ── Cache write (null and present both cached) ───────────
            await self._cache_write(ck, tf)
            return tf

    async def _cache_lookup(self, ck: str) -> tuple[bool, TimeFilter | None]:
        """Cache fast-path → (hit, value). hit=True means use `value` directly
        (None for a cached NULL); hit=False = miss/decode-error, proceed to LLM."""
        if self._cache is None:
            return False, None
        try:
            cached_raw = await self._cache.get(ck)
            if cached_raw in (b"NULL", "NULL"):
                return True, None
            if isinstance(cached_raw, (str, bytes)) and cached_raw:
                decoded = (cached_raw.decode("utf-8")
                           if isinstance(cached_raw, bytes) else cached_raw)
                return True, TimeFilter(**json.loads(decoded))
        except Exception:                                          # noqa: BLE001
            pass
        return False, None

    async def _cache_write(self, ck: str, tf: TimeFilter | None) -> None:
        """Cache the result (both NULL and present are cached). Best-effort."""
        if self._cache is None:
            return
        try:
            if tf is None:
                await self._cache.set(ck, "NULL", ttl=_CACHE_TTL_S)
            else:
                await self._cache.set(ck, tf.model_dump_json(), ttl=_CACHE_TTL_S)
        except Exception:                                          # noqa: BLE001
            pass


__all__ = ["TimeFilterExtractor"]
