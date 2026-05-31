"""Conversation-memory management — turn-boundary trim + summarize.

Closes substrate gap **G2**: without a bound on `conversation_history`,
long-running sessions accumulate every turn ever spoken in the executor's
per-invocation state. On a FaaS deployment (substrate gap target) every
function instance has a small memory ceiling — an unbounded history will
crash the executor or burn the attention budget (Component Spec C9 —
Moveworks attention budget).

The trim contract:

  * **deterministic gate** — token estimate against a budget; never a hidden
    drop. ([[feedback_no_turn_cap]])
  * **summarize, don't truncate** — the oldest-N turns the trimmer would drop
    are LLM-summarized into a single synthetic `{"role": "system",
    "content": "[Prior conversation summary] …"}` message that precedes the
    kept suffix. Information is *compressed*, not discarded.
  * **no LLM wired = no silent drop** — if a trim is required but no
    summarizer is wired, the trimmer raises `ConversationTrimError` (loud,
    typed). The executor surfaces it as a typed turn failure with the
    deny-reasons in the trace. A degraded behavior is never silent.
  * **passthrough below budget** — common case is zero-cost: no LLM call, no
    state mutation, return-as-is.
  * **tenant binding** — the summarizer callback receives the tenant_id from
    the executor envelope and threads it to the LLM gateway; the trimmer
    holds no tenant cache.

Design influences:
  * LangGraph's `trim_messages` shape and `RemoveMessage` semantics — bound
    by token budget, not by turn count.
  * Moveworks attention-budget — protect the model's window proactively.
  * AgentScript determinism dial — the trimmer is data + a callback, not a
    hand-rolled LLM call inside the executor body.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oneops.errors import OneOpsError
from oneops.observability import get_logger

_log = get_logger("oneops.executor.memory")


class ConversationTrimError(OneOpsError):
    """Raised when a trim is required but cannot be performed safely."""

    code = "CONVERSATION_TRIM_FAILED"


# A summariser is an async callback that compresses a prefix of messages into
# a single short summary string. The executor wires this to the LLM gateway
# (single egress); tests supply a deterministic stand-in.
ConversationSummariser = Callable[
    [list[dict[str, str]], str], Awaitable[str]
]  # (messages, tenant_id) -> summary text


@dataclass(frozen=True)
class TrimResult:
    """Output of a trim pass.

    `history` is the bounded conversation to thread into router state. When a
    summary was produced it is the **first** entry (`role="system"`); the
    `keep_suffix` count tells the operator how many original verbatim turns
    survived (useful for debugging and span attributes).
    """

    history: list[dict[str, str]]
    summary_emitted: bool
    estimated_tokens_before: int
    estimated_tokens_after: int
    keep_suffix: int


@runtime_checkable
class ConversationTrimmer(Protocol):
    """Bounds the conversation history for one turn."""

    async def trim(
        self, history: list[dict[str, str]], *, tenant_id: str,
    ) -> TrimResult: ...


# ── Estimation ───────────────────────────────────────────────────────────


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """A deliberately rough character-budget heuristic — ~4 chars/token.

    The point is to bound, not to be exact. A real tokenizer would couple this
    module to a specific provider; the gateway-level cost accounting already
    carries true token counts after the fact."""
    if not messages:
        return 0
    chars = 0
    for m in messages:
        chars += len(m.get("role", "")) + len(m.get("content", ""))
    return max(1, chars // 4)


# ── Implementations ─────────────────────────────────────────────────────


class NoopTrimmer:
    """Passthrough trimmer — preserves today's behavior. The executor uses it
    when no budget / no summariser is wired (default). FaaS deployments wire
    a real budget + summariser."""

    async def trim(
        self, history: list[dict[str, str]], *, tenant_id: str,
    ) -> TrimResult:
        n = estimate_tokens(history)
        return TrimResult(
            history=list(history), summary_emitted=False,
            estimated_tokens_before=n, estimated_tokens_after=n,
            keep_suffix=len(history),
        )


class TokenBudgetTrimmer:
    """Bound `history` to `max_tokens`. When over budget, summarize the
    oldest-prefix into one system message and keep the most-recent
    `keep_last_turns` turns verbatim.

    Failure modes:
      * over budget AND no summariser wired → `ConversationTrimError`. No
        silent drop. Operators surface this as a degraded turn.
      * `max_tokens` ≤ 0 → `ValueError` at construction (caller mistake).
      * `keep_last_turns` ≤ 0 → `ValueError` at construction.
    """

    def __init__(
        self,
        *,
        max_tokens: int,
        keep_last_turns: int,
        summariser: ConversationSummariser | None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError(f"TokenBudgetTrimmer.max_tokens must be > 0, got {max_tokens}")
        if keep_last_turns <= 0:
            raise ValueError(
                f"TokenBudgetTrimmer.keep_last_turns must be > 0, got {keep_last_turns}")
        self._max_tokens = max_tokens
        self._keep_last_turns = keep_last_turns
        self._summariser = summariser

    async def trim(
        self, history: list[dict[str, str]], *, tenant_id: str,
    ) -> TrimResult:
        before = estimate_tokens(history)
        if before <= self._max_tokens:
            return TrimResult(
                history=list(history), summary_emitted=False,
                estimated_tokens_before=before,
                estimated_tokens_after=before,
                keep_suffix=len(history),
            )

        # Over budget. Split: oldest prefix → summary; recent suffix → verbatim.
        if len(history) <= self._keep_last_turns:
            # Already at the minimum keep-window. Can't split further without
            # dropping context the contract asks us to preserve. Loud refuse.
            raise ConversationTrimError(
                f"history has {len(history)} turns but token budget "
                f"({self._max_tokens}) is already exceeded by the "
                f"minimum keep-window ({self._keep_last_turns}); raise "
                f"max_tokens or shrink keep_last_turns")

        if self._summariser is None:
            raise ConversationTrimError(
                "conversation exceeds token budget "
                f"({before} > {self._max_tokens}) but no summariser is wired; "
                "refusing to silently drop history (see [[feedback_no_turn_cap]])")

        prefix = history[: len(history) - self._keep_last_turns]
        suffix = history[len(history) - self._keep_last_turns :]
        if not tenant_id:
            raise ConversationTrimError(
                "trim requires tenant_id (single LLM egress is tenant-scoped)")
        summary_text = (await self._summariser(prefix, tenant_id)) or ""
        if not summary_text.strip():
            raise ConversationTrimError(
                "summariser returned an empty summary; refusing to drop "
                "prefix turns silently")
        summary_msg = {
            "role": "system",
            "content": f"[Prior conversation summary] {summary_text.strip()}",
        }
        trimmed = [summary_msg] + list(suffix)
        after = estimate_tokens(trimmed)
        _log.info(
            "executor.memory.trim",
            tenant_id=tenant_id,
            turns_before=len(history), turns_after=len(trimmed),
            tokens_before=before, tokens_after=after,
        )
        return TrimResult(
            history=trimmed, summary_emitted=True,
            estimated_tokens_before=before, estimated_tokens_after=after,
            keep_suffix=len(suffix),
        )


__all__ = [
    "ConversationTrimError",
    "ConversationSummariser",
    "ConversationTrimmer",
    "TrimResult",
    "NoopTrimmer",
    "TokenBudgetTrimmer",
    "estimate_tokens",
]
