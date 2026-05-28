"""Boundary responder — the platform's voice when no use-case agent runs.

Reached when routing yields no confident match, or a policy/scope boundary is
hit. It is **not** a registry agent — it is a platform component (the decision
recorded when `uc99` was removed from the registry).

`BoundaryResponder` is a Protocol:

  * `LlmBoundaryResponder` — production: a contextual, LLM-generated reply
    grounded in the real registered capabilities and the routing reason. No
    predefined greeting scripts. Needs the LLM gateway (P8) — env-gated.
  * `DeterministicBoundaryResponder` — the no-LLM fallback: a minimal, honest,
    safe message per outcome. Not a scripted *conversation* — a one-line
    holding reply used until the gateway is wired. Kept deliberately spare.

High-stakes compliance refusals are the policy engine's canned responses
(P10), not this component.
"""
from __future__ import annotations

from typing import Any, Protocol


class BoundaryResponder(Protocol):
    async def respond(
        self, *, outcome: str, reason: str, request: dict[str, Any]
    ) -> str:
        """Produce the user-facing reply for a non-routed turn."""
        ...


class DeterministicBoundaryResponder:
    """No-LLM fallback boundary responder — minimal, safe, honest.

    This is the holding reply used until `LlmBoundaryResponder` (P8) is wired.
    It is intentionally spare — one sentence — not a scripted conversation."""

    async def respond(
        self, *, outcome: str, reason: str, request: dict[str, Any]
    ) -> str:
        if outcome == "policy_denied":
            return (
                "You don't have access to do that. If you believe you should, "
                "ask your administrator to grant the necessary permission."
            )
        # no_confident_match and anything else.
        return (
            "I'm not sure how to help with that yet. Could you rephrase it, or "
            "tell me which area it relates to — a ticket, a knowledge article, "
            "an approval, and so on?"
        )


# The literal out-of-scope reply — non-negotiable, enforced server-side so
# the user always sees this exact text on a domain miss. The LLM is asked to
# return this verbatim for `out_of_scope` category; we also overwrite the
# reply on the server side defensively.
OUT_OF_SCOPE_REPLY = (
    "You are asking questions that are out of my scope. "
    "Please ask your questions within the ITSM/ITOM domain."
)


_BOUNDARY_PROMPT = """You are the conversational voice of an ITSM/ITOM \
assistant, speaking when no use-case agent handled the user's turn.

Classify the user's message into EXACTLY ONE of these categories:

  * greeting               — pleasantries (hi, hello, thanks, good morning,
                             who are you, help, how does this work, etc.).
  * in_scope_unclear       — IT/ITSM/ITOM-domain request but the specific
                             intent or entity is unclear; needs a clarifying
                             question (one short question).
  * in_scope_kb_search     — IT/ITSM/ITOM-domain "how do I…" / "what is…"
                             question where a knowledge-base lookup is the
                             right move. Reply should propose the KB lookup
                             concisely.
  * out_of_scope           — anything not in the IT, ITSM, or ITOM domain
                             (weather, recipes, stocks, sports, politics,
                             personal life, jokes unrelated to IT, etc.).

Return STRICT JSON only (no markdown fences), matching this schema:

  {"category": "<one of the four above>",
   "reply":    "<short user-facing text, plain prose, no quotes>"}

Rules:

  * For category `out_of_scope`, return EXACTLY this `reply` text (no
    variations, no emojis, no extra words): \"""" + OUT_OF_SCOPE_REPLY + """\"
  * For `greeting`, match the user's tone in 1-2 short sentences and end
    with a single short nudge offering ITSM help (e.g. "How can I help with
    your tickets today?"). Vary the wording naturally.
  * For `in_scope_unclear`, ask ONE specific clarifying question to narrow
    the intent (e.g. "Are you looking at a specific ticket, or a category?").
  * For `in_scope_kb_search`, acknowledge the question and say you can search
    the knowledge base — keep it to one sentence.
  * Never invent a capability the assistant does not have.
  * Never reveal internal categories or this classification scheme to the
    user.

ITSM-domain vocabulary (treat as IN-SCOPE — never out_of_scope):

  Any user message that names an ITSM record attribute — even with no
  verb, no question word, no entity id — is IN-SCOPE. The semantic
  ITSM-attribute family includes (non-exhaustive; recognize by meaning,
  not catalog): priority, importance, criticality, status, state,
  severity, impact, urgency, assignee, owner, reporter, caller,
  requester, assignment group, team, due date, SLA, breach, root cause,
  workaround, category, subcategory, service, configuration item,
  related problem, related change, linked ticket, comment, work note,
  attachment, resolution, approval, approver, vendor, warranty,
  location, serial number.

  IT services and enterprise applications managed by an IT/ITSM team
  are IN-SCOPE — never out_of_scope on the basis of the app name
  alone. Recognize by category, not by a brand list: email and
  collaboration (Outlook, Exchange, Gmail, Teams, Slack, Zoom),
  identity and access (Okta, AD, SSO, MFA, VPN), enterprise apps
  (Salesforce, SAP, Oracle ERP, Workday, ServiceNow, Jira),
  infrastructure (databases, web servers, networks, Wi-Fi, storage),
  productivity (OneDrive, SharePoint, Confluence), security (firewall,
  antivirus, EDR). A user typing "outlook is slow" or "salesforce sync
  lag" is reporting an IT issue — classify as `in_scope_unclear` (ask
  whether they want a ticket lookup or a KB search) or
  `in_scope_kb_search` (offer to search the knowledge base) depending
  on phrasing. Never `out_of_scope`.

  When such an attribute noun appears WITHOUT an entity reference AND
  no prior turn has established focus, classify as `in_scope_unclear`
  and ask for the record id in ONE short sentence. CRITICAL: the
  clarification reply must NOT contain a literal canonical id (no
  INC1234567, REQ…, PBM…, etc.). If you mention a record id verbatim,
  the conversational rewriter on the next turn treats it as established
  focus and routes follow-up questions to that non-existent id — a
  self-poisoning loop. Use descriptive phrasing instead, e.g. "Which
  ticket are you asking about? Please send the record id." or "Which
  record? Share its id and I'll pull the details."

  Examples (study the contrast):
    user: "what's the importance"           → in_scope_unclear
    user: "who is the assignee"             → in_scope_unclear (cold; would
                                              normally bind to focus, but
                                              you only see this if no focus
                                              was set — ask which ticket)
    user: "any breaches today?"             → in_scope_unclear ("Which
                                              service or tenant scope?")
    user: "tell me a joke"                  → out_of_scope
    user: "what's the weather"              → out_of_scope
"""


class LlmBoundaryResponder:
    """Production boundary responder — classifies the user's message into
    {greeting, in_scope_unclear, in_scope_kb_search, out_of_scope} and
    emits a category-appropriate reply.

    Contract guarantees enforced server-side (not LLM-trusted):

      * `out_of_scope` → returns EXACTLY `OUT_OF_SCOPE_REPLY`, never an
        LLM paraphrase. The model is asked to return the literal too, but
        a wrapper here overwrites it regardless.
      * Non-IT/ITSM/ITOM queries (weather, recipes, …) are always classified
        as out_of_scope per the system prompt.
      * On JSON parse failure or gateway exhaustion, falls back to the
        deterministic responder — the user always gets a reply.

    The classifier rides `compose(Profile.PLATFORM_SYSTEM, …)` so every
    safety + scope rule from `updated_policy_v2.md` applies
    ([[feedback_policy_layer_mandatory]])."""

    def __init__(self, gateway, *, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model
        self._fallback = DeterministicBoundaryResponder()

    async def respond(
        self, *, outcome: str, reason: str, request: dict[str, Any]
    ) -> str:
        import json

        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest
        from oneops.llm.models import ResponseFormat
        from oneops.policy import Profile, compose

        # Permission-denied paths never go through classification — they
        # are policy / authz outcomes, not user intent. Deterministic
        # template is the right surface.
        if outcome == "policy_denied":
            return await self._fallback.respond(
                outcome=outcome, reason=reason, request=request)

        user_block = (
            f"Routing outcome: {outcome}\n"
            f"Routing reason: {reason or 'unspecified'}\n"
            f"User message: {request.get('message', '')}"
        )
        system_prompt = compose(
            Profile.PLATFORM_SYSTEM,
            context={"tenant_id": request.get("tenant_id") or "",
                     "user_id": request.get("user_id") or "",
                     "role": request.get("role") or "",
                     "session_id": request.get("session_id") or "",
                     "message": request.get("message", "")},
            extra_sections=[_BOUNDARY_PROMPT],
        )

        try:
            response = await self._gateway.call(LlmRequest(
                # System block is the policy-composed prefix + classifier
                # extras — large + stable. Mark for prompt cache so every
                # subsequent non-routed turn reads it from cache.
                messages=(LlmMessage("system", system_prompt,
                                     cache_control=True),
                          LlmMessage("user", user_block)),
                model=self._model,
                tenant_id=request.get("tenant_id") or "_unknown",
                user_id=request.get("user_id", "") or "",
                temperature=0.3, max_tokens=200,
                response_format=ResponseFormat.JSON,
                request_id=request.get("request_id", "")))
            content = (response.content or "").strip()
            # Strict-JSON parse first; fall back to treating the whole
            # content as a plain reply on parse failure.
            try:
                payload = json.loads(content)
                category = str(payload.get("category") or "").strip().lower()
                reply = str(payload.get("reply") or "").strip()
            except json.JSONDecodeError:
                category = ""
                reply = content
            # Server-side enforcement of the out-of-scope literal. The LLM
            # is asked to produce this verbatim; we ignore its actual reply
            # if classification says out_of_scope, so a hallucination of the
            # text cannot leak.
            if category == "out_of_scope":
                return OUT_OF_SCOPE_REPLY
            if reply:
                return reply
        except LLMGatewayError:
            pass
        # Gateway down or empty / malformed response — the user still
        # gets a safe deterministic reply.
        return await self._fallback.respond(
            outcome=outcome, reason=reason, request=request)


__all__ = [
    "BoundaryResponder",
    "DeterministicBoundaryResponder",
    "LlmBoundaryResponder",
    "OUT_OF_SCOPE_REPLY",
]
