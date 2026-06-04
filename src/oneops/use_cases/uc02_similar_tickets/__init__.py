"""UC-2 Similar Tickets — read-only similarity over `ai.embeddings_<service>`.

Symptom-to-symptom cosine on `chunk_type='symptom_anchor'` (HNSW), metadata
re-rank from `itsm.<service>`, optional `diagnosis_trail` confirmation on the
top-K. Same `core.find_similar()` powers both the button route and the chat
handler, so the two paths return identical JSON for the same `(ticket_id, k,
role)` triple.
"""
from oneops.use_cases.uc02_similar_tickets.contracts import (
    SimilarTicket,
    SimilarTicketsRequest,
    SimilarTicketsResponse,
)
from oneops.use_cases.uc02_similar_tickets.core import find_similar

__all__ = [
    "find_similar",
    "SimilarTicket",
    "SimilarTicketsRequest",
    "SimilarTicketsResponse",
]
