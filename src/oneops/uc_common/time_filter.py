"""Shared TimeFilter — structured time-window scope for ITSM queries.

Cross-UC contract emitted by `TimeFilterExtractor` (the LLM is the parser per
rule §2.1 — no `dateparser` / `parsedatetime`) and consumed by any retriever
that gates on `created_at` / `updated_at` / `resolved_at`. UC-2 is the first
consumer; UC-3 KB freshness and UC-5 per-call windows are next-natural fits.

Design notes
------------
  • `relative_days` is mutually exclusive with explicit `start_date`/`end_date` —
    the LLM picks one based on the user's phrasing; the validator enforces it.
  • All-None + label=None means "user did not request a window". The orchestrator
    must NOT silently default to "last 30 days" — that masks the corpus and
    breaks the "AI is hiding my answer" intuition.
  • `label` echoes the user's literal phrasing for confirm-back ("Found 2 from
    {label}, …"). Required when ANY filter is set so chat replies can echo.
  • `boundary` defaults to `created_at` (the ticket-open semantics most users
    mean by "since"). UC-3 will likely target `updated_at`; UC-5 might target
    `resolved_at` for resolution-reuse windows.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Boundary = Literal["created_at", "updated_at", "resolved_at"]
"""Which timestamp column the filter applies to."""


class TimeFilter(BaseModel):
    """Structured time window extracted from a user query.

    Either `relative_days` OR (`start_date` and/or `end_date`) is set, never
    both. If all primary fields and `label` are None, no time filter was
    requested — the orchestrator falls through with no temporal predicate.
    """

    model_config = ConfigDict(extra="forbid")

    relative_days: int | None = Field(default=None, ge=1, le=3650)
    """Rolling window: WHERE <boundary> >= NOW() - INTERVAL '<N> days'."""

    start_date: date | None = None
    """Lower bound (inclusive). Resolved by the LLM from absolute phrasing."""

    end_date: date | None = None
    """Upper bound (inclusive, applied as +1 day at SQL site)."""

    label: str | None = Field(default=None, max_length=80)
    """Literal phrase the user used. Echoed in replies for confirm-back."""

    boundary: Boundary = "created_at"
    """Column the predicate targets. Default `created_at` for ticket queries."""

    # ── Cross-field validation ───────────────────────────────────────────

    @model_validator(mode="after")
    def _mutual_exclusion(self) -> TimeFilter:
        """`relative_days` is exclusive with explicit dates."""
        if self.relative_days is not None and (
            self.start_date is not None or self.end_date is not None
        ):
            raise ValueError(
                "TimeFilter: relative_days is mutually exclusive with "
                "start_date/end_date — choose one")
        return self

    @model_validator(mode="after")
    def _ordered_dates(self) -> TimeFilter:
        """A backwards range is a refusal-worthy bug — surface it loudly."""
        if (self.start_date is not None and self.end_date is not None
                and self.start_date > self.end_date):
            raise ValueError(
                f"TimeFilter: start_date ({self.start_date}) is after "
                f"end_date ({self.end_date})")
        return self

    @model_validator(mode="after")
    def _future_anchor_inferred_past(self) -> TimeFilter:
        """Edge case: 'since November' in January → user means LAST November,
        not 11 months in the future. If the resolved start_date is more than
        7 days in the future from today, subtract a year. The orchestrator
        emits a `time_filter.year_inferred_past` span event so the operator
        can audit how often this fires.

        The 7-day grace is for clock-skew + "ending Friday" (today is Thursday)
        cases that are genuinely future and intentional.
        """
        today = date.today()
        if self.start_date is not None and (self.start_date - today).days > 7:
            self.start_date = self.start_date.replace(
                year=self.start_date.year - 1)
        if self.end_date is not None and (self.end_date - today).days > 7:
            self.end_date = self.end_date.replace(
                year=self.end_date.year - 1)
        # Re-check ordering after rewriting.
        if (self.start_date is not None and self.end_date is not None
                and self.start_date > self.end_date):
            raise ValueError(
                "TimeFilter: after year-inference, start_date "
                f"({self.start_date}) is after end_date ({self.end_date})")
        return self

    # ── Boundary methods (used by the retrievers) ───────────────────────

    def is_empty(self) -> bool:
        """True ⇔ no temporal predicate should be applied."""
        return (
            self.relative_days is None
            and self.start_date is None
            and self.end_date is None
        )

    def has_relative(self) -> bool:
        return self.relative_days is not None

    def end_date_inclusive(self) -> date | None:
        """Spec rule: end_date is day-inclusive — apply as `< end_date + 1`."""
        if self.end_date is None:
            return None
        return self.end_date + timedelta(days=1)

    def otel_attrs(self, *, prefix: str = "time_filter") -> dict[str, object]:
        """Stable OTel attribute dict — keys are spec-defined."""
        return {
            f"{prefix}.relative_days": self.relative_days,
            f"{prefix}.start_date":
                self.start_date.isoformat() if self.start_date else None,
            f"{prefix}.end_date":
                self.end_date.isoformat() if self.end_date else None,
            f"{prefix}.label": self.label,
            f"{prefix}.boundary": self.boundary,
        }


__all__ = ["TimeFilter", "Boundary"]
