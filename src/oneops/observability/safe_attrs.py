"""Safe-by-default attribute helpers for OTel spans.

Sensitive text is replaced by (hash, length) attributes. Raw text only
appears in attributes when OTEL_CAPTURE_TEXT=true. Even when the flag is
on, certain fields (ticket body, work notes, KB content) must never be
captured — enforced at the call site by choosing whether to use these
helpers.

Never raises — observability errors must not break business code.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any


def capture_text_enabled() -> bool:
    """Read env each call so tests can flip the flag without restarting."""
    return os.environ.get("OTEL_CAPTURE_TEXT", "").strip().lower() == "true"


def safe_hash_text(text: str | None) -> str:
    """Stable short SHA-256 prefix (16 hex chars). Empty/None → ''."""
    if not text:
        return ""
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def safe_text_len(text: str | None) -> int:
    """Character length. None → 0. Never raises."""
    if not text:
        return 0
    try:
        return len(text)
    except Exception:
        return 0


def set_safe_text_attrs(span: Any, prefix: str, text: str | None) -> None:
    """Set <prefix>_hash and <prefix>_len attributes on a span.

    When OTEL_CAPTURE_TEXT=true and text is non-empty, also sets
    <prefix>_text (bounded to 4 KiB). Never raises.
    """
    if span is None or not hasattr(span, "set_attribute"):
        return
    try:
        span.set_attribute(f"{prefix}_hash", safe_hash_text(text))
        span.set_attribute(f"{prefix}_len", safe_text_len(text))
        if capture_text_enabled() and text:
            span.set_attribute(f"{prefix}_text", text[:4096])
    except Exception:
        pass


def safe_json_attr(value: Any, max_len: int = 2048) -> str:
    """Serialise to JSON and truncate. Never raises.

    Truncation suffix `...[truncated]` makes truncation visible.
    """
    try:
        s = json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        try:
            s = str(value)
        except Exception:
            return ""
    if len(s) <= max_len:
        return s
    return s[: max_len - 14] + "...[truncated]"


def safe_list_attr(values: Any, max_items: int = 10) -> list[str]:
    """Cap a list at max_items; stringify each; truncate each to 200 chars."""
    if values is None:
        return []
    try:
        lst = list(values)
    except Exception:
        return []
    out: list[str] = []
    for v in lst[:max_items]:
        try:
            s = str(v)
        except Exception:
            s = ""
        out.append(s[:200])
    return out


__all__ = [
    "capture_text_enabled",
    "safe_hash_text",
    "safe_text_len",
    "set_safe_text_attrs",
    "safe_json_attr",
    "safe_list_attr",
]
