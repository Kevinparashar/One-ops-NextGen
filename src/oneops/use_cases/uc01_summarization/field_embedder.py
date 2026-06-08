"""Embedding-based field matcher (port of AI-oneops V1 `field_matcher.py`).

Replaces the keyword `_SYNONYMS` regex table with deterministic embedding
cosine matching. The principle (from the thumb rule
[[feedback_descriptions_principle_not_phrases]]): a field's semantic
description IS the routing signal; the matcher does semantic comparison
against it. New phrasings (`"data on this"`, `"solution"`, `"guidance"`,
`"why did it happen"`) work because embeddings capture semantics, not
patterns. New fields work because the matcher reads the live catalog.
No phrase list to maintain.

Architecture:
  1. At first call (per process), embed every field's semantic description
     ONCE via the LLM Gateway (caches the result for the process lifetime).
  2. At each request, embed the user's message.
  3. Cosine-match against every catalog field. Top match >= threshold
     wins; ambiguity-delta detects compound queries.
  4. The matcher returns ALL labels whose cosine >= threshold (multi-field
     ready), or `None` to fall through to the LLM extractor.

Fail-OPEN: any embedding error → returns None so the caller falls through
to the existing LLM field-read extractor. Worst case: no behaviour change.

Integration with UC-1's tools.py: the matcher is consulted BEFORE the LLM
extractor (same slot the keyword `_try_deterministic_extract` currently
occupies). When it returns a confident match, the LLM call is skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
from collections.abc import Callable
from typing import Any

from oneops.observability import get_logger

_log = get_logger("oneops.uc01.field_embedder")

# ── Tunables ──────────────────────────────────────────────────────────
EMBED_MODEL = os.environ.get("UC01_FIELD_EMBED_MODEL", "text-embedding-3-large")
EMBED_DIMENSIONS = int(os.environ.get("UC01_FIELD_EMBED_DIM", "1536"))

# Cosine threshold for a confident single-field match. Calibrated against
# the V1 result set: genuine field questions floor at ~0.338; non-field
# stragglers top out at ~0.313 → 0.33 cleanly separates them.
MATCH_THRESHOLD = float(os.environ.get("UC01_FIELD_THRESHOLD", "0.33"))

# When the runner-up field is within this delta of the top, the query is
# multi-field. The matcher emits all crossers in score order.
AMBIGUITY_DELTA = float(os.environ.get("UC01_FIELD_AMBIGUITY_DELTA", "0.05"))


# ── Semantic descriptions for every canonical record-field label ──────
# These ARE the routing signal — the embedding similarity to these
# sentences is what decides which field the user asked for. Adding a new
# field is one entry here; the matcher picks it up automatically.
_FIELD_DESCRIPTIONS: dict[str, str] = {
    # state
    "Status": "current status, state or condition of the record (open, in_progress, resolved, closed, known_error, scheduled)",
    "State": "current state of the change record (draft, scheduled, in_progress, complete)",
    "Stage": "current stage in the workflow",
    "Priority": "priority level, importance, severity classification (P1, P2, P3, P4)",
    "Severity": "severity rating (high, medium, low)",
    "Impact": "business impact severity (high, medium, low)",
    "Urgency": "urgency rating (high, medium, low)",
    "Approval Status": "approval state (pending, approved, rejected)",
    # classification
    "Category": "high-level category or classification group",
    "Subcategory": "specific subcategory within the category",
    "Service": "the IT service this record relates to (Corporate VPN, Payroll DB, ...)",
    # people / ownership
    "Reported By": "the user who reported, raised, opened or filed the record",
    "Requested By": "the user who requested or submitted the record",
    "Requested For": "the user the record was requested on behalf of",
    "Assigned To": "the user the record is currently assigned to or owned by, person handling it",
    "Assignment Group": "the team, group or squad the record is assigned to",
    "Owner": "the user who owns the record, person responsible",
    "Approved By": "the list of users who approved, signed off or authorised the record",
    # timing
    "SLA Due": "the SLA deadline, due date by which the record must be resolved",
    "SLA Breached": "whether the SLA has been breached, broken or overdue",
    "Planned Start": "the planned start date and time of the change",
    "Planned End": "the planned end date and time of the change",
    "Created At": "when the record was created, opened, raised or filed",
    "Updated At": "when the record was last updated, modified or edited",
    "Resolved At": "when the record was resolved, closed or completed",
    # long-form
    "Title": "the short title, summary headline or name of the record",
    "Description": "the long-form description of the record",
    # diagnostic / resolution
    "Root Cause": "the root cause, RCA, underlying reason, why the problem happened",
    "Workaround": "the workaround, temporary fix or interim mitigation for the problem",
    "Resolution Notes": "the resolution notes, what was done to fix the record",
    # changes / risk
    "Type": "the type of change (standard, normal, emergency)",
    "Risk Level": "the risk level of the change (high, medium, low)",
    "Risk": "the risk of the change",
    # linked records
    "Configuration Item": "the configuration item this record is linked to",
    "Linked CIs": "the configuration items linked to this record",
    "Affected CIs": "the configuration items affected by this change",
    "Related Problem": "the linked problem record this incident or change is related to",
    "Related Problems": "the linked problem records related to this",
    "Related Change": "the linked change record this incident or problem is related to",
    "Related Changes": "the linked change records related to this",
    "Related Incident": "the linked incident this record references",
    "Related Incidents": "the linked incident records related to this",
    "Parent Problem": "the parent problem record",
    "Parent Incident": "the parent incident record",
    "Linked KB": "the linked knowledge base article references",
    # asset / CI specifics
    "Class": "the asset or CI class (hardware, software, virtual)",
    "Subtype": "the asset or CI subtype within the class",
    "Model": "the manufacturer model name or number",
    "Vendor": "the vendor or manufacturer name",
    "Location": "the physical location (office, region, data centre)",
    "Asset ID": "the canonical asset id",
    "Asset Name": "the asset's human-readable name",
    "Serial Number": "the asset's serial number",
    "Purchase Date": "the asset's purchase date",
    "Warranty Expiry": "the asset's warranty expiry date",
    "Criticality": "the criticality rating of the configuration item",
    "Incident ID": "the canonical incident id",
    "Problem ID": "the canonical problem id",
    "Change ID": "the canonical change id",
    "Known Error": "whether the problem is a known error",
    # conversational threads
    "Work Notes": "the internal work notes, agent investigation notes",
    "Comments": "the public comments and customer-facing messages",
    "Attachments": "the file attachments on the record",
}

# Grammar-level noise stripped before embedding (per V1 pattern). These
# are TOKENISATION rules, not phrasebooks — same status as lowercasing.
_ID_TOKEN_RE = re.compile(r"\b[A-Za-z]{2,6}\d{4,}\b")
_BARE_NUM_RE = re.compile(r"\b\d{4,}\b")
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "of", "it", "its", "in", "on", "at", "to", "for", "this", "that",
    "do", "does", "did", "done", "me", "my", "i", "and", "or", "with",
    "about", "please", "can", "could", "would", "will", "you", "your",
    "tell", "give", "show", "get", "there", "any", "some",
})


# ── Process-lifetime caches ───────────────────────────────────────────
_field_vectors: dict[str, list[float]] | None = None     # label → vec
_msg_cache: dict[str, list[float]] = {}                  # msg-hash → vec
_field_vec_lock = asyncio.Lock()


# A vector magnitude at or below this is treated as a zero vector: cosine is
# undefined and dividing by it is numerically unstable. Using an epsilon (not
# an exact `== 0.0`) also guards against denormal/near-zero norms (S1244).
_ZERO_NORM_EPSILON = 1e-12


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= _ZERO_NORM_EPSILON or nb <= _ZERO_NORM_EPSILON:
        return 0.0
    return dot / (na * nb)


def _normalise_for_embed(msg: str) -> list[str]:
    """Strip entity-id tokens + bare numerics. Build TWO views: full and
    stopword-stripped. Taking max cosine over both views means stopword
    stripping can only RESCUE diluted queries ("what state is it in" →
    "state") and never degrade queries where function words carried
    signal ("who is working on it")."""
    no_ids = _ID_TOKEN_RE.sub(" ", msg or "")
    no_ids = (_BARE_NUM_RE.sub(" ", no_ids)).strip() or (msg or "")
    content = " ".join(
        w for w in no_ids.split()
        if w.strip(".,;:!?'\"()[]").lower() not in _STOPWORDS
    ).strip()
    views = [no_ids]
    if content and content != no_ids:
        views.append(content)
    return views


# ── Public: build a field matcher bound to one LLM Gateway ────────────

async def _ensure_field_vectors(
    gateway: Any, model: str, dimensions: int | None, tenant_id: str,
) -> dict[str, list[float]] | None:
    """Bootstrap (once, process-lifetime) the label → description vectors."""
    global _field_vectors
    if _field_vectors is not None:
        return _field_vectors
    async with _field_vec_lock:
        if _field_vectors is not None:
            return _field_vectors
        labels = sorted(_FIELD_DESCRIPTIONS.keys())
        texts = [f"{lbl}. {_FIELD_DESCRIPTIONS[lbl]}" for lbl in labels]
        try:
            vectors = await gateway.embed(
                texts, model=model,
                tenant_id=tenant_id or "_unknown",
                user_id="", dimensions=dimensions,
            )
        except Exception as exc:                    # noqa: BLE001
            _log.warning("field_embedder.bootstrap_failed",
                         error=str(exc)[:200])
            return None
        if not vectors or len(vectors) != len(labels):
            return None
        _field_vectors = {lbl: [float(x) for x in vec]
                          for lbl, vec in zip(labels, vectors, strict=False)}
        _log.info("field_embedder.bootstrapped",
                  label_count=len(labels), model=model)
        return _field_vectors


async def _embed_message(
    gateway: Any, model: str, dimensions: int | None,
    text: str, tenant_id: str, user_id: str,
) -> list[float] | None:
    """Embed one message view, with a process-lifetime hash cache."""
    if not text:
        return None
    h = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()[:24]
    cached = _msg_cache.get(h)
    if cached is not None:
        return cached
    try:
        vectors = await gateway.embed(
            [text], model=model,
            tenant_id=tenant_id or "_unknown",
            user_id=user_id or "",
            dimensions=dimensions,
        )
    except Exception as exc:                        # noqa: BLE001
        _log.warning("field_embedder.message_embed_failed",
                     error=str(exc)[:200])
        return None
    if not vectors:
        return None
    vec = [float(x) for x in vectors[0]]
    if len(_msg_cache) > 2000:
        _msg_cache.clear()
    _msg_cache[h] = vec
    return vec


async def _embed_views(
    gateway: Any, model: str, dimensions: int | None,
    views: list[str], tenant_id: str, user_id: str,
) -> list[list[float]]:
    """Embed every message view, dropping the ones that fail to embed."""
    out: list[list[float]] = []
    for v in views:
        mv = await _embed_message(gateway, model, dimensions, v, tenant_id, user_id)
        if mv is not None:
            out.append(mv)
    return out


def _select_matches(scored: list[tuple[str, float]]) -> list[str] | None:
    """From labels sorted by score (desc), collect every label at or above
    `(top - AMBIGUITY_DELTA)` AND `>= MATCH_THRESHOLD` — so multi-field
    queries surface both labels in score order. None if nothing clears."""
    if not scored or scored[0][1] < MATCH_THRESHOLD:
        return None
    cutoff = max(MATCH_THRESHOLD, scored[0][1] - AMBIGUITY_DELTA)
    return [lbl for lbl, sc in scored if sc >= cutoff]


async def _match_fields(
    gateway: Any, model: str, dimensions: int | None,
    user_message: str, available_labels: list[str], *,
    tenant_id: str = "", user_id: str = "",
) -> list[str] | None:
    """Core field matcher — see `build_field_embedder` for the contract."""
    if not user_message or not available_labels:
        return None
    field_vecs = await _ensure_field_vectors(gateway, model, dimensions, tenant_id)
    if field_vecs is None:
        return None                                  # fail-OPEN

    # Restrict to labels present in BOTH the catalog descriptions AND the
    # focus record's schema. A label without a description silently falls
    # out — that's correct: we have nothing semantic to match against. Add
    # a description to _FIELD_DESCRIPTIONS and it appears.
    candidate_labels = [lbl for lbl in available_labels if lbl in field_vecs]
    if not candidate_labels:
        return None

    views = _normalise_for_embed(user_message)
    msg_vecs = await _embed_views(gateway, model, dimensions, views, tenant_id, user_id)
    if not msg_vecs:
        return None

    scored = sorted(
        (
            (lbl, max(_cosine(mv, field_vecs[lbl]) for mv in msg_vecs))
            for lbl in candidate_labels
        ),
        key=lambda kv: kv[1], reverse=True,
    )
    matched = _select_matches(scored)
    if matched is None:
        return None
    _log.info(
        "field_embedder.matched",
        user_message=user_message[:100],
        matched=matched,
        top=(scored[0][0], round(scored[0][1], 3)),
        runner=(scored[1][0], round(scored[1][1], 3)) if len(scored) > 1 else None,
    )
    return matched


def build_field_embedder(
    gateway: Any, *, model: str = EMBED_MODEL,
    dimensions: int | None = EMBED_DIMENSIONS,
):
    """Return an async field matcher that uses `gateway.embed` for vector
    computation. Wire-once at app boot via `set_field_embedder(fn)`.

    Signature of returned matcher:
        async fn(user_message, available_labels, *, tenant_id, user_id)
        -> list[str] | None

    `available_labels` is the focus record's humanised label set (the
    schema). The matcher returns ONLY labels in this set whose cosine
    similarity to the message clears `MATCH_THRESHOLD`, ordered by
    score. Returns `None` on embedding failure (fail-OPEN → caller falls
    through to the LLM extractor).
    """

    async def _matcher(
        user_message: str, available_labels: list[str], *,
        tenant_id: str = "", user_id: str = "",
    ) -> list[str] | None:
        return await _match_fields(
            gateway, model, dimensions, user_message, available_labels,
            tenant_id=tenant_id, user_id=user_id)

    return _matcher


# ── Wire-once injection point ─────────────────────────────────────────
FieldMatcher = Callable[..., Any]
_matcher_fn: FieldMatcher | None = None


def set_field_embedder(fn: FieldMatcher | None) -> None:
    global _matcher_fn
    _matcher_fn = fn


def get_field_embedder() -> FieldMatcher | None:
    return _matcher_fn


__all__ = [
    "build_field_embedder",
    "set_field_embedder",
    "get_field_embedder",
    "MATCH_THRESHOLD",
    "AMBIGUITY_DELTA",
]
