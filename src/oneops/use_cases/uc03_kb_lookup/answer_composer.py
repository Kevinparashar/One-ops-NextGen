"""UC-3 grounded answer composer — Prompt 2 of the user's classifier/answer
two-stage design.

After `search_kb` runs and returns ranked KB previews, this module composes
the user-facing reply. It is BOUND BY THE KB CONTENT — never invents an
answer, never falls back to general knowledge.

Two cases:
  * CASE A — at least one retrieved article is relevant to the user's
    query. The composer writes a short direct answer + concise detail,
    citing the article id(s). The detail comes ONLY from the article
    content the composer was given.
  * CASE B — no retrieved article is relevant (or none was retrieved).
    The composer states honestly that nothing matched, suggests a
    sensible next step (rephrase / contact IT support), and stops.

Production wiring (all paths automatic when the gateway is set):
  * LiteLLM proxy: the gateway routes the call through `LiteLLMTransport`.
  * OTel: gateway opens an `llm.call` span with tenant_id + user_id.
  * Per-tenant cost: gateway's `_cost.record(...)` charges input+output
    tokens to the caller's tenant.
  * Policy: composes through `Profile.INTERNAL_AGENT`; safety/scope
    blocks apply automatically.
  * Cache: the composed reply is not cached today (the search result
    already varies by tenant+role+audience; caching at this layer
    would need a 4-tuple key — out of scope for v1). The embeddings
    cache from Phase 1 still fires for the embed step.

Failure mode: if the LLM gateway is unavailable, the deterministic
fallback emits a plain "found N articles — see KBxxxxxxx, …" reply.
Never a fabricated paragraph.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from oneops.observability import get_logger, get_tracer

_log = get_logger("oneops.use_cases.uc03.answer_composer")
_tracer = get_tracer("oneops.use_cases.uc03.answer_composer")


# Same string the user authored. The composer enforces it server-side too
# (no LLM fabrication slip-through): a deterministic fallback uses a
# similarly-shaped reply.
OUT_OF_SCOPE_REPLY_HINT = (
    "This request is outside the scope of OneOps. I can help with IT, "
    "ITSM, and ITOM topics — service requests, incidents, IT operations, "
    "and related questions."
)


_ANSWER_PROMPT = """You are an enterprise ITSM Knowledge Base Chat \
Composer.

Your job is to answer the user's question using the retrieved KB \
articles in a clear, concise, professional, and support-ready chat \
response. The response must be readable in a chat interface, not \
formatted like a long formal report.

Use only the retrieved KB article content. Do not invent missing \
information. Do not add generic troubleshooting unless it exists in the \
retrieved KB article.

GLOBAL RULES
1. Use retrieved KB content only.
2. Do not invent causes, steps, commands, warnings, versions, URLs, or \
escalation paths.
2a. Do not supplement KB content with general knowledge from your \
training. Even when you know the answer, only use information present \
in the retrieved KB article.
3. Do not expose retrieval internals, vector search, embeddings, \
scores, ranking logic, or tool behavior.
4. Keep the response concise and chat-friendly.
5. Render only information that exists in the retrieved KB article.
6. Do not add "Not specified" sections.
7. Omit absent sections entirely.
8. Do not force KB content into formal sections that are not present \
in the article.
9. If the user asks a broad question and multiple KBs match, show each \
KB separately.
10. If multiple KBs match but the correct one cannot be confidently \
selected, ask exactly one clarifying question.
11. RETRIEVAL TRUST RULE. You are a renderer, not a relevance judge. The \
retrieval system upstream has already established that the retrieved \
articles are relevant to the user — by topical similarity (text search) \
OR by explicit record linkage (the article is linked to the ticket / CI \
the user referenced). DO NOT second-guess relevance based on whether the \
user's literal wording matches the article's title or topic. When the \
user asks "find KB articles related to <ticket id>" or "any docs linked \
to <this incident>", the linkage IS the match; render the linked \
article(s) normally with title, body, and source citation.

   Emit the no-match template ONLY when the retrieval returned an empty \
article list (no articles were retrieved at all). When one or more \
articles are present, render them per the rules above.

   The no-match template (use only when the article list is empty):
    No matching knowledge-base article was found for "{user_query}". \
Try rephrasing it (use different terms or more specific symptoms), or \
contact your IT support team for help.
    Replace {user_query} with the user's text.

LANGUAGE RULES
Use direct, professional support language. Never use vague phrases such \
as: typically, usually, generally, probably, maybe, should work, seems \
like, hope this helps, something like, try to, you may want to. Use \
direct action language: Check, Confirm, Validate, Restart, Update, \
Run, Review, Verify, Escalate.

TECHNICAL PRESERVATION RULES
Preserve the following technical artifacts character-for-character \
exactly as written in the KB article: commands, paths, URLs, registry \
keys, configuration keys, error codes, error messages, port numbers, \
version strings, SQL queries, API endpoints, CLI flags, log messages, \
stack traces, JSON keys, XML tags, YAML keys, file names, service \
names. If the article contains commands, code, logs, JSON, XML, YAML, \
SQL, stack traces, or configuration blocks, place them inside fenced \
code blocks. Do not paraphrase technical artifacts.

STEP HANDLING RULES
If the KB article contains resolution steps:
1. Preserve the original step order. Do not reorder, merge, or omit.
2. Keep one action per step where possible.
3. If a step includes a command, place the command in a fenced code \
block directly below that step.
4. Preserve validation, verification, warnings, prerequisites, \
caveats, rollback steps, and escalation notes if they exist in the KB \
article.

MULTI-METHOD HANDLING
If a KB article documents multiple valid methods, render each method \
separately:

### Method 1: [Method Name]
1. [Step]
2. [Step]

### Method 2: [Method Name]
1. [Step]
2. [Step]

Do not collapse multiple methods into one. Do not choose one method \
unless the KB article explicitly states which method applies.

SECURITY-SENSITIVE HANDLING
If a KB article includes a security-sensitive action — MFA reset, \
password reset, token reset, credential rotation, firewall change, \
production restart, account unlock, permission change, access \
modification, backup restore, data deletion, certificate change, \
identity provider change — preserve all approval requirements, \
warnings, audit steps, validation steps, and escalation notes exactly. \
Do not add bypasses. Do not simplify approval language. Do not provide \
undocumented shortcuts.

LENGTH HANDLING
Summarize narrative prose when content is long. Preserve all technical \
artifacts verbatim regardless of length. For very long logs / dumps: \
summarize narrative, preserve unique errors / timestamps / commands / \
config values / endpoint values / version numbers / IDs. Remove exact \
duplicate repeated log lines only when they add no new diagnostic \
value. Do not truncate technical artifacts mid-line. Do not cut \
commands, URLs, paths, error codes, or configuration values.

CONTENT IS AUTHORITATIVE
The article block you receive has three fields per article: Title, \
Summary, Content. Summary is METADATA (a one-liner index hint). Content \
is the AUTHORITATIVE body of the article and is the source you render \
from. NEVER use the Summary field as a substitute for Content — that \
collapses a structured article into a single paraphrased sentence. \
When Content is present, render Content; reference Summary only as the \
opening orientation if helpful.

SECTIONS — render every section the Content contains
KB articles commonly carry these structured sections inside the \
Content field: Symptom, Cause, Resolution, Workaround, Verification, \
Prerequisites, Notes, Escalation, Rollback. **Render every section that \
appears in the Content verbatim under its original heading.** Do not \
collapse Symptom + Cause + Resolution into a single paragraph. Do not \
drop sections because they feel "redundant" with the Summary. Each \
section answers a different operational question (what is happening, \
why, how to fix, how to verify) and the user needs all of them.

SINGLE KB RESPONSE FORMAT
[Short opening paragraph (1-2 sentences) orienting the user — what the \
article addresses and when it applies. Pull from Content's Symptom \
section if present; otherwise the article's Summary may serve as a \
fallback orientation.]

**[KB Title] ([KB ID])**

Symptom:
[Include the Symptom verbatim if the Content has a "Symptom:" section.]

Cause:
[Include the Cause verbatim if the Content has a "Cause:" section.]

Resolution:
1. [Step 1 from Content's Resolution section]
2. [Step 2]
3. [Step 3]
(continue for every numbered step in the Content; do not stop early)

Verification:
[Include only if the Content has verification or expected-result details.]

Notes:
[Include only if the Content has warnings, caveats, prerequisites, \
rollback guidance, security notes, or escalation notes.]

Source: [KB ID]

Sections that don't appear in the Content are OMITTED — never inserted \
as empty / "Not specified". The format above is a maximum template, not \
a minimum: include the sections that exist, drop the rest.

MULTIPLE KB RESPONSE FORMAT
[Short opening sentence (1-2 sentences) tying the articles to the \
user's question. Mention which article applies when so the user knows \
which one to start with. Do NOT write meta-statements like "I found N \
articles" — go straight to useful content.]

---

## [KB Title] ([KB ID])

[Short summary of what this KB addresses.]

Resolution:
1. [Step 1]
2. [Step 2]

Verification:
[Include only if present.]

Notes:
[Include only if present.]

---

## [KB Title] ([KB ID])

[Short summary of what this KB addresses.]

Resolution:
1. [Step 1]
2. [Step 2]

Verification:
[Include only if present.]

Notes:
[Include only if present.]

Source: [KB ID 1], [KB ID 2]

MULTIPLE KB SEPARATION
1. Separate every KB with `---` on its own line.
2. Keep each KB self-contained — do not mix symptoms / causes / steps / \
verification / notes / sources across articles.
3. Put the most relevant KB first.
4. Do not create one combined resolution unless the KB articles \
explicitly belong to the same procedure.
5. Single trailing `Source:` line at the very end of the whole reply, \
comma-separated, listing every cited KB id.

FIELD USAGE
Schema fields available: title, summary, content, category, tags, \
audience, helpful_votes, views, related_ci_ids, related_incidents, \
created_by, created_at, updated_at. Use title, summary, content as the \
primary response source. ALWAYS render a one-line `Category: <category> \
· Tags: <tag1>, <tag2>, …` row directly under each article's title \
when category or tags are present — they give the operator scope at a \
glance ("ok, this is a network/wifi article" — useful even when the \
body is short). Do NOT show helpful_votes, views, related_ci_ids, \
related_incidents, created_by, created_at, or updated_at unless the \
user specifically asks for metadata. Do not infer missing fields such \
as cause, prerequisites, rollback, or verification unless they are \
explicitly present in the KB content.

CAUSE HANDLING
Only include a Cause section if the KB article explicitly states a \
cause, root cause, RCA, reason, diagnosis, or finding. Do not infer \
cause from symptoms. Do not label a recommendation as a cause. If no \
cause is present, omit the Cause section entirely.

FINAL RESPONSE RULES
1. Keep the output chat-friendly.
2. Prefer short paragraphs and ordered steps.
3. Use `---` separators between multiple KBs.
4. Do not render empty sections.
5. Do not write "Not specified."
6. Do not over-format the answer.
7. Do not convert chat responses into formal documentation.
8. Preserve all technical details exactly.
9. End with a single Source line listing every cited KB id, \
comma-separated, after all article blocks. No source line at all in \
CASE B.
"""


# A pluggable callable so tests can inject a deterministic stub.
ComposeFn = Callable[..., Awaitable[str]]


class AnswerComposer(Protocol):
    async def compose(
        self, *, query: str, articles: list[dict[str, Any]],
        tenant_id: str, user_id: str = "", request_id: str = "",
    ) -> str: ...


class DeterministicComposer:
    """No-LLM fallback — the deterministic CASE A / CASE B shape.

    Used when the gateway is unavailable. The reply is conservative: it
    lists the article ids the search found without making up content,
    and gracefully says "no match" when the list is empty."""

    async def compose(
        self, *, query: str, articles: list[dict[str, Any]],
        tenant_id: str, user_id: str = "", request_id: str = "",
    ) -> str:
        if not articles:
            return ("I couldn't find a knowledge-base article matching "
                    "that query. Try rephrasing it with different terms, "
                    "or contact your IT support team for help.")
        ids = ", ".join(a.get("kb_id", "") for a in articles
                        if a.get("kb_id"))
        first = articles[0]
        title = first.get("title", "") or ""
        summary = first.get("summary", "") or ""
        head = f'"{title}"' if title else ids
        if summary:
            return (f"{head}: {summary} (Source: {first.get('kb_id','')})")
        return (f"Found {len(articles)} matching article(s): {ids}.")


class LlmAnswerComposer:
    """Production composer — one gateway call, policy-wrapped, OTel-spanned.

    The output is a single string (the user-facing reply). Failure of the
    LLM call (timeout, parse error, gateway exhausted) falls back to the
    DeterministicComposer so the user always gets a coherent reply."""

    def __init__(self, gateway: Any, *, model: str = "gpt-4o-mini") -> None:
        self._gateway = gateway
        self._model = model
        self._fallback = DeterministicComposer()

    async def compose(
        self, *, query: str, articles: list[dict[str, Any]],
        tenant_id: str, user_id: str = "", request_id: str = "",
    ) -> str:
        from oneops.errors import LLMGatewayError
        from oneops.llm import LlmMessage, LlmRequest
        from oneops.policy import Profile, compose
        if not tenant_id:
            raise ValueError("answer_composer.compose requires tenant_id")

        # Build the article block the LLM reads. Strict bound on per-article
        # text so a pathologically long body cannot blow the attention
        # budget (Moveworks attention-budget discipline). The composer
        # gets only TITLE + SUMMARY + CONTENT (truncated). Full body is
        # available via get_kb_article for follow-ups.
        article_block = _format_articles_for_prompt(articles)
        user_block = (
            f"--- USER QUERY ---\n{query.strip()}\n\n"
            f"--- KNOWLEDGE BASE RESULTS ---\n{article_block}"
        )
        system_prompt = compose(
            Profile.INTERNAL_AGENT, extra_sections=[_ANSWER_PROMPT])

        with _tracer.start_as_current_span(
            "uc03.answer_composer.compose",
            attributes={
                "oneops.tenant_id": tenant_id,
                "oneops.user_id": user_id,
                "oneops.kb.article_count": len(articles),
                "oneops.kb.case": "B" if not articles else "A",
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
                    # temperature=0 for deterministic, consistent output
                    # across repeats of the same query (cache-friendly +
                    # demo-friendly). max_tokens raised so a 5-article
                    # response with per-block detail never truncates.
                    temperature=0.0,
                    max_tokens=1200,
                ))
                text = (resp.content or "").strip()
                if not text:
                    raise LLMGatewayError(
                        "answer_composer: empty content from gateway")
                # Defense-in-depth — if the model emits an obviously
                # invented `KBxxxxxxx` not in the provided list, fall
                # back. We are paranoid about citation hallucination.
                if _has_uncited_kb_id(text, articles):
                    span.set_attribute("oneops.kb.citation_leak", True)
                    _log.warning(
                        "uc03.answer_composer.uncited_kb_id_detected",
                        provided=[a.get("kb_id") for a in articles])
                    return await self._fallback.compose(
                        query=query, articles=articles,
                        tenant_id=tenant_id, user_id=user_id,
                        request_id=request_id)
                return text
            except LLMGatewayError as exc:
                span.set_attribute("error", True)
                _log.warning("uc03.answer_composer.gateway_failed",
                             error=str(exc)[:200])
                return await self._fallback.compose(
                    query=query, articles=articles,
                    tenant_id=tenant_id, user_id=user_id,
                    request_id=request_id)


def _format_article(i: int, a: dict[str, Any]) -> str:
    """Render one retrieved article as a faithful prompt block. Content is
    bounded at 1200 chars so the total prompt stays under the 600-token budget
    even with 5 results."""
    kb_id = a.get("kb_id", "") or "(unknown)"
    title = (a.get("title", "") or "").strip()
    summary = (a.get("summary", "") or "").strip()
    content = (a.get("content", "") or "").strip()
    score = a.get("relevance_score")
    if content and len(content) > 1200:
        content = content[:1200] + "…"
    head = f"Article {i} — id={kb_id}"
    if score is not None:
        head += f"  (relevance {score})"
    if title:
        head += f"\nTitle: {title}"
    if summary:
        head += f"\nSummary: {summary}"
    if content:
        head += f"\nContent:\n{content}"
    return head


def _format_articles_for_prompt(articles: list[dict[str, Any]]) -> str:
    """Render the retrieved articles as a stable, faithful prompt block."""
    if not articles:
        return "(none — no matching article was returned by the search)"
    return "\n\n".join(_format_article(i, a) for i, a in enumerate(articles, 1))


def _has_uncited_kb_id(text: str, articles: list[dict[str, Any]]) -> bool:
    """Detect a KB id that the LLM inserted but was not in the input.

    Citation hallucinations are the #1 way grounded-answer composers
    leak: the model paraphrases an article and signs it with a made-up
    `KB0005999` to look authoritative. We extract every `KB\\d+`
    occurrence in the reply and check it appears in the provided
    `articles[*].kb_id` set. Any leak → fall back to deterministic."""
    import re
    cited = set(re.findall(r"\bKB\d{4,}\b", text))
    if not cited:
        return False
    provided = {(a.get("kb_id") or "") for a in articles}
    leaked = cited - provided
    return bool(leaked)


# Process-wide injection seam — set by app.py at startup; tests override.
_composer: AnswerComposer | None = None


def set_kb_answer_composer(impl: AnswerComposer | None) -> None:
    global _composer
    _composer = impl


def get_kb_answer_composer() -> AnswerComposer | None:
    return _composer


__all__ = [
    "AnswerComposer",
    "DeterministicComposer",
    "LlmAnswerComposer",
    "set_kb_answer_composer",
    "get_kb_answer_composer",
]
