"""Capability-class classifier — narrow a query to its plausible need-KINDS.

Stage 2.5 of routing. It does NOT force a single kind (that biased ambiguous
queries toward whichever kind was ranked highest). Instead it KEEPS the top kind
plus any kind whose centroid is within a small band of the top, and DROPS the
clearly-far kinds. The router then admits only agents whose capability is in the
kept set. This:

  • removes a cross-kind overlap when one side is clearly wrong — "database
    payroll issue" sits far from the record_retrieval centroid (whose exemplars
    are all "find tickets like X"), so uc02 is dropped and the coin-flip with
    uc03 disappears; while
  • PRESERVES genuine ambiguity — when knowledge and fulfilment are both close
    ("I need the VPN"), BOTH survive, so the existing KB-default-then-offer-SR
    flow decides exactly as before. The filter steps aside when unsure.

DATA-DERIVED & SCALE-INVARIANT: each kind's centroid is the mean of the EMBEDDED
`use_when` / `examples` / `description` of the agents that declare that kind (the
cards' own routing data), seeded with the kind's principle. Nothing is
hand-authored — add an agent and its examples extend its kind's centroid
automatically; the work is still one cosine per kind, independent of agent
count. Degrades safely: no embedder ⇒ returns None ⇒ the filter is inert.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Protocol

from oneops.observability import get_logger
from oneops.registry.capabilities import (
    CapabilityTaxonomy,
    get_capability_taxonomy,
)

_log = get_logger("oneops.router.capability_classifier")

# Cap per-agent exemplar texts so centroid build stays bounded as the agent
# pool grows (at true scale, precompute from ai.embeddings_agent instead).
_MAX_TEXTS_PER_AGENT = 12


def capability_filter_enabled() -> bool:
    """Stage-2.5 capability filter flag (default OFF). OFF ⇒ routing is
    byte-identical to today (safe rollback). Set
    ONEOPS_ROUTER_CAPABILITY_FILTER to 1/true/yes/on to enable."""
    return os.getenv("ONEOPS_ROUTER_CAPABILITY_FILTER", "0").strip().lower() in (
        "1", "true", "yes", "on")


def _neg_weight() -> float:
    """How hard a query's similarity to a kind's NEGATIVE exemplars pushes that
    kind down (contrastive subtraction). 0 ⇒ positive-only (no contrast);
    higher ⇒ stronger 'what it is NOT' signal. Tunable without a redeploy."""
    try:
        return float(os.getenv("ONEOPS_ROUTER_KIND_NEG_WEIGHT", "1.0"))
    except ValueError:
        return 1.0


def _kept_band() -> float:
    """Keep every kind whose centroid cosine is within this gap of the top
    kind; drop the rest. Wider ⇒ keeps more kinds (softer filter, safer on
    ambiguity); narrower ⇒ stricter. Tunable without a redeploy."""
    try:
        return float(os.getenv("ONEOPS_ROUTER_KIND_BAND", "0.05"))
    except ValueError:
        return 0.05


class _Embedder(Protocol):
    async def embed(self, text: str, *, tenant_id: str) -> list[float]: ...


@dataclass(frozen=True)
class KindResult:
    """The kept set of plausible kinds (top + any within the band) plus the
    top kind and its margin (for spans / determinism debugging)."""

    kept_kinds: frozenset[str]
    top_kind: str
    top_score: float
    margin: float              # top-1 minus top-2 cosine


def _mean(vecs: list[list[float]]) -> list[float]:
    if not vecs:
        return []
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class CapabilityClassifier:
    """Classify a query into its plausible kinds from data-derived centroids.

    `registry` supplies the agents (and their skill cards) per capability;
    `embedder` exposes `embed(text, *, tenant_id) -> list[float]` (share the
    router's embedder so the query vector is a warm-cache hit)."""

    def __init__(
        self, *, embedder: _Embedder | None, registry: Any,
        taxonomy: CapabilityTaxonomy | None = None,
    ) -> None:
        self._embedder = embedder
        self._registry = registry
        self._tax = taxonomy or get_capability_taxonomy()
        self._pos: dict[str, list[float]] | None = None
        self._neg: dict[str, list[float]] = {}

    def _texts_for_kind(self, kind: str, *, negative: bool) -> list[str]:
        """Exemplar texts defining a kind. Positive = the principle (seed) +
        use_when / examples / description of agents declaring it. Negative =
        the negative_examples of those agents (what the kind must NOT win),
        used to push a query off the kind contrastively."""
        texts: list[str] = [] if negative else [self._tax.principle(kind)]
        try:
            agents = self._registry.agents.list_active()
        except Exception:                                          # noqa: BLE001
            agents = []
        for a in agents:
            if kind not in (getattr(a, "capabilities", ()) or ()):
                continue
            per_agent: list[str] = []
            for sk in getattr(a, "skills", ()) or ():
                if negative:
                    per_agent.extend(getattr(sk, "negative_examples", ()) or ())
                else:
                    per_agent.extend(getattr(sk, "use_when", ()) or ())
                    per_agent.extend(getattr(sk, "examples", ()) or ())
                    if getattr(sk, "description", ""):
                        per_agent.append(sk.description)
            texts.extend(per_agent[:_MAX_TEXTS_PER_AGENT])
        return list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))

    async def _centroid(self, texts: list[str], tenant_id: str) -> list[float]:
        vecs = [await self._embedder.embed(t, tenant_id=tenant_id)  # type: ignore[union-attr]
                for t in texts]
        return _mean(vecs) if vecs else []

    async def _ensure_centroids(self, tenant_id: str) -> None:
        if self._pos is not None:
            return
        pos: dict[str, list[float]] = {}
        neg: dict[str, list[float]] = {}
        for e in self._tax.entries():
            kind = e["id"]
            pv = await self._centroid(self._texts_for_kind(kind, negative=False), tenant_id)
            if pv:
                pos[kind] = pv
            nv = await self._centroid(self._texts_for_kind(kind, negative=True), tenant_id)
            if nv:
                neg[kind] = nv
        self._pos, self._neg = pos, neg
        _log.info("router.kind_centroids_built",
                  pos_kinds=sorted(pos), neg_kinds=sorted(neg))

    async def classify(
        self, query_text: str, *, tenant_id: str,
    ) -> KindResult | None:
        """Return the kept set of plausible kinds, or None when no embedder is
        wired (caller then leaves the candidate set untouched). Each kind is
        scored CONTRASTIVELY: similarity to its positive centroid MINUS
        `_neg_weight()` × similarity to its negative centroid."""
        if self._embedder is None or not query_text.strip():
            return None
        try:
            await self._ensure_centroids(tenant_id)
            qv = await self._embedder.embed(query_text, tenant_id=tenant_id)
        except Exception as exc:                                   # noqa: BLE001
            _log.warning("router.kind_classify.embed_failed", error=str(exc)[:160])
            return None
        w = _neg_weight()
        scored = sorted(
            ((_cosine(qv, c) - w * _cosine(qv, self._neg.get(k, [])), k)
             for k, c in (self._pos or {}).items()),
            reverse=True)
        if not scored:
            return None
        top_score, top_kind = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        band = _kept_band()
        kept = frozenset(k for s, k in scored if top_score - s <= band)
        return KindResult(kept, top_kind, top_score, top_score - second)


__all__ = [
    "CapabilityClassifier",
    "KindResult",
    "capability_filter_enabled",
]
