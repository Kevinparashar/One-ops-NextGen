"""LlmGateway — the single egress for every model call (P8).

Every model call in the system is `LlmGateway.call`. There is no other path
to a provider — `test_no_direct_provider` is the CI gate that enforces it.

`call` does, in order:
  1. **Quota** — per-tenant budget check; refuse once spent.
  2. **Redaction** — scrub structural PII from the prompt before it leaves.
  3. **Transport + retry + fallback** — call the model; retry transient
     failures; fail over to `fallback_model` if the primary keeps failing.
  4. **Cost accounting** — record per-tenant per-model token spend.
  5. Return an `LlmResponse` carrying the content, usage, cost, and what was
     redacted / whether it fell back.

`embed` is the same egress discipline for embeddings.
"""
from __future__ import annotations

import time
from dataclasses import replace

from oneops.errors import (
    LLMGatewayError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUpstreamError,
)
from oneops.llm.cost import CostTracker
from oneops.llm.models import LlmRequest, LlmResponse
from oneops.llm.quota import QuotaGuard
from oneops.llm.redaction import redact_messages
from oneops.llm.transport import LlmTransport
from oneops.observability import (
    get_logger,
    get_tracer,
    histogram,
    increment,
    set_langfuse_generation,
)

_log = get_logger("oneops.llm.gateway")
_tracer = get_tracer("oneops.llm.gateway")

# Errors that are worth retrying / failing over on.
_TRANSIENT = (LLMTimeoutError, LLMRateLimitError, LLMUpstreamError)


class LlmGateway:
    """The single model egress."""

    def __init__(
        self,
        transport: LlmTransport,
        *,
        cost_tracker: CostTracker | None = None,
        quota_guard: QuotaGuard | None = None,
        redact: bool = True,
        max_retries: int = 2,
        fallback_model: str | None = None,
    ) -> None:
        self._transport = transport
        self._cost = cost_tracker or CostTracker()
        self._quota = quota_guard
        self._redact = redact
        self._max_retries = max_retries
        self._fallback_model = fallback_model

    @property
    def cost(self) -> CostTracker:
        return self._cost

    async def call(self, request: LlmRequest) -> LlmResponse:
        """Run one model call through the full egress discipline."""
        _call_t0 = time.monotonic()
        with _tracer.start_as_current_span(
            "llm.call",
            attributes={"oneops.tenant_id": request.tenant_id,
                        "oneops.user_id": request.user_id,
                        "llm.model": request.model},
        ) as span:
            # 1. Quota.
            if self._quota is not None:
                self._quota.check_and_charge(request.tenant_id)

            # 2. Redaction — scrub PII before the prompt leaves.
            redacted_pii: tuple[str, ...] = ()
            outbound = request
            if self._redact:
                messages, found = redact_messages(request.messages)
                redacted_pii = tuple(sorted(found))
                outbound = replace(request, messages=messages)
            span.set_attribute("llm.redacted_pii", len(redacted_pii))

            # 3. Transport with retry + fallback.
            t0 = time.monotonic()
            result, fell_back = await self._send(outbound)
            latency_ms = int((time.monotonic() - t0) * 1000)

            served_model = result.actual_model or outbound.model

            # 4. Cost accounting.
            cost = self._cost.record(
                request.tenant_id, served_model,
                result.prompt_tokens, result.completion_tokens)
            span.set_attribute("llm.cost_usd", cost)
            span.set_attribute("llm.total_tokens",
                               result.prompt_tokens + result.completion_tokens)
            span.set_attribute("llm.fell_back", fell_back)
            # Prompt-cache observability: surface cache-token counts so the
            # dashboard can prove the prefix is being cached. A high
            # `cache_read_input_tokens` share of `prompt_tokens` is the
            # signal that prompt caching is paying back.
            if result.cache_read_input_tokens > 0:
                span.set_attribute(
                    "llm.cache_read_input_tokens",
                    result.cache_read_input_tokens)
            if result.cache_creation_input_tokens > 0:
                span.set_attribute(
                    "llm.cache_creation_input_tokens",
                    result.cache_creation_input_tokens)

            # Langfuse: mark this span a "generation" so the prompt/response,
            # model, tokens, and cost render in the trace tree. Model/tokens/cost
            # always; prompt/completion only under LANGFUSE_CAPTURE_CONTENT,
            # dual-layer redacted. `outbound.messages` is already PII-scrubbed
            # (step 2) — redact_for_span re-applies redaction defensively.
            set_langfuse_generation(
                span, model=served_model,
                prompt=[{"role": m.role, "content": m.content}
                        for m in outbound.messages],
                completion=result.content,
                input_tokens=result.prompt_tokens,
                output_tokens=result.completion_tokens,
                cost_usd=cost)

            # Latency histogram for p99 alerting on LLM duration.
            # Paired with the `llm.call` span — same labels for Tempo↔
            # Prometheus correlation. Histogram bucket bounds default to
            # OTel SDK defaults (good for sub-1s → 30s range).
            histogram("ai.llm.call.duration_ms", float(latency_ms),
                      model=served_model, fell_back=str(fell_back).lower())
            return LlmResponse(
                content=result.content, model=served_model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                cost_usd=cost, latency_ms=latency_ms,
                finish_reason=result.finish_reason,
                redacted_pii=redacted_pii, fell_back=fell_back,
                cache_read_input_tokens=result.cache_read_input_tokens,
                cache_creation_input_tokens=result.cache_creation_input_tokens)

    async def _send(self, request: LlmRequest):
        """Transport call with retry on transient errors, then one fail-over to
        `fallback_model`. Raises the last transient error if all attempts fail."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._transport.complete(request), False
            except _TRANSIENT as exc:
                last_exc = exc
                _log.warning("llm.transport_retry", attempt=attempt,
                             model=request.model, error=str(exc))
                # Counter for transient retries (timeout, 429, 5xx).
                # Distinct from `_errors_total` — a retry isn't a failure
                # yet; the operator wants to see retry pressure as an
                # early warning signal before exhaustion.
                increment("ai.llm.call.retries.total",
                          model=request.model,
                          reason=type(exc).__name__)

        if self._fallback_model and self._fallback_model != request.model:
            _log.warning("llm.fallback", primary=request.model,
                         fallback=self._fallback_model)
            increment("ai.llm.call.fallbacks.total",
                      primary=request.model, fallback=self._fallback_model)
            fallback_req = replace(request, model=self._fallback_model)
            try:
                return await self._transport.complete(fallback_req), True
            except _TRANSIENT as exc:
                last_exc = exc

        # The gateway presents ONE failure type to callers — LLMGatewayError —
        # so a consumer has a single thing to catch (the underlying upstream
        # error is the `cause`). QuotaExceededError is also an LLMGatewayError.
        # Counter for the terminal failure — drives the
        # `LLMP99Spike` / future `LLMErrorRate` alerts.
        increment("ai.llm.call.errors.total",
                  model=request.model,
                  reason=type(last_exc).__name__ if last_exc else "unknown")
        raise LLMGatewayError(
            f"LLM call failed after {self._max_retries + 1} attempt(s)"
            + (" and a fallback" if self._fallback_model else ""),
            cause=last_exc)

    async def embed(
        self, texts: list[str], *, model: str, tenant_id: str,
        user_id: str = "", dimensions: int | None = None,
    ) -> list[list[float]]:
        """Embed texts through the same egress (no separate provider path).

        `user_id` is optional but should be passed for user-originated
        embeddings (search-time queries) so per-user spend is traceable.
        Batch / system embeddings (write-time index builds) can leave it
        empty."""
        if not tenant_id:
            raise ValueError("embed requires a tenant_id")
        with _tracer.start_as_current_span(
            "llm.embed",
            attributes={"oneops.tenant_id": tenant_id,
                        "oneops.user_id": user_id,
                        "llm.model": model,
                        "llm.embed_count": len(texts)},
        ):
            if self._quota is not None:
                self._quota.check_and_charge(tenant_id)
            vectors = await self._transport.embed(
                texts, model=model, dimensions=dimensions)
            # Embedding cost is input-token only.
            total_tokens = sum(max(1, len(t) // 4) for t in texts)
            self._cost.record(tenant_id, model, total_tokens, 0)
            return vectors


__all__ = ["LlmGateway"]
