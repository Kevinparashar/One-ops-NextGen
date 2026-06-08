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
        for phrase in skill.get("use_when") or []:
            text = (phrase or "").strip()
            if text:
                chunks.append(("use_when", uw_i, text))
                uw_i += 1
        for phrase in skill.get("examples") or []:
            text = (phrase or "").strip()
            if text:
                chunks.append(("example", ex_i, text))
                ex_i += 1
    return chunks
