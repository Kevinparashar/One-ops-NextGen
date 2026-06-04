"""Adaptive KB body chunker.

Strategy (matches Anthropic Contextual Retrieval + Pinecone guidance):

  • Articles below TARGET_CHUNK_CHARS → single chunk (no overhead)
  • Otherwise → split with overlap, preferring natural boundaries:
        paragraph break > sentence end > word boundary > hard char split
  • Each chunk ≤ MAX_CHUNK_CHARS to keep well below the 8191-token embed limit
  • OVERLAP_CHARS bridge between consecutive chunks preserves cross-boundary
    context (e.g. "Step 3 continues from Step 2 above")

Used by `worker.py` when processing a kb_knowledge refresh: produces (anchor_text,
[body_chunk_text, ...]). Anchor is always one chunk; body is N chunks where N
depends on content length.
"""
from __future__ import annotations

# Tunables — match industry practice for text-embedding-3-large
TARGET_CHUNK_CHARS = 2000   # ~500 tokens — articles below this are single-chunk
OVERLAP_CHARS      = 200    # ~10% — context bridge between chunks
MAX_CHUNK_CHARS    = 6000   # ~1500 tokens — hard upper bound per chunk
MIN_CHUNK_CHARS    = 100    # don't emit trailing dust


def _find_split_point(text: str, start: int, target: int, hard_max: int) -> int:
    """Return the best character position to split between `start` and (start + hard_max).

    Prefers paragraph break, then sentence end, then word boundary, then hard
    character split. Always returns a value > start so we make progress.
    """
    if start + target >= len(text):
        return len(text)

    # Window we're willing to expand into for a better boundary
    window_start = start + max(MIN_CHUNK_CHARS, target - 400)
    window_end = min(len(text), start + hard_max)
    if window_end <= start:
        return len(text)

    window = text[window_start:window_end]

    # Look back from end of window for boundary candidates
    # 1. Paragraph (two or more newlines, or a blank line)
    for delim in ("\n\n\n", "\n\n"):
        idx = window.rfind(delim)
        if idx >= 0:
            return window_start + idx + len(delim)

    # 2. Sentence-ending punctuation followed by whitespace
    for delim in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = window.rfind(delim)
        if idx >= 0:
            return window_start + idx + len(delim)

    # 3. Single newline
    idx = window.rfind("\n")
    if idx >= 0:
        return window_start + idx + 1

    # 4. Word boundary (space)
    idx = window.rfind(" ")
    if idx >= 0:
        return window_start + idx + 1

    # 5. Hard split at the target boundary
    return min(start + target, len(text))


def split_body(content: str) -> list[str]:
    """Split content body into a list of chunks. Returns 1 chunk if content
    is short; multiple chunks with overlap otherwise. Empty content → []."""
    if not content or not content.strip():
        return []
    if len(content) <= TARGET_CHUNK_CHARS:
        return [content.strip()]

    chunks: list[str] = []
    cursor = 0
    n = len(content)
    # Effective forward stride per iteration is roughly TARGET - OVERLAP.
    # The progress guard uses this so a degenerate split (short tail) cannot
    # cause an infinite stream of tiny overlap-only chunks.
    _MIN_STRIDE = max(MIN_CHUNK_CHARS, TARGET_CHUNK_CHARS - OVERLAP_CHARS)
    while cursor < n:
        end = _find_split_point(content, cursor, TARGET_CHUNK_CHARS, MAX_CHUNK_CHARS)
        chunk = content[cursor:end].strip()
        if len(chunk) >= MIN_CHUNK_CHARS:
            chunks.append(chunk)
        # If this chunk reached end-of-document, we're done. No tail-fragment
        # loop possible.
        if end >= n:
            break
        # Otherwise advance with overlap, but never less than _MIN_STRIDE
        # forward — protects against degenerate cases where the boundary
        # finder returns a value close to `cursor`.
        next_cursor = end - OVERLAP_CHARS
        if next_cursor < cursor + _MIN_STRIDE:
            next_cursor = cursor + _MIN_STRIDE
        cursor = next_cursor

    return chunks


def build_anchor_text(row: dict) -> str:
    """Build the anchor (always single-chunk) — short, high-signal fields only.

    Mirrors the symptom_anchor pattern from ticket triage: a focused
    representation of "what this article IS about" rather than its contents.
    """
    parts: list[str] = []
    if row.get("title"):    parts.append(f"Title: {row['title']}")
    if row.get("summary"):  parts.append(f"Summary: {row['summary']}")
    if row.get("category"): parts.append(f"Category: {row['category']}")
    tags = row.get("tags")
    if tags:
        if isinstance(tags, (list, tuple)):
            tag_str = ", ".join(str(t) for t in tags if t)
        else:
            tag_str = str(tags)
        if tag_str:
            parts.append(f"Tags: {tag_str}")
    return "\n".join(parts)


def build_kb_chunks(row: dict) -> tuple[str, list[str]]:
    """Top-level entry: returns (anchor_text, body_chunks).

    anchor_text is always 1 string. body_chunks may be empty (no content) or
    contain 1..N chunks depending on content length.
    """
    anchor = build_anchor_text(row)
    body_chunks = split_body(row.get("content") or "")
    return anchor, body_chunks


__all__ = [
    "build_kb_chunks", "build_anchor_text", "split_body",
    "TARGET_CHUNK_CHARS", "OVERLAP_CHARS", "MAX_CHUNK_CHARS",
]
