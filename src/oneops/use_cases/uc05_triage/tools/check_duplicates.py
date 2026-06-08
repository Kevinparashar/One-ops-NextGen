"""Tool 1: check_duplicate_candidates (Bundle A + LLM tiebreaker).

Path B design (locked 2026-05-29): one vector search produces both the
duplicate verdict AND the field suggestions — same neighbours, no
second DB hit.

Bundle A (added 2026-05-29 evening):
  * Top-K = 5 (was 10) — tighter neighbour band, sharper signals
  * Per-field FieldSuggestion with confidence + coverage + diversity +
    basis_ids + rationale (production-grade explainability)
  * LLM tiebreaker for low-confidence cases — only fires when:
      0.4 <= coverage  (corpus has at least some signal)
      confidence < 0.60 (kNN vote is genuinely split)
    Cap: at most one LLM call per request; only on contested fields.
    Cheap model (gpt-4o-mini via gateway). Pluggable via tiebreak_fn.

Score thresholds (calibrated against the 290-row live corpus):
  DEFAULT_DUPLICATE_THRESHOLD = 0.85  # real dups 0.87-0.89, related 0.79
  DEFAULT_TOP_K               = 5
  CONFIDENCE_FLOOR_FOR_LLM    = 0.60  # below → LLM tiebreak
  COVERAGE_MIN_FOR_LLM        = 0.40  # below → don't even try LLM
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any

from oneops.embeddings.triage_input import build_embedding_input
from oneops.observability import span
from oneops.use_cases.uc05_triage.contracts import (
    DuplicateCheckResult,
    FieldSuggestion,
    ScoredNeighbour,
)
from oneops.use_cases.uc05_triage.retrieval.schema_loader import (
    load_retrieval_schema,
)
from oneops.use_cases.uc05_triage.retrieval.similarity_search import (
    EmbedFn,
    _Connection,
    search_similar,
)

DEFAULT_TOP_K = 5
CONFIDENCE_FLOOR_FOR_LLM = 0.60
COVERAGE_MIN_FOR_LLM = 0.40

# Tag keyword extraction — per ai-service usecase.md Step 5 enrichment
MAX_TAGS = 3
TAG_MIN_CHARS = 3
TAG_MAX_CHARS = 30
PROBE_TITLE_BOOST = 2
"""Tokens that appear in the probe's title get their count multiplied by this
boost — the new ticket's words should dominate over neighbour-only words."""

# English stoplist — frozen build-time snapshot of NLTK stopwords (198 words).
# Runtime has zero dependency on the nltk package; refresh by running
# `python dev/freeze_stopwords.py`. See stopwords_en.py for provenance.
# Tokenize: split on whitespace and most punctuation BUT keep hyphens
# inside words (so "Wi-Fi" stays one token).
import re as _re

from oneops.use_cases.uc05_triage.tools.stopwords_en import STOPWORDS_EN as _STOPWORDS

_TOKEN_RE = _re.compile(r"[A-Za-z][A-Za-z0-9\-_]*")

# Optional pluggable LLM tiebreaker. Signature:
#   async tiebreak(
#       probe_text: str,
#       field: str,
#       candidates: list[dict],     # [{value, vote_count, example_titles}]
#       ticket_row: dict,
#   ) -> str | None
TiebreakFn = Callable[..., Awaitable[str | None]]

# Optional pluggable LLM tagger (Step 3 enrichment, Fix E). Signature:
#   async tag(
#       probe_title: str,
#       probe_description: str,
#       neighbour_titles: list[str],
#       neighbour_descriptions: list[str],
#   ) -> list[str]
# LLM should return short domain-meaningful tags. The caller defensively
# normalises (lowercases, dedupes, caps at MAX_TAGS) so the LLM doesn't have
# to be perfectly behaved.
TagFn = Callable[..., Awaitable[list[str]]]

# Optional pluggable LLM proposer (2026-05-31 trust-fix). Signature:
#   async propose(
#       probe_text: str,
#       field: str,
#       ticket_row: dict,
#       pool: list[str],          # historical pool of values for this field
#   ) -> dict | None
# Returns `{"value": str, "confidence": float, "rationale": str}` or None.
# Fires only when kNN voting yields empty/below-floor majority on selected
# fields (`category`, `subcategory`, `assignment_group`). Lets the LLM
# *propose* a value rather than only break ties between existing ones.
ProposeFn = Callable[..., Awaitable[dict[str, Any] | None]]

# Fields where LLM proposal is allowed when kNN can't decide. Excludes
# `assigned_to` and `ci_id` — those need ground-truth references.
PROPOSE_ALLOWED_FIELDS = frozenset({"category", "subcategory",
                                    "assignment_group"})


async def check_duplicate_candidates(
    *,
    service_id: str,
    tenant_id: str,
    ticket_row: Mapping[str, Any],
    embed_fn: EmbedFn,
    conn: _Connection,
    duplicate_threshold: float = 0.85,
    max_candidates: int = DEFAULT_TOP_K,
    tiebreak_fn: TiebreakFn | None = None,
    tag_fn: TagFn | None = None,
    propose_fn: ProposeFn | None = None,
    now: datetime | None = None,
) -> DuplicateCheckResult:
    """Return DuplicateCheckResult with verdict + rich per-field suggestions.

    LLM tiebreak fires only when corpus signal is split (confidence < 0.6)
    AND coverage clears the floor (>= 0.4). Skipped when tiebreak_fn=None.
    """
    with span("uc05.tool.check_duplicates",
              **{"oneops.tenant_id": tenant_id, "uc05.service_id": service_id,
                 "uc05.ticket_id": str(ticket_row.get(f"{service_id}_id") or
                                       ticket_row.get("id") or "")}) as _sp:
        return await _check_duplicate_candidates_impl(
            service_id=service_id, tenant_id=tenant_id, ticket_row=ticket_row,
            embed_fn=embed_fn, conn=conn,
            duplicate_threshold=duplicate_threshold,
            max_candidates=max_candidates,
            tiebreak_fn=tiebreak_fn, tag_fn=tag_fn,
            propose_fn=propose_fn, now=now, _sp=_sp,
        )


async def _check_duplicate_candidates_impl(
    *, service_id, tenant_id, ticket_row, embed_fn, conn,
    duplicate_threshold, max_candidates, tiebreak_fn, tag_fn,
    propose_fn, now, _sp,
):
    schema = load_retrieval_schema(service_id)
    probe_text = build_embedding_input(ticket_row, service_id)

    candidates, top_match = await search_similar(
        conn,
        service_id=service_id,
        tenant_id=tenant_id,
        probe_text=probe_text,
        embed_fn=embed_fn,
        duplicate_threshold=duplicate_threshold,
        max_candidates=max_candidates,
        probe_ci_id=_safe_str(ticket_row.get("ci_id")),
        probe_service_name=_safe_str(ticket_row.get("service_name")),
        now=now,
    )

    # Filter the probe ticket out of its own results (defence in depth)
    self_id = _safe_str(
        ticket_row.get(schema["id_column"]) or ticket_row.get("id")
    )
    if self_id:
        candidates = [c for c in candidates if c.id != self_id]
        if top_match and top_match.id == self_id:
            top_match = None

    verdict = "duplicate" if top_match is not None else "none"

    # Build per-field FieldSuggestion for every aggregation target
    field_suggestions = await _build_field_suggestions(
        candidates=candidates,
        schema=schema,
        probe_text=probe_text,
        ticket_row=ticket_row,
        tiebreak_fn=tiebreak_fn,
        propose_fn=propose_fn,
    )

    # Shortcut strings — aligned to UC-5 spec aggregation_targets
    s_cat = _value_of(field_suggestions, "category")
    s_sub = _value_of(field_suggestions, "subcategory")
    s_assigned = _value_of(field_suggestions, "assigned_to")
    s_ci = _value_of(field_suggestions, "ci_id")

    # Step 5 enrichment — tag keywords (Fix A+B+E, 2026-05-29)
    tags = await _extract_tags(
        probe_title=str(ticket_row.get("title") or ""),
        probe_description=str(ticket_row.get("description") or ""),
        candidates=candidates,
        tag_fn=tag_fn,
    )

    result = DuplicateCheckResult(
        candidates=candidates,
        top_match=top_match,
        duplicate_verdict=verdict,
        duplicate_threshold=duplicate_threshold,
        suggested_category=s_cat,
        suggested_subcategory=s_sub,
        suggested_assigned_to=s_assigned,
        suggested_ci_id=s_ci,
        field_suggestions=field_suggestions,
        suggested_tags=tags,
    )
    if _sp is not None:
        _sp.set_attribute("uc05.duplicate_verdict", verdict)
        _sp.set_attribute("uc05.candidates_count", len(candidates))
        _sp.set_attribute("uc05.tags_count", len(tags))
    return result


async def _extract_tags(
    *,
    probe_title: str,
    probe_description: str,
    candidates: list[ScoredNeighbour],
    tag_fn: TagFn | None = None,
) -> list[str]:
    """Extract up to MAX_TAGS distinct keyword tags.

    Pipeline (locked 2026-05-29 PM after industry-survey decision):

      Step 1: Algorithmic preprocessing — free, always runs.
        Tokenise probe + neighbours, drop stopwords + digits + length-fails.
        This produces a CLEAN candidate pool we hand to the LLM. The LLM
        gets less noise to reason over.

      Step 2: LLM tag selection — primary path when tag_fn provided.
        Caller's prompt is ITSM/ITOM-grounded (lives in the runtime layer,
        not in the tool — so it can be tenant-specific). LLM reads:
          - probe title + description
          - neighbour titles + descriptions (truncated)
          - the pre-filtered candidate words (LLM uses them as a hint)
        Returns 1-3 short domain tags.

      Step 3: Algorithmic fallback.
        If tag_fn is None, or the LLM returns nothing usable, or the LLM
        raises, return the top-MAX_TAGS algorithmic candidates by
        frequency. Never returns silent empty when signal exists.

    Cost discipline lives at the CALLER layer: pass tag_fn for interactive
    triage, omit it for bulk jobs. Industry consensus (Aisera, ServiceNow
    Now Assist, hybrid LLM+ML research): LLM is the right tool for
    domain-aware tag extraction at $0.0001/triage.

    Locked output: distinct lowercase tokens, <= MAX_TAGS, no padding.
    """
    neighbour_titles = [str(c.fields.get("title") or "") for c in candidates]
    neighbour_descriptions = [
        str(c.fields.get("description") or "") for c in candidates
    ]

    # Algorithmic preprocessing — produces clean candidate words for the LLM
    # AND the fallback result if LLM unavailable.
    counts: Counter[str] = Counter()
    for tok in _tokenise(probe_title):
        counts[tok] += PROBE_TITLE_BOOST
    for tok in _tokenise(probe_description):
        counts[tok] += 1
    for title in neighbour_titles:
        for tok in _tokenise(title):
            counts[tok] += 1
    for desc in neighbour_descriptions:
        for tok in _tokenise(desc):
            counts[tok] += 1
    filtered = [
        (tok, n) for tok, n in counts.items()
        if tok not in _STOPWORDS
        and TAG_MIN_CHARS <= len(tok) <= TAG_MAX_CHARS
        and not tok.isdigit()
    ]
    ranked = sorted(filtered, key=lambda x: (-x[1], x[0]))
    candidate_pool = [tok for tok, _ in ranked[:20]]  # hint set for LLM
    algorithmic_fallback = [tok for tok, _ in ranked[:MAX_TAGS]]

    # LLM path
    if tag_fn is not None:
        try:
            raw = await tag_fn(
                probe_title=probe_title,
                probe_description=probe_description,
                neighbour_titles=neighbour_titles,
                neighbour_descriptions=neighbour_descriptions,
                candidate_pool=candidate_pool,
            )
        except Exception:
            return algorithmic_fallback
        normalised = _normalise_tag_list(raw) if raw else []
        return normalised or algorithmic_fallback

    return algorithmic_fallback


def _normalise_tag_list(raw: Any) -> list[str]:
    """Lowercase + dedupe + cap at MAX_TAGS. Drops non-strings / empties."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip().lower()
        if not s or len(s) < TAG_MIN_CHARS or len(s) > TAG_MAX_CHARS:
            continue
        if s.isdigit() or s in _STOPWORDS:
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= MAX_TAGS:
            break
    return out


def _tokenise(text: str) -> list[str]:
    """Lowercase tokens; keep hyphens inside words so 'Wi-Fi' stays whole."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


# ── Per-field aggregation + LLM tiebreak ─────────────────────────────────────

async def _suggest_when_no_votes(
    col: str, *, candidates: list[ScoredNeighbour], probe_text: str,
    ticket_row: Mapping[str, Any], propose_fn: ProposeFn | None,
) -> FieldSuggestion:
    """No neighbour had a value for `col`. Try the LLM proposer fallback (only
    on whitelisted fields, so we never invent assignees / CIs), else return an
    explicit empty-neighbours suggestion."""
    if propose_fn is not None and col in PROPOSE_ALLOWED_FIELDS:
        pool = _collect_pool_for_field(candidates, col)
        proposed = await _safe_propose(
            propose_fn, probe_text=probe_text, field=col,
            ticket_row=dict(ticket_row), pool=pool,
        )
        if proposed:
            return FieldSuggestion(
                value=proposed["value"], confidence=proposed["confidence"],
                coverage=0.0, diversity=0, basis_ids=[], basis="llm_propose",
                rationale=(
                    f"No similar tickets had a {col} value; LLM "
                    f"proposed '{proposed['value']}' "
                    f"({proposed.get('rationale','')})"
                ),
            )
    return FieldSuggestion(
        value=None, confidence=0.0, coverage=0.0, diversity=0, basis_ids=[],
        basis="empty_neighbours",
        rationale=f"No similar tickets had a {col} value to vote on.",
    )


async def _try_llm_tiebreak(
    col: str, *, votes: list[tuple[str, str]],
    candidates: list[ScoredNeighbour], counts: Counter[str],
    confidence: float, coverage: float, diversity: int,
    probe_text: str, ticket_row: Mapping[str, Any], tiebreak_fn: TiebreakFn | None,
) -> FieldSuggestion | None:
    """When the kNN vote is split (below the confidence floor) but has enough
    coverage + diversity to be worth an LLM read, ask the tiebreaker to pick
    the best semantic fit. Returns the suggestion, or None to fall back to the
    pure-kNN majority (gate not met / LLM error / choice not a candidate)."""
    if not (
        tiebreak_fn is not None
        and confidence < CONFIDENCE_FLOOR_FOR_LLM
        and coverage >= COVERAGE_MIN_FOR_LLM
        and diversity >= 2
    ):
        return None
    # Pass ALL distinct candidate values (not truncated). For each value,
    # attach the titles of the neighbours that used it — gives the LLM
    # concrete examples to ground its semantic reasoning.
    candidate_records = _build_llm_candidates(
        votes=votes, candidates=candidates, max_examples_per_value=2,
    )
    all_values = [c["value"] for c in candidate_records]
    try:
        llm_choice = await tiebreak_fn(
            probe_text=probe_text, field=col,
            candidates=candidate_records, ticket_row=dict(ticket_row),
        )
    except Exception:
        llm_choice = None
    if isinstance(llm_choice, str) and llm_choice.strip() in all_values:
        chosen = llm_choice.strip()
        return FieldSuggestion(
            value=chosen,
            confidence=counts.get(chosen, 0) / len(votes),
            coverage=coverage, diversity=diversity,
            basis_ids=[nid for v, nid in votes if v == chosen],
            basis="llm_tiebreak",
            rationale=(
                f"kNN vote was split ({_summary(counts)}); LLM read the "
                f"ticket description and chose '{chosen}' as the best "
                f"semantic fit."
            ),
        )
    return None


async def _suggest_for_field(
    col: str, *, candidates: list[ScoredNeighbour], total_k: int,
    probe_text: str, ticket_row: Mapping[str, Any],
    tiebreak_fn: TiebreakFn | None, propose_fn: ProposeFn | None,
) -> FieldSuggestion:
    """Single-field suggestion: kNN majority over the neighbours' values, with
    an LLM tiebreak when the vote is split, or the proposer fallback when no
    neighbour had a value."""
    votes: list[tuple[str, str]] = []  # (value, neighbour_id)
    for c in candidates:
        v = c.fields.get(col)
        if isinstance(v, str) and v.strip():
            votes.append((v, c.id))

    coverage = (len(votes) / total_k) if total_k else 0.0
    diversity = len({v for v, _ in votes})

    if not votes:
        return await _suggest_when_no_votes(
            col, candidates=candidates, probe_text=probe_text,
            ticket_row=ticket_row, propose_fn=propose_fn)

    counts = Counter(v for v, _ in votes)
    winner, winner_count = counts.most_common(1)[0]
    confidence = winner_count / len(votes)

    tb = await _try_llm_tiebreak(
        col, votes=votes, candidates=candidates, counts=counts,
        confidence=confidence, coverage=coverage, diversity=diversity,
        probe_text=probe_text, ticket_row=ticket_row, tiebreak_fn=tiebreak_fn)
    if tb is not None:
        return tb

    # Pure kNN majority result
    if confidence < CONFIDENCE_FLOOR_FOR_LLM and tiebreak_fn is None:
        rationale = (
            f"kNN vote split ({_summary(counts)}); top value '{winner}' "
            f"chosen by plurality. Consider human review."
        )
        basis: Any = "below_confidence_floor"
    else:
        rationale = f"{winner_count} of {total_k} similar tickets are '{winner}'."
        basis = "majority_of_top_k"
    return FieldSuggestion(
        value=winner, confidence=confidence, coverage=coverage,
        diversity=diversity,
        basis_ids=[nid for v, nid in votes if v == winner],
        basis=basis, rationale=rationale,
    )


async def _apply_propose_override(
    out: dict[str, FieldSuggestion], *, candidates: list[ScoredNeighbour],
    probe_text: str, ticket_row: Mapping[str, Any], propose_fn: ProposeFn,
) -> None:
    """LLM-propose override pass (parallel). For every whitelisted field whose
    chosen value is below the confidence floor, fire the proposer for all of
    them concurrently (bounds latency at ~one LLM round-trip instead of N).
    The override only takes effect when the proposed confidence exceeds the
    current one, so a weak proposal can't degrade a fine kNN answer."""
    weak_fields: list[str] = [
        c for c in out
        if c in PROPOSE_ALLOWED_FIELDS
        and out[c].confidence < CONFIDENCE_FLOOR_FOR_LLM
    ]
    if not weak_fields:
        return
    import asyncio
    results = await asyncio.gather(*[
        _safe_propose(
            propose_fn, probe_text=probe_text, field=c,
            ticket_row=dict(ticket_row),
            pool=_collect_pool_for_field(candidates, c),
        )
        for c in weak_fields
    ])
    for col, proposed in zip(weak_fields, results, strict=False):
        if proposed and proposed["confidence"] > out[col].confidence:
            prev = out[col]
            out[col] = FieldSuggestion(
                value=proposed["value"], confidence=proposed["confidence"],
                coverage=prev.coverage, diversity=prev.diversity,
                basis_ids=[], basis="llm_propose",
                rationale=(
                    f"kNN signal was weak (was '{prev.value}' at "
                    f"{prev.confidence:.2f}); LLM proposed "
                    f"'{proposed['value']}' "
                    f"({proposed.get('rationale','')})"
                ),
            )


async def _build_field_suggestions(
    *,
    candidates: list[ScoredNeighbour],
    schema: Mapping[str, Any],
    probe_text: str,
    ticket_row: Mapping[str, Any],
    tiebreak_fn: TiebreakFn | None,
    propose_fn: ProposeFn | None = None,
) -> dict[str, FieldSuggestion]:
    targets: list[str] = list(schema.get("aggregation_targets") or [])
    total_k = len(candidates)
    out: dict[str, FieldSuggestion] = {}

    for col in targets:
        out[col] = await _suggest_for_field(
            col, candidates=candidates, total_k=total_k,
            probe_text=probe_text, ticket_row=ticket_row,
            tiebreak_fn=tiebreak_fn, propose_fn=propose_fn)

    if propose_fn is not None:
        await _apply_propose_override(
            out, candidates=candidates, probe_text=probe_text,
            ticket_row=ticket_row, propose_fn=propose_fn)
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_llm_candidates(
    *,
    votes: list[tuple[str, str]],
    candidates: list[ScoredNeighbour],
    max_examples_per_value: int = 2,
) -> list[dict[str, Any]]:
    """For each distinct value the neighbours voted, return a record
    {value, vote_count, example_titles} so the LLM can reason about
    semantic fit instead of guessing from value names alone.
    """
    by_value: dict[str, list[str]] = {}
    counts = Counter(v for v, _ in votes)
    id_to_title: dict[str, str] = {
        c.id: str(c.fields.get("title") or "").strip()
        for c in candidates
    }
    for v, nid in votes:
        title = id_to_title.get(nid)
        if not title:
            continue
        by_value.setdefault(v, [])
        if len(by_value[v]) < max_examples_per_value and title not in by_value[v]:
            by_value[v].append(title)
    out: list[dict[str, Any]] = []
    for value, vote_count in counts.most_common():
        out.append({
            "value": value,
            "vote_count": vote_count,
            "example_titles": by_value.get(value, []),
        })
    return out


def _summary(counts: Counter[str]) -> str:
    parts = [f"{v}={n}" for v, n in counts.most_common(3)]
    return ", ".join(parts)


def _collect_pool_for_field(
    candidates: list[ScoredNeighbour], col: str,
) -> list[str]:
    """Distinct non-empty historical values for a field across kNN candidates.

    Passed to the LLM proposer as a guidance pool — the LLM is told to
    prefer a value from this pool when one fits, so we don't keep
    inventing new categories every turn.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for c in candidates:
        v = c.fields.get(col)
        if isinstance(v, str):
            v = v.strip()
            if v and v not in seen_set:
                seen.append(v)
                seen_set.add(v)
    return seen


async def _safe_propose(
    propose_fn: ProposeFn,
    *,
    probe_text: str,
    field: str,
    ticket_row: dict[str, Any],
    pool: list[str],
) -> dict[str, Any] | None:
    """Wrap propose_fn so any exception returns None instead of bubbling."""
    try:
        return await propose_fn(
            probe_text=probe_text,
            field=field,
            ticket_row=ticket_row,
            pool=pool,
        )
    except Exception:                                               # noqa: BLE001
        return None


def _value_of(
    suggestions: dict[str, FieldSuggestion], col: str
) -> str | None:
    fs = suggestions.get(col)
    return fs.value if fs else None


def _safe_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
