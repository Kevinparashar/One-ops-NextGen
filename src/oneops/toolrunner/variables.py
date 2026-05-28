"""Variable store — large tool outputs as named variables with a preview.

Moveworks attention-budget discipline: a big tool payload must never be dumped
into the next LLM prompt. When a tool output exceeds the threshold, the runner
stores the full value here and substitutes a `VariableRef` (name + short
preview + size). The LLM context sees the preview; a later step can fetch the
full value by name.

`InMemoryVariableStore` is the P7 implementation — per-process, request-scoped.
The interface is small enough that a Dragonfly-backed store (shared across the
nodes of one fan-out) drops in without touching callers.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from oneops.toolrunner.models import VariableRef

# Outputs whose serialised form exceeds this go to the variable store.
DEFAULT_PREVIEW_THRESHOLD_BYTES = 4096
DEFAULT_PREVIEW_CHARS = 280


def _serialised_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _preview(value: Any, *, chars: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:chars] + ("…" if len(text) > chars else "")


class InMemoryVariableStore:
    """Request-scoped store of large values, keyed by generated variable name."""

    def __init__(
        self,
        *,
        threshold_bytes: int = DEFAULT_PREVIEW_THRESHOLD_BYTES,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
    ) -> None:
        self._threshold = threshold_bytes
        self._preview_chars = preview_chars
        self._lock = threading.RLock()
        self._values: dict[str, Any] = {}

    def capture(self, value: Any, *, hint: str = "var") -> Any:
        """Return `value` unchanged when small; otherwise store it and return a
        `VariableRef` in its place. This is what the runner calls on every
        tool output."""
        if _serialised_size(value) <= self._threshold:
            return value
        name = f"{hint}_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._values[name] = value
        return VariableRef(
            name=name,
            preview=_preview(value, chars=self._preview_chars),
            size_bytes=_serialised_size(value),
        )

    def get(self, name: str) -> Any:
        """Fetch a stored value by name. Raises KeyError if absent."""
        with self._lock:
            return self._values[name]

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._values

    @property
    def count(self) -> int:
        return len(self._values)


__all__ = [
    "InMemoryVariableStore",
    "DEFAULT_PREVIEW_THRESHOLD_BYTES",
    "DEFAULT_PREVIEW_CHARS",
]
