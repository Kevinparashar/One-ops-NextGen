"""Prompt redaction — scrub PII before a prompt leaves to a provider.

ARCHITECTURE.md §9: PII is redacted before it reaches the LLM Gateway prompt.
This module is deterministic, pattern-based: it recognises structural PII
shapes (email, phone, SSN, credit-card, IP) and replaces each with a typed
placeholder. It is **not** a phrase list — it matches *structure*, not a
catalogue of known values.

`redact_messages` returns the scrubbed messages plus the set of PII classes it
found, so the gateway can record what was redacted on the response.
"""
from __future__ import annotations

import re

from oneops.llm.models import LlmMessage

# Structural PII patterns. Order matters — longer/more-specific first so a
# credit-card number is not partly eaten by the phone pattern.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("phone", re.compile(r"\b\+?\d[\d\s().-]{7,}\d\b")),
]


def redact_text(text: str) -> tuple[str, set[str]]:
    """Return `text` with structural PII replaced by typed placeholders, plus
    the set of PII classes found."""
    found: set[str] = set()
    out = text
    for label, pattern in _PATTERNS:
        if pattern.search(out):
            found.add(label)
            out = pattern.sub(f"[REDACTED_{label.upper()}]", out)
    return out, found


def redact_messages(
    messages: tuple[LlmMessage, ...],
) -> tuple[tuple[LlmMessage, ...], set[str]]:
    """Redact every message. Returns (scrubbed messages, PII classes found)."""
    scrubbed: list[LlmMessage] = []
    all_found: set[str] = set()
    for msg in messages:
        clean, found = redact_text(msg.content)
        all_found |= found
        scrubbed.append(LlmMessage(role=msg.role, content=clean) if found else msg)
    return tuple(scrubbed), all_found


__all__ = ["redact_text", "redact_messages"]
