"""Router layer (P5) — the four-stage routing funnel.

    glossary normalization → semantic retrieval → condition+ABAC filter
    → LLM disambiguation  →  RouteResult (plan DAG | non-routed outcome)

Three of four stages are deterministic; the LLM only disambiguates an
already-narrowed, already-eligible candidate set. Routing decisions consult
the registry's declarative activation conditions and the P4 ABAC rules — never
a phrase catalogue.

Public surface:
    from oneops.router import Router, RouteResult, RouteOutcome, RoutePlan
    from oneops.router import Glossary, RequestSignals
    from oneops.router import LexicalRetriever, PgVectorRetriever
    from oneops.router import ThresholdDisambiguator
"""
from __future__ import annotations

from oneops.router.conditions import evaluate as evaluate_condition
from oneops.router.conditions import survives_filter
from oneops.router.decompose import (
    Decomposer,
    LlmDecomposer,
    PassthroughDecomposer,
    SubQuery,
)
from oneops.router.disambiguation import (
    Disambiguation,
    Disambiguator,
    LlmDisambiguator,
    ThresholdDisambiguator,
)
from oneops.router.entity_id import (
    EntityIdNormalizer,
    ExtractionResult,
    NormalizationResult,
    NormalizedEntity,
)
from oneops.router.glossary import Glossary
from oneops.router.plan import (
    PlanStep,
    RouteOutcome,
    RoutePlan,
    RouteResult,
    SubQueryRoute,
    assemble_plan,
)
from oneops.router.retrieval import (
    Candidate,
    CandidateRetriever,
    GatewayEmbedder,
    LexicalRetriever,
    PgVectorRetriever,
)
from oneops.router.rewrite import (
    ConversationTurn,
    LlmRewriter,
    PassthroughRewriter,
    Rewriter,
    RewriteResult,
)
from oneops.router.router import DEFAULT_TOP_K, Router
from oneops.router.signals import RequestSignals, Ternary, with_intents

__all__ = [
    "Router",
    "DEFAULT_TOP_K",
    "RouteResult",
    "RouteOutcome",
    "RoutePlan",
    "PlanStep",
    "SubQueryRoute",
    "assemble_plan",
    "Glossary",
    "RequestSignals",
    "Ternary",
    "with_intents",
    "evaluate_condition",
    "survives_filter",
    "Candidate",
    "CandidateRetriever",
    "GatewayEmbedder",
    "LexicalRetriever",
    "PgVectorRetriever",
    "Disambiguation",
    "Disambiguator",
    "ThresholdDisambiguator",
    "LlmDisambiguator",
    "Decomposer",
    "PassthroughDecomposer",
    "LlmDecomposer",
    "SubQuery",
    "Rewriter",
    "PassthroughRewriter",
    "LlmRewriter",
    "RewriteResult",
    "ConversationTurn",
    "EntityIdNormalizer",
    "NormalizedEntity",
    "NormalizationResult",
    "ExtractionResult",
]
