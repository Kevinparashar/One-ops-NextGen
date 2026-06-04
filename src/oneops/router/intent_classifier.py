"""Focus-intent classifier — a small LLM call that decides whether the
current turn is asking about a property of the focused record (axis A) or
asking for external knowledge content (axis B).

Runs in the focus-update node BEFORE the disambiguator, so the focus state
reaching disambiguator is already correct for topic-switch cases. This
removes the need for the disambiguator to disambiguate the focus-vs-topic
tension itself.

Production discipline:
  • No keyword lists, no example queries — principle only
  • LLM judgement, cached per (focus_service, normalised_message) hash
  • Cache TTL 24h — same phrasings recur often inside one chat session
  • Falls back to "unknown" on any error → focus is preserved (current
    legacy behaviour), so a classifier failure never breaks routing
  • Cost ~$0.00002 per call on gpt-4o-mini; cached calls are free

Returned values:
  "field"   — keep focus (axis A intent)
  "topic"   — drop focus (axis B intent)
  "unknown" — preserve focus (fall back to existing behaviour)
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from oneops.llm.gateway import LlmGateway
from oneops.llm.models import LlmMessage, LlmRequest, ResponseFormat
from oneops.observability import get_logger, get_tracer

_log = get_logger(__name__)
_tracer = get_tracer(__name__)

IntentLabel = Literal["field", "topic", "unknown"]

# Cache TTL — same phrasings recur within a chat session window
_CACHE_TTL_S = 24 * 3600

_SYSTEM_PROMPT = """You decide the focus-intent of a chat turn for an ITSM \
assistant. A prior turn established a "focus entity" — a specific record \
(incident, request, KB article, etc.) the user has been discussing.

For the CURRENT turn, decide ONE of:

  • "field"
    The user is asking for the VALUE of an attribute, property, status, or \
    linked-record-id that is STORED on the focused entity itself. The answer \
    has to come from the focused record's own data — its own fields.

  • "topic"
    The user is asking for external knowledge content — material that exists \
    as a separately authored resource (documentation, articles, write-ups, \
    runbooks, procedures, troubleshooting guides) about a topic, technology, \
    service, or problem area. The answer has to come from a written-up \
    knowledge source, not the focused record's own fields. The fact that the \
    topic overlaps with what the focused entity concerns does NOT make this \
    "field" — the answer source is different.

  • "unknown"
    Neither applies — off-topic, greeting, genuinely ambiguous, or the \
    intent cannot be classified from the text alone.

Apply the principle. The presence of the focused entity's topical keywords \
inside a knowledge-content question does NOT make it "field". The presence \
of an attribute name does NOT make it "topic". Reason from what the answer \
source must be: the focused record's own data, vs separately authored \
content.

Output strict JSON only:
{"intent": "field" | "topic" | "unknown"}"""


def _cache_key(message: str, focus_service: str) -> str:
    """Deterministic cache key. Lowercased + stripped to normalise phrasing
    variants ('What Is Its Category?' vs 'what is its category')."""
    norm = (message or "").strip().lower()
    h = hashlib.sha256(f"{focus_service}|{norm}".encode()).hexdigest()[:16]
    return f"router:intent:{h}"


class FocusIntentClassifier:
    """Small LLM-driven classifier with Dragonfly caching."""

    def __init__(
        self, *, gateway: LlmGateway, cache=None,
        model: str = "gpt-4o-mini",
    ) -> None:
        self._gateway = gateway
        self._cache = cache
        self._model = model

    async def classify(
        self, *, message: str, focus_entity_id: str, focus_service: str,
        tenant_id: str, user_id: str = "",
    ) -> IntentLabel:
        if not message or not focus_entity_id:
            return "unknown"

        ck = _cache_key(message, focus_service)
        if self._cache is not None:
            try:
                cached = await self._cache.get(ck)
                if cached in ("field", "topic", "unknown"):
                    return cached  # type: ignore[return-value]
            except Exception:                                       # noqa: BLE001
                pass

        with _tracer.start_as_current_span(
            "router.focus_intent.classify",
            attributes={
                "oneops.tenant_id": tenant_id,
                "oneops.focus.entity_id": focus_entity_id,
                "oneops.focus.service_id": focus_service,
                "llm.model": self._model,
            },
        ) as span:
            user_block = (
                f"Focus entity: {focus_entity_id}\n"
                f"Focus service: {focus_service or 'unknown'}\n"
                f"Current message: {message}"
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
                    max_tokens=20, temperature=0.0,
                ))
                doc = json.loads(response.content or "{}")
                label: IntentLabel = doc.get("intent", "unknown")
                if label not in ("field", "topic", "unknown"):
                    label = "unknown"
                span.set_attribute("router.focus_intent.label", label)
            except Exception as exc:                                # noqa: BLE001
                _log.warning("router.focus_intent.classify_failed",
                             error=str(exc)[:160])
                span.set_attribute("router.focus_intent.label", "unknown")
                label = "unknown"

            if self._cache is not None and label != "unknown":
                try:
                    await self._cache.set(ck, label, ttl=_CACHE_TTL_S)
                except Exception:                                   # noqa: BLE001
                    pass
            return label


__all__ = ["FocusIntentClassifier", "IntentLabel"]
