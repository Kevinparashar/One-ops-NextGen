"""Agent embedding-input builder — turns an agent card body into vector chunks.

Single source of truth for what an agent's routing vectors are built from, used
by both database/agent/worker.py (live) and database/agent/sync.py-triggered
refreshes. One row per facet (multi-chunk, D2) for sharp per-phrase recall:

  * description : one chunk per skill.description
  * use_when    : one chunk per use_when phrase
  * example     : one chunk per example phrase

`not_when` is deliberately EXCLUDED — it's read only by the LLM disambiguator;
embedding a "don't pick me" phrase would attract the very queries it repels.

chunk_index is monotonic per chunk_type across all of the agent's skills, so the
PK (agent_id, chunk_type, chunk_index, embedding_version) stays unique for
multi-skill agents.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# (chunk_type, chunk_index, content_text)
AgentChunk = tuple[str, int, str]


def build_agent_chunks(body: Mapping[str, Any]) -> list[AgentChunk]:
    """Flatten an agent card body into (chunk_type, chunk_index, text) tuples."""
    skills = body.get("skills") or []
    chunks: list[AgentChunk] = []
    desc_i = uw_i = ex_i = 0
    for skill in skills:
        desc = (skill.get("description") or "").strip()
        if desc:
            chunks.append(("description", desc_i, desc))
            desc_i += 1
        uw_chunks, uw_i = _phrase_chunks(skill, "use_when", "use_when", uw_i)
        chunks.extend(uw_chunks)
        ex_chunks, ex_i = _phrase_chunks(skill, "examples", "example", ex_i)
        chunks.extend(ex_chunks)
    return chunks


def _phrase_chunks(
    skill: Mapping[str, Any], key: str, chunk_type: str, start_idx: int,
) -> tuple[list[AgentChunk], int]:
    """Non-empty, stripped phrases under `skill[key]` as (chunk_type, idx, text)
    tuples, continuing the global per-type index from `start_idx`. Returns
    (chunks, next_idx)."""
    out: list[AgentChunk] = []
    idx = start_idx
    for phrase in skill.get(key) or []:
        text = (phrase or "").strip()
        if text:
            out.append((chunk_type, idx, text))
            idx += 1
    return out, idx
