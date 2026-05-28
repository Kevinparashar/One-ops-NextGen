"""LLM Gateway value objects (P8).

`LlmRequest` / `LlmResponse` are the gateway's contract. Every model call in
the system is one `LlmRequest` through `LlmGateway.call` — there is no other
egress. `TransportResult` is what a provider transport returns; the gateway
turns it into an `LlmResponse` after cost accounting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ResponseFormat(str, Enum):
    TEXT = "text"
    JSON = "json"            # provider must return strict JSON (structured output)


@dataclass(frozen=True)
class LlmMessage:
    """One conversation message.

    `cache_control=True` is a hint to the transport: this message's content
    is large and stable; emit it in the provider's prompt-cache shape
    (Anthropic `cache_control: {type: "ephemeral"}`). The provider caches
    the tokens after the first call; subsequent identical calls return
    `cache_read_input_tokens` and skip re-tokenisation — typically ~50-90%
    of the input-token cost on a hot prefix.

    Transports that do not support prompt caching (older OpenAI endpoints,
    where OpenAI's automatic prefix caching applies server-side to prompts
    ≥1024 tokens) ignore the flag — the call still succeeds."""

    role: str                # system | user | assistant
    content: str
    cache_control: bool = False     # provider-side ephemeral cache hint


@dataclass(frozen=True)
class LlmRequest:
    """One model call.

    `tenant_id` is mandatory — cost and quota are per-tenant.

    `user_id` is optional but should be populated whenever the call
    originates from a user turn (versus a system task). It rides the
    `llm.call` span as `oneops.user_id` so per-user spend can be
    rolled up from trace storage (Tempo / a billing DB) for abuse
    detection and per-seat attribution. Metrics deliberately do NOT
    label by user — that would blow up Prometheus cardinality; user
    accountability lives in traces only.
    """

    messages: tuple[LlmMessage, ...]
    model: str
    tenant_id: str
    user_id: str = ""
    temperature: float = 0.0
    max_tokens: int = 1024
    response_format: ResponseFormat = ResponseFormat.TEXT
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("LlmRequest.tenant_id is mandatory (cost/quota are per-tenant)")
        if not self.messages:
            raise ValueError("LlmRequest.messages must be non-empty")


@dataclass(frozen=True)
class TransportResult:
    """What a provider transport returns — before cost accounting.

    `cache_read_input_tokens` is the count of input tokens the provider
    served from its prompt cache (Anthropic / OpenAI). It is a SUBSET of
    `prompt_tokens` — the gateway uses it to compute cached-discount cost.
    `cache_creation_input_tokens` is the count of tokens the provider
    wrote into its cache on THIS call (one-time cost, future hits free)."""

    content: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"
    actual_model: str = ""           # the model that actually served (fallback-aware)
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class LlmResponse:
    """The gateway's output — a transport result plus accounting."""

    content: str
    model: str                       # the model that served the call
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    finish_reason: str = "stop"
    redacted_pii: tuple[str, ...] = ()      # PII classes scrubbed from the prompt
    fell_back: bool = False                  # True if the primary model failed over
    # Provider-side prompt-cache counters. `cache_read_input_tokens` is the
    # share of `prompt_tokens` the provider served from cache (cheaper);
    # `cache_creation_input_tokens` is the share written to cache this call
    # (one-time write cost, future hits free).
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def prompt_cache_hit_ratio(self) -> float:
        """Share of input tokens served from prompt cache. 0.0 when no cache
        info or no input tokens; 1.0 when every input token was a cache hit.
        Surfaces in dashboards to see cache health."""
        if self.prompt_tokens <= 0:
            return 0.0
        return min(1.0, self.cache_read_input_tokens / self.prompt_tokens)


__all__ = [
    "ResponseFormat",
    "LlmMessage",
    "LlmRequest",
    "TransportResult",
    "LlmResponse",
]
