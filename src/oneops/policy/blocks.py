"""Policy blocks — loaded ONCE from docs/policies/updated_policy_v2.md at import.

Parser strategy: scan for ``BLOCK_NAME = <triple-quote> ... <triple-quote>``
declarations in the markdown. This is the shape the policy doc already uses
(it's literate-Python in disguise).

The parser is intentionally strict so accidental edits to the markdown that break
the BLOCK_NAME pattern raise a ConfigError at import time — fail-fast, never silently
serve a corrupted prompt to an LLM.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from oneops.errors import ConfigError
from oneops.observability import get_logger

_log = get_logger("oneops.policy.blocks")

# Default source: docs/policies/updated_policy_v2.md (relative to project root)
_DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[3] / "docs" / "policies" / "updated_policy_v2.md"
)

# Matches lines like:   COMMON_SAFETY_RULES = """
#                       ... content ...
#                       """
# Tolerant of extra whitespace before/after the assignment and around the triple quote.
_BLOCK_RE = re.compile(
    r"^(?P<name>[A-Z][A-Z0-9_]+)\s*=\s*\"\"\"(?P<body>.*?)\"\"\"\s*$",
    re.DOTALL | re.MULTILINE,
)


def _parse_blocks(source: Path) -> dict[str, str]:
    """Parse the policy markdown into name → body dict.

    Raises ConfigError on file-not-found or zero matches (the source is corrupt).
    """
    if not source.is_file():
        raise ConfigError(f"policy source not found: {source}")
    text = source.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for m in _BLOCK_RE.finditer(text):
        name = m.group("name")
        body = m.group("body").strip("\n")
        if name in found:
            _log.warning("policy.duplicate_block_in_source", name=name, source=str(source))
        found[name] = body
    if not found:
        raise ConfigError(
            f"policy source produced zero blocks — pattern mismatch in {source}"
        )
    _log.info("policy.blocks_loaded", count=len(found), source=str(source))
    return found


# Eager parse at import. Immutable after.
_blocks_raw = _parse_blocks(_DEFAULT_SOURCE)
POLICY_BLOCKS: Mapping[str, str] = MappingProxyType(_blocks_raw)


def get_block(name: str) -> str:
    """Return the policy block by name. Raises KeyError if absent."""
    try:
        return POLICY_BLOCKS[name]
    except KeyError as e:
        raise KeyError(
            f"policy block {name!r} not found; available: {sorted(POLICY_BLOCKS)}"
        ) from e


def list_block_names() -> list[str]:
    """Sorted list of all known block names."""
    return sorted(POLICY_BLOCKS)


__all__ = ["POLICY_BLOCKS", "get_block", "list_block_names"]
