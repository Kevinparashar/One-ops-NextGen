"""Tool 2: recommend_assignment.

Takes the neighbour list already produced by Tool 1 (no second DB hit) and
returns the most-common `assignment_group` with full provenance — same
Bundle A shape as Tool 1's FieldSuggestion (confidence, coverage, diversity,
basis_ids, rationale).

Tie-breaks via the same pluggable cheap-LLM mechanism when:
  • coverage  >= 0.40  (at least some non-null groups in the corpus)
  • confidence < 0.60  (kNN vote is genuinely split)
  • diversity >= 2

PERSON-level assignment (assigned_to) is NOT handled here — requires
workload + skill + shift data the corpus doesn't carry. Industry pattern:
ML predicts group, separate balancing layer picks person. Phase-2 deferral.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from oneops.observability import span
from oneops.use_cases.uc05_triage.contracts import (
    AssignmentRecommendation,
    ScoredNeighbour,
)

CONFIDENCE_FLOOR = 0.50
"""Below this → return None instead of a low-confidence team guess."""

CONFIDENCE_FLOOR_FOR_LLM = 0.60
"""Below this AND coverage>=floor → invoke LLM tiebreak if provided."""

COVERAGE_MIN_FOR_LLM = 0.40
"""Below this → corpus too sparse, don't bother LLM."""

# Same signature as Tool 1's tiebreak — async (probe_text, field, candidates,
# ticket_row) -> str | None. Candidates carry vote_count + example_titles.
TiebreakFn = Callable[..., Awaitable[str | None]]


async def recommend_assignment(
    *,
    candidates: list[ScoredNeighbour],
    probe_text: str = "",
    ticket_row: dict[str, Any] | None = None,
    tiebreak_fn: TiebreakFn | None = None,
) -> AssignmentRecommendation:
    """Return AssignmentRecommendation from majority vote over top-K neighbours.

    `probe_text` and `ticket_row` are only used if the LLM tiebreak path
    fires. Optional — Tool 2 works without them for pure-kNN paths.
    """
    with span("uc05.tool.recommend_assignment",
              **{"uc05.candidates_count": len(candidates)}) as _sp:
        return await _recommend_assignment_impl(
            candidates=candidates, probe_text=probe_text,
            ticket_row=ticket_row, tiebreak_fn=tiebreak_fn, _sp=_sp,
        )


async def _recommend_assignment_impl(
    *, candidates, probe_text, ticket_row, tiebreak_fn, _sp,
) -> AssignmentRecommendation:
    total_k = len(candidates)

    if total_k == 0:
        return AssignmentRecommendation(
            assignment_group=None,
            confidence=0.0,
            coverage=0.0,
            diversity=0,
            basis_ids=[],
            basis="empty_neighbours",
            rationale="No similar tickets found — cannot recommend an assignment group.",
        )

    votes: list[tuple[str, str]] = []
    for c in candidates:
        v = c.fields.get("assignment_group")
        if isinstance(v, str) and v.strip():
            votes.append((v, c.id))

    coverage = len(votes) / total_k
    distinct = {v for v, _ in votes}
    diversity = len(distinct)

    if not votes:
        return AssignmentRecommendation(
            assignment_group=None,
            confidence=0.0,
            coverage=0.0,
            diversity=0,
            basis_ids=[],
            basis="below_coverage",
            rationale=(
                f"None of the {total_k} similar tickets had an assignment "
                f"group recorded — recommendation withheld."
            ),
        )

    counts = Counter(v for v, _ in votes)
    winner, winner_count = counts.most_common(1)[0]
    confidence = winner_count / len(votes)
    winner_ids = [nid for v, nid in votes if v == winner]

    # LLM tiebreak path
    if (
        tiebreak_fn is not None
        and confidence < CONFIDENCE_FLOOR_FOR_LLM
        and coverage >= COVERAGE_MIN_FOR_LLM
        and diversity >= 2
    ):
        candidate_records = _build_llm_candidates(votes=votes, candidates=candidates)
        all_values = [c["value"] for c in candidate_records]
        try:
            llm_choice = await tiebreak_fn(
                probe_text=probe_text,
                field="assignment_group",
                candidates=candidate_records,
                ticket_row=dict(ticket_row or {}),
            )
        except Exception:
            llm_choice = None
        if isinstance(llm_choice, str) and llm_choice.strip() in all_values:
            chosen = llm_choice.strip()
            chosen_ids = [nid for v, nid in votes if v == chosen]
            return AssignmentRecommendation(
                assignment_group=chosen,
                confidence=counts.get(chosen, 0) / len(votes),
                coverage=coverage,
                diversity=diversity,
                basis_ids=chosen_ids,
                basis="llm_tiebreak",
                rationale=(
                    f"kNN vote split ({_summary(counts)}); LLM read the "
                    f"ticket description and chose '{chosen}' as the best fit."
                ),
            )

    # Pure-kNN path
    if confidence < CONFIDENCE_FLOOR:
        return AssignmentRecommendation(
            assignment_group=None,
            confidence=confidence,
            coverage=coverage,
            diversity=diversity,
            basis_ids=[],
            basis="below_confidence_floor",
            rationale=(
                f"kNN vote split ({_summary(counts)}) — no clear majority. "
                f"Defer to human triage."
            ),
        )

    return AssignmentRecommendation(
        assignment_group=winner,
        confidence=confidence,
        coverage=coverage,
        diversity=diversity,
        basis_ids=winner_ids,
        basis="majority_of_top_k",
        rationale=f"{winner_count} of {total_k} similar tickets routed to '{winner}'.",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_llm_candidates(
    *,
    votes: list[tuple[str, str]],
    candidates: list[ScoredNeighbour],
    max_examples_per_value: int = 2,
) -> list[dict[str, Any]]:
    by_value: dict[str, list[str]] = {}
    counts = Counter(v for v, _ in votes)
    id_to_title: dict[str, str] = {
        c.id: str(c.fields.get("title") or "").strip() for c in candidates
    }
    for v, nid in votes:
        title = id_to_title.get(nid)
        if not title:
            continue
        by_value.setdefault(v, [])
        if len(by_value[v]) < max_examples_per_value and title not in by_value[v]:
            by_value[v].append(title)
    return [
        {"value": v, "vote_count": n, "example_titles": by_value.get(v, [])}
        for v, n in counts.most_common()
    ]


def _summary(counts: Counter[str]) -> str:
    return ", ".join(f"{v}={n}" for v, n in counts.most_common(3))
