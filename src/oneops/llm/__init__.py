"""LLM Gateway layer (P8) — the single egress for every model call.

Every completion and embedding goes through `LlmGateway`: per-tenant quota,
PII redaction, retry + fallback, and per-tenant per-model cost accounting. No
other module talks to a provider — the CI gate `test_no_direct_provider`
enforces it.

Public surface:
    from oneops.llm import LlmGateway, LlmRequest, LlmResponse, LlmMessage
    from oneops.llm import EchoTransport, LiteLLMTransport
    from oneops.llm import CostTracker, QuotaGuard
"""
from __future__ import annotations

from oneops.llm.cost import CostTracker, compute_cost, price_for
from oneops.llm.gateway import LlmGateway
from oneops.llm.models import (
    LlmMessage,
    LlmRequest,
    LlmResponse,
    ResponseFormat,
    TransportResult,
)
from oneops.llm.quota import QuotaGuard
from oneops.llm.redaction import redact_messages, redact_text
from oneops.llm.transport import EchoTransport, LiteLLMTransport, LlmTransport

__all__ = [
    "LlmGateway",
    "LlmRequest",
    "LlmResponse",
    "LlmMessage",
    "ResponseFormat",
    "TransportResult",
    "LlmTransport",
    "EchoTransport",
    "LiteLLMTransport",
    "CostTracker",
    "compute_cost",
    "price_for",
    "QuotaGuard",
    "redact_messages",
    "redact_text",
]
