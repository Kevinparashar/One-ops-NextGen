"""Glossary normalization — stage 1 of the routing funnel.

Domain vocabulary varies ("pwd", "passwd", "pass word" all mean *password*).
The `Glossary` collapses synonyms to a canonical term *before* semantic
retrieval, so retrieval scores against stable vocabulary rather than every
spelling a user might pick. This is the Parlant glossary pattern — a
first-class **data** file (`registries/v2/platform/glossary.json`), not code.

Deterministic: whole-word / whole-phrase, case-insensitive replacement; longer
synonyms are applied first so a multi-word synonym wins over a single-word one.
Tenant glossaries overlay the platform base via `overlaid_with()`.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from oneops.errors import ConfigError
from oneops.observability import get_logger

_log = get_logger("oneops.router.glossary")

_DEFAULT_GLOSSARY = "registries/v2/platform/glossary.json"


class Glossary:
    """Synonym → canonical-term normalizer."""

    def __init__(self, synonym_to_canonical: dict[str, str]) -> None:
        # Lower-cased synonym → canonical. Compile one regex per synonym,
        # ordered longest-first so multi-word phrases match before their parts.
        self._pairs: list[tuple[re.Pattern[str], str]] = []
        for synonym in sorted(synonym_to_canonical, key=len, reverse=True):
            canonical = synonym_to_canonical[synonym]
            pattern = re.compile(rf"\b{re.escape(synonym)}\b", re.IGNORECASE)
            self._pairs.append((pattern, canonical))
        self._raw = dict(synonym_to_canonical)

    @classmethod
    def from_file(cls, path: str | None = None) -> Glossary:
        """Load the platform base glossary from its JSON data file."""
        if path is None:
            repo_root = Path(__file__).resolve().parents[3]
            path = str(repo_root / _DEFAULT_GLOSSARY)
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"glossary file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"glossary file unreadable: {p}", cause=exc) from exc

        mapping: dict[str, str] = {}
        for entry in doc.get("entries", []):
            canonical = entry.get("canonical")
            if not canonical:
                raise ConfigError(f"glossary entry without `canonical` in {p}")
            for synonym in entry.get("synonyms", []):
                mapping[str(synonym).lower()] = canonical
        _log.info("router.glossary_loaded", synonym_count=len(mapping), source=str(p))
        return cls(mapping)

    def overlaid_with(self, tenant_entries: dict[str, str]) -> Glossary:
        """Return a new glossary with a tenant's synonyms layered on top of the
        platform base. Tenant entries win on a key collision."""
        merged = dict(self._raw)
        merged.update({k.lower(): v for k, v in tenant_entries.items()})
        return Glossary(merged)

    def normalize(self, text: str) -> str:
        """Replace every known synonym in `text` with its canonical term."""
        if not text:
            return text
        out = text
        for pattern, canonical in self._pairs:
            out = pattern.sub(canonical, out)
        return out

    @property
    def synonym_count(self) -> int:
        return len(self._raw)


__all__ = ["Glossary"]
