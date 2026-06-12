"""Capability taxonomy — the bounded, scale-invariant set of NEED KINDS.

Loaded from `registries/v2/platform/capabilities.json`. The router classifies a
query into ONE kind, then admits only agents whose declared `capabilities`
include it (capability-class routing). The taxonomy stays ~5 entries no matter
how many agents exist, which is why it scales where a per-agent/per-pair scheme
does not.

Mirrors the `field_policy` singleton pattern: process-wide, loaded on first use,
replaceable in tests via `set_capability_taxonomy`. Never partially-initialised —
a malformed file is a fatal ConfigError at load (a service must not route on a
broken taxonomy).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from oneops.errors import ConfigError

_DEFAULT_PATH = "registries/v2/platform/capabilities.json"


class CapabilityTaxonomy:
    """The closed set of capability classes, each with a tie-break `priority`
    (higher wins a cross-kind tie) and a semantic `principle` (NOT a keyword
    list — §2.1) the classifier uses to map a query to a kind."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            raise ConfigError("capabilities taxonomy is empty")
        self._by_id: dict[str, dict[str, Any]] = {}
        for e in entries:
            cid = str(e.get("id") or "").strip()
            if not cid:
                raise ConfigError(f"capability entry missing id: {e!r}")
            if cid in self._by_id:
                raise ConfigError(f"duplicate capability id: {cid}")
            if "priority" not in e:
                raise ConfigError(f"capability {cid} missing priority")
            if not str(e.get("principle") or "").strip():
                raise ConfigError(f"capability {cid} missing principle")
            self._by_id[cid] = {
                "id": cid,
                "priority": int(e["priority"]),
                "principle": str(e["principle"]).strip(),
            }
        # Highest priority first — the deterministic cross-kind tie order.
        self._order = sorted(
            self._by_id.values(), key=lambda e: e["priority"], reverse=True)

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._by_id)

    def priority(self, capability: str) -> int:
        return self._by_id[capability]["priority"]

    def principle(self, capability: str) -> str:
        return self._by_id[capability]["principle"]

    def entries(self) -> list[dict[str, Any]]:
        """All entries, highest-priority first (deterministic tie order)."""
        return [dict(e) for e in self._order]

    def best_of(self, capabilities: frozenset[str] | set[str]) -> str | None:
        """The highest-priority capability among `capabilities` (the cross-kind
        tie-break). None when the set is empty or contains no known id."""
        known = [c for c in self._order if c["id"] in capabilities]
        return known[0]["id"] if known else None

    @classmethod
    def from_registry_file(cls, path: str | None = None) -> CapabilityTaxonomy:
        if path is None:
            path = str(Path(__file__).resolve().parents[3] / _DEFAULT_PATH)
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"capabilities file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"capabilities unreadable: {p}", cause=exc) from exc
        return cls(doc.get("capabilities", []))


_taxonomy: CapabilityTaxonomy | None = None


def get_capability_taxonomy() -> CapabilityTaxonomy:
    """The process-wide capability taxonomy, loaded on first use."""
    global _taxonomy
    if _taxonomy is None:
        _taxonomy = CapabilityTaxonomy.from_registry_file()
    return _taxonomy


def set_capability_taxonomy(taxonomy: CapabilityTaxonomy | None) -> None:
    """Replace the process-wide taxonomy — used by tests."""
    global _taxonomy
    _taxonomy = taxonomy


__all__ = [
    "CapabilityTaxonomy",
    "get_capability_taxonomy",
    "set_capability_taxonomy",
]
