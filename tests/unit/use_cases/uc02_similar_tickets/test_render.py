"""UC-2 chat renderer — locks the spec output shape."""
from __future__ import annotations

from oneops.use_cases.uc02_similar_tickets.contracts import (
    SimilarTicket,
    SimilarTicketsResponse,
)
from oneops.use_cases.uc02_similar_tickets.render import render


def _result(**kw) -> SimilarTicket:
    base = dict(
        ticket_id="INC0001002",
        service_id="incident",
        title="VPN drops after 10 minutes",
        status="in_progress",
        similarity_score=0.86,
        match_pct=86,
        confidence=0.84,
        why_similar=["same_ci", "same_category", "same_service", "same_group"],
        flag=None,
    )
    base.update(kw)
    return SimilarTicket(**base)


def _resp(results, **kw) -> SimilarTicketsResponse:
    base = dict(
        source_ticket_id="INC0001001",
        service_id="incident",
        tenant_id="T001",
        results=results,
    )
    base.update(kw)
    return SimilarTicketsResponse(**base)


def test_empty_results_uses_message():
    out = render(_resp([], message="no significantly similar tickets found"))
    assert "no significantly similar" in out.lower()


def test_empty_results_default_message_when_none():
    out = render(_resp([]))
    assert "no similar" in out.lower()


def test_empty_results_includes_warning_when_present():
    out = render(_resp([], warning="limited context — source has short body"))
    assert "limited context" in out.lower()


def test_single_result_grammar_singular():
    out = render(_resp([_result()]))
    assert "1 similar ticket:" in out
    assert "1 similar tickets" not in out


def test_multiple_results_plural():
    out = render(_resp([_result(), _result(ticket_id="INC0001003")]))
    assert "2 similar tickets" in out


def test_renders_match_pct_and_status():
    out = render(_resp([_result(match_pct=92, status="resolved")]))
    assert "92% match" in out
    assert "Resolved" in out


def test_renders_common_prose_from_why_similar():
    out = render(_resp([_result(why_similar=["same_ci", "resolved"])]))
    assert "same configuration item" in out
    assert "already resolved" in out


def test_renders_likely_duplicate_flag():
    out = render(_resp([_result(flag="likely_duplicate")]))
    assert "Likely Duplicate" in out


def test_renders_resolution_available_flag():
    out = render(_resp([_result(flag="resolution_available", status="resolved")]))
    assert "Resolution Available" in out


def test_show_at_most_truncates_but_keeps_total():
    results = [_result(ticket_id=f"INC000100{i}") for i in range(2, 7)]
    out = render(_resp(results), show_at_most=3)
    assert "Found 5 similar tickets (showing top 3)" in out
    assert "INC0001002" in out
    assert "INC0001006" not in out


def test_title_with_inline_quotes_stripped():
    out = render(_resp([_result(title='"VPN tunnel resets"')]))
    # Inner quotes from data should not double-wrap
    assert out.count('""') == 0


def test_unknown_signal_still_renders_human_readable():
    out = render(_resp([_result(why_similar=["weird_new_signal"])]))
    # Fallback: replace underscores with spaces, never raw token
    assert "weird new signal" in out


def test_results_with_warning_appends_note():
    out = render(_resp([_result()], warning="ticket has limited body"))
    assert "_Note:" in out
    assert "limited body" in out


def test_no_warning_no_note():
    out = render(_resp([_result()]))
    assert "_Note" not in out
