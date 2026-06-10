"""Markdown renderer for the chat output.

The chat composer LLM uses the structured `SimilarTicketsResponse` to write
a chat reply. We pre-render the spec-exact format alongside the structured
fields so:

  • The LLM has a "ready-to-quote" version it can pass through verbatim when
    the user just asked for a list, and
  • The structured fields stay available for richer follow-ups
    ("filter to resolved", "show me the second one in detail").

Output shape mirrors docs/product/ai-service-use-cases.md §UC-2 "Output Format":

    Found 5 similar tickets (showing top 3):

    1. **INC004512** (92% match) — Resolved
       "Wi-Fi connectivity issues in Building-3 Floor 4"
       Common: same CI, same category, same symptoms
       Likely Duplicate — same CI, open

Edge cases handled here:
  • Empty results → human message from contracts (no fake list).
  • limited-context warning preserved as a callout.
  • flag (likely_duplicate / resolution_available) becomes a one-line callout.
  • why_similar list → "Common: ..." prose mapping.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from oneops.use_cases.uc02_similar_tickets.contracts import (
    SimilarTicket,
    SimilarTicketsResponse,
)

# Map machine signal names to operator-readable prose. Keep these tight —
# they're shown in the chat reply, not a debug log.
_PROSE: dict[str, str] = {
    "same_ci": "same configuration item",
    "same_category": "same category",
    "same_service": "same service",
    "same_group": "same assignment group",
    "resolved": "already resolved",
    "diagnosis_match": "diagnosis matches",
}

_FLAG_TEXT: dict[str, str] = {
    "likely_duplicate":   "⚠️ Likely Duplicate — same CI, currently open",
    "resolution_available": "✅ Resolution Available — this one was resolved",
}


def _prose_join(signals: Iterable[str]) -> str:
    """Turn ['same_ci', 'same_category'] into 'same configuration item, same category'."""
    parts = [_PROSE.get(s, s.replace("_", " ")) for s in signals]
    # Dedupe preserving order — defensive: the core may include duplicates if
    # both anchor + diagnosis signals fire.
    seen: set[str] = set()
    uniq: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return ", ".join(uniq) if uniq else "semantic match"


def _fmt_date(dt) -> str:
    if dt is None:
        return ""
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:                                                  # noqa: BLE001
        return str(dt)[:10]


def _meta_line(r: SimilarTicket) -> str:
    """One-line metadata summary: priority · category · CI · service · group."""
    parts: list[str] = []
    if r.priority:
        parts.append(f"Priority {r.priority}")
    if r.category:
        cat = r.category if not r.subcategory else f"{r.category} / {r.subcategory}"
        parts.append(f"Category {cat}")
    if r.service_name:
        parts.append(f"Service {r.service_name}")
    if r.ci_id:
        parts.append(f"CI {r.ci_id}")
    if r.assignment_group:
        parts.append(f"Group {r.assignment_group}")
    if r.assigned_to:
        parts.append(f"Assigned {r.assigned_to}")
    return " · ".join(parts)


def _dates_line(r: SimilarTicket) -> str:
    opened = _fmt_date(r.opened_at)
    resolved = _fmt_date(r.resolved_at)
    if opened and resolved:
        return f"Opened {opened} · Resolved {resolved}"
    if opened:
        return f"Opened {opened}"
    return ""


def _line_for(rank: int, r: SimilarTicket) -> str:
    status_word = (r.status or "open").replace("_", " ").title()
    title = (r.title or "(no title)").strip().strip('"')
    common = _prose_join(r.why_similar)
    meta = _meta_line(r)
    dates = _dates_line(r)
    out = [
        f"{rank}. **{r.ticket_id}** ({r.match_pct}% match) — {status_word}",
        f'   "{title}"',
    ]
    if r.discriminator:
        out.append(f"   _{r.discriminator}_")
    if meta:
        out.append(f"   {meta}")
    if dates:
        out.append(f"   {dates}")
    out.append(f"   Common: {common}")
    if r.flag and r.flag in _FLAG_TEXT:
        out.append(f"   {_FLAG_TEXT[r.flag]}")
    return "\n".join(out)


def render(
    resp: SimilarTicketsResponse,
    *,
    show_at_most: int | None = None,
    time_filter_label: str | None = None,
) -> str:
    """Return the spec-format chat text. Caller passes the structured response.

    `show_at_most` lets the chat reply trim to top-K for readability when
    `resp.results` has more (the spec example shows 5 found, top 3 shown).
    Defaults to all returned results — the API already capped at max_results.

    `time_filter_label`, when supplied, is echoed in the header per spec
    UC-2.6: "Found N incidents similar to INC0001020 from {label}, …".
    """
    if not resp.results:
        return _render_empty(resp)

    rows = resp.results
    if show_at_most is not None and show_at_most > 0:
        rows = rows[:show_at_most]

    lines: list[str] = _source_echo_lines(resp.source_ticket)
    lines.append(_build_header(resp, rows, time_filter_label, show_at_most))
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines.append(_line_for(i, r))
        lines.append("")  # blank line between entries

    if resp.warning:
        lines.append(f"_Note: {resp.warning}._")

    return "\n".join(lines).rstrip()


def _render_empty(resp: SimilarTicketsResponse) -> str:
    """Empty-results path — explain why from the two sources of truth
    (`resp.message`, `resp.warning`) so the user sees actionable next steps."""
    parts: list[str] = []
    if resp.message:
        parts.append(resp.message.capitalize() + ".")
    else:
        parts.append("No similar tickets found.")
    if resp.warning:
        parts.append(f"_Note: {resp.warning}._")
    return "\n\n".join(parts)


def _build_header(
    resp: SimilarTicketsResponse, rows: list, time_filter_label: str | None,
    show_at_most: int | None,
) -> str:
    """The "Found N similar ticket(s) [from <label>] [(showing top K)]:" line."""
    header = (
        f"Found {len(resp.results)} similar ticket"
        f"{'s' if len(resp.results) != 1 else ''}"
    )
    if time_filter_label:
        header += f" from {time_filter_label}"
    if show_at_most is not None and show_at_most < len(resp.results):
        header += f" (showing top {len(rows)})"
    return header + ":"


def _source_echo_lines(src: Any) -> list[str]:
    """Source-ticket echo block — lets the operator verify the match context at
    a glance (id, status, title, meta) before scanning the similar list.
    Returns the lines (with a trailing blank) or [] when there's no source."""
    if src is None or not src.title:
        return []
    head = f"_Your ticket:_ **{src.ticket_id}**"
    src_status = (src.status or "").replace("_", " ").title()
    if src_status:
        head += f" — {src_status}"
    lines = [head, f'   "{(src.title or "").strip().strip(chr(34))}"']
    src_meta = _meta_line(src)
    if src_meta:
        lines.append(f"   {src_meta}")
    lines.append("")
    return lines


__all__ = ["render"]
