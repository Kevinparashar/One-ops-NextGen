"""Stage-1 conversation-control gate — pre-router, LLM-driven, cached.

Handles non-task social/meta utterances (greetings, thanks, acks,
farewells, help inquiries, noise) BEFORE the router / disambiguator /
planner sees the message. A turn classified as conversational is
answered with a fixed, canned response — instant, zero token cost on
cache hit, never fabricated.

Architecture (adapted from AI-oneops `stage1_conversation_control`):

  1. **Structural guards** (no LLM): empty / whitespace, pure punctuation,
     pure emoji → canned `noise` reply.
  2. **Canonical-ID fast-path**: any token shaped like a record id
     (INC0001234, REQ0001234, KB0005010, …) means a task — bypass the
     gate entirely, let the router run.
  3. **LLM semantic classifier**: one short gateway call, temperature 0,
     max 8 tokens. The LLM returns a label from a fixed enum. Result
     cached in Dragonfly under `oneops:control:v1:` with 7-day TTL,
     keyed by MD5 of the normalised message.

Why pre-router and not a post-router responder:
  * **Latency + cost.** A pure greeting today goes through retrieval,
    stage-3 filter, stage-4 LLM disambiguator, and finally lands at the
    boundary responder — 1–2 LLM calls + Dragonfly lookups. The gate
    short-circuits to ZERO LLM calls on a cache hit and ONE call cold.
  * **Naturalness.** Eight fine-grained labels (greeting, thanks, …)
    each get an idiomatic reply — "You're welcome." vs "Hello!" — that
    the boundary's four buckets can't express.
  * **State protection.** The gate NEVER touches focus / active subject
    / pending clarification. A "thanks!" mid-conversation preserves the
    prior turn's focus so a subsequent "what's the priority" still
    binds.

Discipline (Moveworks / Parlant inspired):
  * **No keyword catalog.** Detection is semantic; the LLM judges
    intent regardless of phrasing or language.
  * **Abstain on uncertainty.** A false positive (classifying a task as
    conversational) silently drops the user's request — the worst
    outcome. The prompt explicitly biases toward `none` when uncertain.
  * **No state mutation.** The gate's only effect is the canned reply.
  * **Policy layer applies.** The LLM call rides `Profile.PLATFORM_SYSTEM`
    so every safety + scope rule from `updated_policy_v2.md` is in the
    system prompt.
  * **Tenant-prefixed cache key.** Dragonfly key includes tenant_id so
    one tenant's cache never serves another tenant's classification.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from oneops.observability import get_logger, get_tracer

_log = get_logger("oneops.conversation.control")
_tracer = get_tracer("oneops.conversation.control")


ControlType = Literal[
    "none",
    # Social openings / closings
    "greeting",
    "wellbeing_check",
    "farewell",
    # Gratitude / acknowledgement / compliment / apology / frustration
    "thanks",
    "acknowledgement",
    "compliment",
    "apology",
    "frustration",
    # Meta about the assistant
    "identity",
    "help_inquiry",
    "capabilities_inquiry",
    # Casual conversation off the social opening
    "chitchat",
    # Genuinely out-of-domain — weather, sports, recipes, etc. The
    # gate emits the canonical OOS literal directly so the boundary
    # responder never has to.
    "out_of_scope",
    # Structural (no LLM)
    "noise",
]


@dataclass(frozen=True)
class ConversationControlResult:
    """Outcome of the Stage-1 gate.

    `is_control=True` ⇒ the gate produced the user-facing reply; the
    executor must short-circuit (no routing). `is_control=False` ⇒
    fall through to the normal route → wave → run_step pipeline.
    """
    is_control: bool
    control_type: ControlType
    response_text: str | None
    source: str   # "punctuation_only" | "emoji_only" | "whitespace_only"
                  # | "llm_classifier" | "cache" | "fallthrough"


# ── canned responses — one per label, deterministic ───────────────────────
#
# These are the ONLY pre-defined strings in the gate. They're not decision
# logic — they're the user-facing reply once the LLM has classified intent.
# Adding a new label = (1) extend ControlType, (2) add to _RESPONSES,
# (3) add to the prompt's enum.

_RESPONSES: dict[str, str] = {
    "greeting":             "Hello. How can I help you with your ITSM work today?",
    "wellbeing_check":      "I'm doing well, thanks. How can I help with your "
                            "ITSM work today?",
    "thanks":               "You're welcome.",
    "acknowledgement":      "Got it.",
    "farewell":             "Goodbye.",
    "compliment":           "Thank you. Happy to help — what else do you need?",
    "apology":              "No problem at all. What can I help you with?",
    "frustration":          "I understand — let's work through it. Tell me "
                            "what's going wrong and I'll help.",
    "identity":             "I'm OneOps, an AI assistant for IT, ITSM, and "
                            "ITOM. I can summarise tickets, search the "
                            "knowledge base, and help you navigate "
                            "incidents, requests, problems, changes, and "
                            "configuration items. What can I help with?",
    "help_inquiry":         "I help with ITSM work — tickets, requests, "
                            "problems, changes, knowledge articles, assets, "
                            "and CMDB records. Try asking about a specific "
                            "ticket (e.g. 'summarize INC0001001') or "
                            "describing an issue.",
    "capabilities_inquiry": "I can summarise any incident / request / "
                            "problem / change / asset / CMDB record, search "
                            "the knowledge base, and answer follow-up "
                            "questions about a ticket's fields. Share a "
                            "record id or describe the issue and I'll take "
                            "it from there.",
    "chitchat":             "Happy to chat, but I'm really only useful for "
                            "your ITSM work. Got a ticket I can help with?",
    "out_of_scope":         "You are asking questions that are out of my "
                            "scope. Please ask your questions within the "
                            "ITSM/ITOM domain.",
    "noise":                "Please share what you need help with.",
}


# ── structural guards (no LLM) ────────────────────────────────────────────

_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)
# Pure-emoji detection — any glyph in common emoji ranges, no letters/digits.
_EMOJI_ONLY_RE = re.compile(
    r"^[\s\U0001F300-\U0001FAFF\U00002600-\U000027BF]+$",
    re.UNICODE,
)
# Canonical-ID shape — generic prefix+digit pattern; matches every entity
# type past, present, and future (registry-shape, not registry-content).
_CANONICAL_ID_RE = re.compile(r"\b[A-Z]{2,6}\d{4,}\b", re.IGNORECASE)


# ── classifier prompt ─────────────────────────────────────────────────────

_VALID_LABELS = frozenset(_RESPONSES.keys()) | {"none"}

_CACHE_NAMESPACE = "oneops:control:v4"
_CACHE_TTL_SECONDS = 7 * 24 * 3600

_PROMPT = """Classify the user's short message into EXACTLY ONE label.

Social / closing labels:
- greeting             : a pure social opening (any phrasing, any language).
                         e.g. "hi", "hello there", "good morning",
                         "hey assistant", "namaste".
- wellbeing_check      : asking how the assistant is doing socially.
                         e.g. "how are you", "you good?", "how's it going".
- farewell             : pure closing with no other request.
                         e.g. "bye", "see you later", "we're done", "thanks
                         and bye".

Gratitude / acknowledgement / sentiment labels:
- thanks               : pure gratitude with no other request.
                         e.g. "thanks", "thank you", "appreciate it".
- acknowledgement      : short confirmation that the user understood or
                         accepted what was just said. e.g. "ok", "got it",
                         "sure", "alright", "noted", "fine".
- compliment           : praise of the assistant or its work.
                         e.g. "nice work", "good job", "you're helpful".
- apology              : the user is apologising (often for a typo, a
                         misclick, or earlier confusion).
                         e.g. "sorry", "my bad", "apologies for that".
- frustration          : the user is venting about a problem in general
                         terms, with NO specific entity to act on.
                         e.g. "this is so annoying", "ugh", "I'm
                         frustrated". If they name a ticket or describe a
                         concrete symptom, that's `none` — a real request.

Meta-about-the-assistant labels:
- identity             : "who are you", "are you a bot", "what's your
                         name", "are you AI". Pure identity question.
- help_inquiry         : asking what the assistant can do / how to use it /
                         what topics it covers (META question about the
                         assistant itself). e.g. "what can you do", "how
                         do I use this", "what areas do you cover".
- capabilities_inquiry : a more specific feature inventory ask — "list
                         what you can do", "what tools do you have",
                         "what record types do you support". Treat as
                         help_inquiry when ambiguous.

Off-topic chat:
- chitchat             : casual chat that's clearly not ITSM and not a
                         pure greeting/thanks/farewell. e.g. "what did
                         you do today", "tell me something fun". Keep the
                         response polite but redirect to ITSM.
- out_of_scope         : a SUBSTANTIVE question that is genuinely outside
                         the IT/ITSM/ITOM domain. STRICTLY limited to
                         these categories:
                           * weather, sports, news, politics
                           * recipes, food, restaurants, travel
                           * jokes, entertainment, music, movies
                           * personal advice unrelated to work
                           * creative tasks ("write me a poem",
                             "translate this")
                           * general-knowledge trivia ("who won the
                             World Cup", "capital of France")

                         CRITICAL — IT how-to questions are ALWAYS
                         IN-DOMAIN, return `none`. Even when OneOps
                         itself may not have the article. Examples that
                         MUST be `none` (NOT out_of_scope):
                           * "how do I reset my LDAP password"
                           * "how do I configure kubernetes pod
                             autoscaling"
                           * "why is my outlook so slow"
                           * "vpn keeps dropping"
                           * "how do I install python on the build
                             server"
                           * "what is SSO" / "what is MFA"
                           * "active directory not syncing"
                           * "exchange queue depth high"

                         CRITICAL — HOMONYM RESOLUTION. Many ordinary
                         English words have a separate IT meaning. When
                         such a word appears NEXT TO an IT term, take
                         the IT meaning and return `none`. Do NOT let
                         the personal-life sense of the homonym flip
                         your decision to `out_of_scope`.

                           * "sleep" = bedtime OR laptop standby. With
                             "VPN", "laptop", "standby", "wake" nearby
                             it means standby. → `none`
                           * "drop" = dance step OR packet loss. With
                             "Wi-Fi", "tunnel", "connection" nearby it
                             means packet loss. → `none`
                           * "wake" = morning OR resume-from-standby.
                             With "laptop", "from sleep" nearby it means
                             resume. → `none`
                           * "stuck" = emotion OR hung process. With
                             "queue", "build", "deploy" nearby it means
                             a hung resource. → `none`

                         Worked example:
                           input: "documentation about VPN reconnection
                                   after sleep?"
                           reasoning: "documentation about X" is asking
                                      for written IT content. "VPN" is
                                      an IT service. "sleep" is laptop
                                      standby in this context (paired
                                      with "VPN reconnection").
                           answer:  `none` (the router/KB handles it).

                         Rule: if the question names ANY of the
                         following — an IT system, OS, application,
                         service, protocol, device, error, login,
                         password, credential, network, server, cloud,
                         database, container, deployment, monitoring,
                         backup, security, compliance — it is
                         IN-DOMAIN. The router/KB will handle it; the
                         composer will say "no article" if there's no
                         match.

                         When in doubt between out_of_scope and none,
                         choose `none`. A false OOS silently blocks a
                         legitimate IT question; CASE B in the KB
                         composer is the right place to say "no
                         article found."

                         **Focus-aware override (2026-05-29).** When the
                         conversation has an ACTIVE FOCUS RECORD (an
                         incident, problem, change, asset, or CI the
                         user is currently working on), a query whose
                         subject is clearly unrelated to that focused
                         record AND to general IT/ITSM is
                         `out_of_scope`. The off-topic signal is the
                         change of subject, not the verb shape.

                         Examples (use the principle, not the words):
                           * focus = INC0001005 (Exchange mailbox
                             issue), user asks "how to fix the
                             bluetooth connectivity" → `out_of_scope`
                             (bluetooth on a personal device is not
                             on this incident and not an enterprise
                             IT subject).
                           * focus = INC0001005, user asks "lets meet
                             tomorrow now" → `out_of_scope` (meeting
                             scheduling is not ITSM).
                           * focus = INC0001005, user asks "any data
                             on this" → `none` (legitimate follow-up
                             about the focused incident).
                           * focus = INC0001005, user asks "outlook
                             keeps crashing" → `none` (outlook IS the
                             focused incident's subject; legitimate).
                           * no focus, user asks "how do I fix VPN"
                             → `none` (general IT how-to, in-domain).

Catch-all:
- none                 : ANYTHING that is not clearly one of the labels
                         above — any task, request, action, business
                         question, reference to enterprise data, AND any
                         short or single-word message that could be a
                         data query. In an ITSM assistant a terse message
                         like "priority" or "status" is almost always a
                         field-read on the active focus, not nonsense.

Decision principles (apply in order):
1. **Intent, not keywords.** A message that contains "hello" but asks a
   task ("hello, please summarize INC0001001") is `none`, not `greeting`.
2. **Multilingual.** Classify by semantic intent regardless of language.
3. **Business reference → none.** If the message references any business
   entity, data point, action, ticket id, or specific concrete symptom,
   return `none`. The gate is for purely social/meta messages only.
4. **Terse words → none.** Any single English word that names an ITSM
   field, service, or concept (priority, status, sla, breached, owner,
   assignee, vpn, outlook, password, salesforce, mfa, …) is `none`. The
   downstream router will handle it as a data query.
5. **Mixed messages → none.** When the user does a task AND a social
   thing in one turn ("thanks, now summarize INC0001234"), the task wins:
   return `none`. The boundary's own greetings still feel natural
   because the task reply comes first.
6. **ABSTAIN ON UNCERTAINTY.** A false positive (classifying a task as
   conversational) silently drops the user's request — the worst
   outcome. When uncertain between a conversational label and `none`,
   ALWAYS return `none`.

Reply with EXACTLY one label word (snake_case if multi-word). No
punctuation, no explanation, no markdown.

{focus_block}User message: {message}

Label:"""


# ── classifier with cache ────────────────────────────────────────────────

class ControlClassifier(Protocol):
    """LLM classifier + cache contract. The executor calls `classify(text)`
    once per turn; impls are responsible for cache lookup, LLM call (when
    needed), and cache write-back."""
    async def classify(
        self, *, message: str, tenant_id: str, user_id: str = "",
        request_id: str = "",
        focus_entity_id: str = "", focus_service_id: str = "",
    ) -> str | None: ...


class _AbstainingClassifier:
    """Fallback used when no real classifier is wired (no gateway).

    Always returns `None` — the gate falls through to the normal pipeline.
    Greetings still get answered by the post-router boundary; we just
    lose the latency + cost win on this turn."""
    async def classify(
        self, *, message: str, tenant_id: str, user_id: str = "",
        request_id: str = "",
        focus_entity_id: str = "", focus_service_id: str = "",
    ) -> str | None:
        return None


def _normalize(message: str) -> str:
    s = (message or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[?.!,;:]+$", "", s)
    return s


def _cache_key(*, tenant_id: str, message: str) -> str:
    h = hashlib.md5(_normalize(message).encode("utf-8")).hexdigest()[:16]
    # Tenant prefix is structural — a cache entry can never leak across
    # tenants (defensive, even though the classification has no business
    # data — different tenants may have different scope conventions).
    return f"{_CACHE_NAMESPACE}:{tenant_id}:{h}"


class DragonflyControlCache:
    """Lazy Dragonfly cache for control-gate verdicts.

    Failure is non-fatal: get returns None, put swallows. The gate
    proceeds to the LLM call. This matches the same discipline as
    `kb_embed._DragonflyEmbedCache`."""

    def __init__(self) -> None:
        self._redis: Any = None
        self._lock = asyncio.Lock()

    async def _client(self) -> Any:
        if self._redis is not None:
            return self._redis
        async with self._lock:
            if self._redis is not None:
                return self._redis
            try:
                import redis.asyncio as aioredis

                from oneops.config import get_settings
                url = getattr(get_settings(), "dragonfly_url",
                              "redis://localhost:6379/0")
                self._redis = aioredis.from_url(url, decode_responses=False)
            except Exception as exc:
                _log.warning("control.cache.init_failed",
                             error=str(exc)[:160])
                self._redis = False
            return self._redis

    async def get(self, *, tenant_id: str, message: str) -> str | None:
        client = await self._client()
        if not client:
            return None
        try:
            raw = await client.get(_cache_key(
                tenant_id=tenant_id, message=message))
        except Exception:
            return None
        if raw is None:
            return None
        v = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        return None if v == "__NONE__" else v

    async def put(self, *, tenant_id: str, message: str,
                  label: str | None) -> None:
        client = await self._client()
        if not client:
            return
        with contextlib.suppress(Exception):
            await client.setex(
                _cache_key(tenant_id=tenant_id, message=message),
                _CACHE_TTL_SECONDS,
                (label or "__NONE__"),
            )


class LlmControlClassifier:
    """Production classifier — gateway-backed LLM call, Dragonfly cache.

    The gateway is the same egress as every other LLM call in the
    system, so policy layer, OTel `llm.call` span, per-tenant cost,
    retries, and LiteLLM proxy routing all apply automatically."""

    def __init__(self, gateway: Any, *, model: str = "gpt-4o-mini",
                 cache: DragonflyControlCache | None = None) -> None:
        self._gateway = gateway
        self._model = model
        self._cache = cache or DragonflyControlCache()

    async def classify(
        self, *, message: str, tenant_id: str, user_id: str = "",
        request_id: str = "",
        focus_entity_id: str = "", focus_service_id: str = "",
    ) -> str | None:
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest
        from oneops.policy import Profile, compose

        if not message or not tenant_id:
            return None

        # Cache key must include focus — "any data on this" with focus
        # INC0001005 is a different decision than the same message with
        # no focus or a different focus.
        cache_key_msg = (
            f"{message}|focus={focus_entity_id}|svc={focus_service_id}"
            if focus_entity_id else message
        )
        cached = await self._cache.get(tenant_id=tenant_id, message=cache_key_msg)
        if cached is not None:
            return cached if cached in _VALID_LABELS else None

        system_prompt = compose(
            Profile.PLATFORM_SYSTEM, extra_sections=[_PROMPT.split("User message:")[0]])
        if focus_entity_id and focus_service_id:
            focus_block = (
                f"ACTIVE FOCUS RECORD (the conversation is currently about):\n"
                f"  entity_id: {focus_entity_id}\n"
                f"  service:   {focus_service_id}\n\n"
            )
        else:
            focus_block = "ACTIVE FOCUS RECORD: (none — fresh session or no entity yet)\n\n"
        user_block = f"{focus_block}User message: {message}\n\nLabel:"

        with _tracer.start_as_current_span(
            "conversation.control.classify",
            attributes={
                "oneops.tenant_id": tenant_id,
                "oneops.user_id": user_id,
            },
        ) as span:
            try:
                resp = await self._gateway.call(LlmRequest(
                    messages=(
                        LlmMessage("system", system_prompt, cache_control=True),
                        LlmMessage("user", user_block),
                    ),
                    model=self._model,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_id=request_id,
                    temperature=0.0,
                    max_tokens=8,
                ))
            except LLMGatewayError as exc:
                span.set_attribute("error", True)
                _log.warning("control.classify.gateway_failed",
                             error=str(exc)[:160])
                return None
            raw = (resp.content or "").strip().lower()
            # Strip surrounding punctuation/quotes; preserve underscores
            # so multi-word labels survive.
            raw = re.sub(r"[^a-z_]", "", raw)
            # `noise` is a STRUCTURAL judgment — never an LLM verdict.
            # An LLM tempted to call a short domain term ("sla?") "nonsense"
            # is wrong; treat as `none` so the message reaches routing.
            if raw == "noise":
                raw = "none"
            if raw not in _VALID_LABELS:
                # Recover near-misses (e.g. "helpinquiry" no underscore).
                squashed = {lbl.replace("_", ""): lbl for lbl in _VALID_LABELS}
                raw = squashed.get(raw, "")
            label = raw if raw in _VALID_LABELS else None
            span.set_attribute("oneops.control.label", label or "none")
            await self._cache.put(
                tenant_id=tenant_id, message=cache_key_msg, label=label)
            return label


_classifier: ControlClassifier = _AbstainingClassifier()


def set_control_classifier(impl: ControlClassifier) -> None:
    """Wire the production classifier at startup; tests inject a stub."""
    global _classifier
    _classifier = impl


def get_control_classifier() -> ControlClassifier:
    return _classifier


async def detect_conversation_control(
    *, message: str, tenant_id: str, user_id: str = "",
    request_id: str = "",
    focus_entity_id: str = "", focus_service_id: str = "",
) -> ConversationControlResult:
    """Run the three-layer pipeline.

    Layer 1 (structural):
      * empty / whitespace-only  → noise
      * pure punctuation         → noise
      * pure emoji               → noise

    Layer 2 (deterministic): canonical-ID present → fall through (task).

    Layer 3 (LLM): classify with cache; non-`none` → canned reply,
    `none` or abstain → fall through.
    """
    raw = message or ""

    # 1a.
    if not raw.strip():
        return ConversationControlResult(
            is_control=True, control_type="noise",
            response_text=_RESPONSES["noise"], source="whitespace_only")
    # 1b.
    if _PUNCT_ONLY_RE.match(raw):
        return ConversationControlResult(
            is_control=True, control_type="noise",
            response_text=_RESPONSES["noise"], source="punctuation_only")
    # 1c.
    if _EMOJI_ONLY_RE.match(raw):
        return ConversationControlResult(
            is_control=True, control_type="noise",
            response_text=_RESPONSES["noise"], source="emoji_only")
    # 2. Canonical-ID shape — it's a task.
    if _CANONICAL_ID_RE.search(raw):
        return ConversationControlResult(
            is_control=False, control_type="none",
            response_text=None, source="fallthrough")
    # 3. LLM semantic classification.
    label = await _classifier.classify(
        message=raw, tenant_id=tenant_id, user_id=user_id,
        request_id=request_id,
        focus_entity_id=focus_entity_id,
        focus_service_id=focus_service_id)
    if not label or label == "none":
        return ConversationControlResult(
            is_control=False, control_type="none",
            response_text=None, source="fallthrough")
    response = _RESPONSES.get(label)
    if not response:
        _log.warning("control.classified_no_template", label=label)
        return ConversationControlResult(
            is_control=False, control_type="none",
            response_text=None, source="fallthrough")
    return ConversationControlResult(
        is_control=True, control_type=label,             # type: ignore[arg-type]
        response_text=response, source="llm_classifier")


__all__ = [
    "ControlType",
    "ConversationControlResult",
    "ControlClassifier",
    "DragonflyControlCache",
    "LlmControlClassifier",
    "detect_conversation_control",
    "get_control_classifier",
    "set_control_classifier",
]
