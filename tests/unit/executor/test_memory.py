"""Conversation memory management — substrate gap G2.

Verifies the trim contract: passthrough when under budget, LLM-summarized
prefix + verbatim suffix when over, loud `ConversationTrimError` instead of
silent drop when no summariser is wired, attention-budget invariants
(no_turn_cap memory: never `messages[-N:]`).
"""
from __future__ import annotations

import pytest

from oneops.executor.memory import (
    ConversationTrimError,
    NoopTrimmer,
    TokenBudgetTrimmer,
    TrimResult,
    estimate_tokens,
)


def _msg(role: str, content: str) -> dict[str, str]:
    return {"role": role, "content": content}


def _conversation(turns: int, content_per_turn: str = "hello world " * 20) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for i in range(turns):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(_msg(role, content_per_turn + f" turn={i}"))
    return out


# ── estimate_tokens ──────────────────────────────────────────────────────


def test_estimate_tokens_empty_history_is_zero():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_scales_with_content():
    short = estimate_tokens([_msg("user", "hi")])
    longer = estimate_tokens([_msg("user", "hi" * 100)])
    assert longer > short


# ── NoopTrimmer ──────────────────────────────────────────────────────────


async def test_noop_passes_history_through_unchanged():
    history = _conversation(50)
    out = await NoopTrimmer().trim(history, tenant_id="t1")
    assert out.history == history
    assert out.summary_emitted is False
    assert out.keep_suffix == len(history)


# ── TokenBudgetTrimmer construction validation ───────────────────────────


def test_trimmer_rejects_non_positive_max_tokens():
    with pytest.raises(ValueError, match="max_tokens"):
        TokenBudgetTrimmer(max_tokens=0, keep_last_turns=4, summariser=None)


def test_trimmer_rejects_non_positive_keep_last_turns():
    with pytest.raises(ValueError, match="keep_last_turns"):
        TokenBudgetTrimmer(max_tokens=100, keep_last_turns=0, summariser=None)


# ── Below-budget passthrough — zero-cost common case ─────────────────────


async def test_below_budget_returns_history_unchanged_and_calls_no_llm():
    summariser_calls: list = []

    async def summariser(prefix, tenant_id):
        summariser_calls.append((prefix, tenant_id))
        return "should not be called"

    trimmer = TokenBudgetTrimmer(
        max_tokens=10_000, keep_last_turns=4, summariser=summariser)
    history = _conversation(5)
    out = await trimmer.trim(history, tenant_id="t1")
    assert out.history == history
    assert out.summary_emitted is False
    assert summariser_calls == []


# ── Above-budget — summarize prefix, keep suffix verbatim ────────────────


async def test_over_budget_summarizes_prefix_and_keeps_suffix(monkeypatch):
    captured_prefix: list = []

    async def summariser(prefix, tenant_id):
        captured_prefix.extend(prefix)
        assert tenant_id == "tenant-a"
        return "Earlier the user asked about VPN and the agent restarted the tunnel."

    trimmer = TokenBudgetTrimmer(
        max_tokens=200, keep_last_turns=4, summariser=summariser)
    history = _conversation(40)                      # well over 200 tokens
    out = await trimmer.trim(history, tenant_id="tenant-a")

    # Summary emitted; first message is the synthetic system summary.
    assert out.summary_emitted is True
    assert out.history[0]["role"] == "system"
    assert "Prior conversation summary" in out.history[0]["content"]
    assert "VPN" in out.history[0]["content"]

    # Verbatim suffix is exactly the LAST 4 turns of the original history.
    assert out.history[1:] == history[-4:]
    assert out.keep_suffix == 4

    # Summariser saw EXACTLY the prefix turns — no overlap with the suffix.
    assert captured_prefix == history[: len(history) - 4]


# ── No-silent-drop invariants ────────────────────────────────────────────


async def test_over_budget_without_summariser_raises_loud():
    trimmer = TokenBudgetTrimmer(
        max_tokens=50, keep_last_turns=2, summariser=None)
    history = _conversation(30)
    with pytest.raises(ConversationTrimError, match="no summariser"):
        await trimmer.trim(history, tenant_id="t1")


async def test_empty_summary_is_refused_loud():
    async def empty_summariser(prefix, tenant_id):
        return "   "                                  # whitespace only

    trimmer = TokenBudgetTrimmer(
        max_tokens=50, keep_last_turns=2, summariser=empty_summariser)
    history = _conversation(30)
    with pytest.raises(ConversationTrimError, match="empty summary"):
        await trimmer.trim(history, tenant_id="t1")


async def test_over_budget_at_minimum_keep_window_is_loud():
    # If the keep-window alone already blows the budget, we can't trim
    # further without dropping a kept turn — that is a configuration bug
    # that must be visible.
    trimmer = TokenBudgetTrimmer(
        max_tokens=5, keep_last_turns=10,
        summariser=lambda *_: "noop")               # type: ignore[arg-type]
    history = _conversation(10)
    with pytest.raises(ConversationTrimError, match="minimum keep-window"):
        await trimmer.trim(history, tenant_id="t1")


async def test_missing_tenant_id_is_refused_when_summarizing():
    async def summariser(prefix, tenant_id):
        return "summary"

    trimmer = TokenBudgetTrimmer(
        max_tokens=50, keep_last_turns=2, summariser=summariser)
    history = _conversation(30)
    with pytest.raises(ConversationTrimError, match="tenant_id"):
        await trimmer.trim(history, tenant_id="")


# ── Tenant binding — summariser ALWAYS receives the envelope tenant ──────


async def test_summariser_receives_the_envelope_tenant_id():
    seen: list = []

    async def summariser(prefix, tenant_id):
        seen.append(tenant_id)
        return "ok"

    trimmer = TokenBudgetTrimmer(
        max_tokens=50, keep_last_turns=2, summariser=summariser)
    await trimmer.trim(_conversation(20), tenant_id="tenant-zzz")
    assert seen == ["tenant-zzz"]


# ── TrimResult metadata is honest ────────────────────────────────────────


async def test_trim_result_reports_token_deltas_when_summarized():
    async def summariser(prefix, tenant_id):
        return "short"

    trimmer = TokenBudgetTrimmer(
        max_tokens=200, keep_last_turns=4, summariser=summariser)
    history = _conversation(40)
    out: TrimResult = await trimmer.trim(history, tenant_id="t1")
    assert out.estimated_tokens_after < out.estimated_tokens_before
    assert out.keep_suffix == 4
