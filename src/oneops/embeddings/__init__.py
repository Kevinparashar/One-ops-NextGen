"""Embedding-input builders shared across seed-time and query-time.

The same function MUST produce the embedding-input text whether we are:
  * backfilling existing rows (database/<service>/backfill.py), or
  * embedding a brand-new ticket at UC-5 query-time

Otherwise the two vector spaces drift and cosine similarity becomes
meaningless. This module is the single source of truth.
"""
from oneops.embeddings.triage_input import (
    build_canonical_anchor,
    build_embedding_input,
    enrich_incident,
    enrich_request,
    validate_embed_text,
)

__all__ = [
    "build_canonical_anchor",
    "build_embedding_input",
    "enrich_incident",
    "enrich_request",
    "validate_embed_text",
]
