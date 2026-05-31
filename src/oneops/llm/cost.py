"""Cost accounting — per tenant, per model.

"Without it, one customer can bankrupt you" (the brief). Every call through
the gateway records token usage and computes a USD cost; `CostTracker` keeps
running per-(tenant, model) totals and emits OTel counters so spend is
visible per tenant before the bill arrives.

The price table is pricing *data*, not a routing list — a small config map of
USD-per-million-tokens. Unknown models fall back to a conservative default.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from oneops.observability import get_logger, increment

_log = get_logger("oneops.llm.cost")

# USD per 1,000,000 tokens — (input, output). Pricing data, kept here as config.
_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-3-small": (0.02, 0.0),
}
# Conservative default for an unrecognised model — never silently free.
_DEFAULT_PRICING: tuple[float, float] = (1.0, 3.0)


def price_for(model: str) -> tuple[float, float]:
    return _PRICING.get(model, _DEFAULT_PRICING)


def compute_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    in_price, out_price = price_for(model)
    return (prompt_tokens / 1_000_000) * in_price + \
           (completion_tokens / 1_000_000) * out_price


@dataclass
class _Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class CostTracker:
    """Per-(tenant, model) running token + cost totals. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._usage: dict[tuple[str, str], _Usage] = {}

    def record(
        self, tenant_id: str, model: str,
        prompt_tokens: int, completion_tokens: int,
    ) -> float:
        """Record one call's usage; return its USD cost."""
        cost = compute_cost(model, prompt_tokens, completion_tokens)
        with self._lock:
            u = self._usage.setdefault((tenant_id, model), _Usage())
            u.calls += 1
            u.prompt_tokens += prompt_tokens
            u.completion_tokens += completion_tokens
            u.cost_usd += cost
        # OTel — spend visible per tenant per model.
        increment("ai.llm.cost_usd_micros", value=int(cost * 1_000_000),
                  tenant_id=tenant_id, model=model)
        increment("ai.llm.tokens.total", value=prompt_tokens + completion_tokens,
                  tenant_id=tenant_id, model=model)
        return cost

    def usage(self, tenant_id: str) -> dict[str, dict[str, float]]:
        """Per-model usage for one tenant."""
        with self._lock:
            return {
                model: {
                    "calls": u.calls,
                    "prompt_tokens": u.prompt_tokens,
                    "completion_tokens": u.completion_tokens,
                    "cost_usd": round(u.cost_usd, 6),
                }
                for (tid, model), u in self._usage.items() if tid == tenant_id
            }

    def total_cost(self, tenant_id: str) -> float:
        with self._lock:
            return round(sum(u.cost_usd for (tid, _), u in self._usage.items()
                             if tid == tenant_id), 6)


__all__ = ["CostTracker", "compute_cost", "price_for"]
