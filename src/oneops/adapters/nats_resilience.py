"""NATS resilience — retry policy + circuit breaker for request/reply.

Production discipline:

  * **Bounded retries** — a transient `NATSUnavailableError` retries
    up to N times with exponential backoff + jitter. Idempotent only;
    callers that publish non-idempotent work must opt out by setting
    `max_attempts=1`.
  * **Per-subject circuit breaker** — track recent failure rate per
    NATS subject. When the rate crosses a threshold inside a rolling
    window, OPEN the breaker for a cooldown period. Subsequent calls
    fail fast — `NATSUnavailableError` raised without hitting NATS.
    After cooldown the breaker goes HALF_OPEN: one probe attempt; on
    success the breaker CLOSES, on failure it re-OPENS.
  * **Observable** — every retry attempt, every breaker transition,
    every fail-fast carries an OTel attribute + structured log line.
    Operators see exactly why a turn was rejected.
  * **No silent fallback** — on terminal failure (retries exhausted
    or breaker open) the call raises `NATSUnavailableError`. The
    ingress maps that to a typed degradation response, never an HTTP
    500. See `nats_invoker` for the ingress mapping.

The module deliberately has NO external dependencies (no `tenacity`,
no `pybreaker`) — circuit-breaker logic is ~100 lines and tuning
knobs live in env vars so operators can shape them without code
edits. The cost of a third-party dep at the substrate layer is
permanent supply-chain risk; the savings is dozens of lines.

Env knobs:
  NATS_RETRY_MAX_ATTEMPTS      (default 3)
  NATS_RETRY_INITIAL_DELAY_MS  (default 200)
  NATS_RETRY_MAX_DELAY_MS      (default 2000)
  NATS_BREAKER_FAILURE_RATIO   (default 0.5)
  NATS_BREAKER_MIN_REQUESTS    (default 5)
  NATS_BREAKER_WINDOW_SECONDS  (default 30)
  NATS_BREAKER_COOLDOWN_SECONDS (default 15)
"""
from __future__ import annotations

import asyncio
import os
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from oneops.errors import NATSUnavailableError
from oneops.observability import get_logger, get_tracer, increment

_log = get_logger("oneops.adapters.nats_resilience")
_tracer = get_tracer("oneops.adapters.nats_resilience")


# ── Retry policy ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff with full jitter. Bounded attempts."""

    max_attempts: int = 3
    initial_delay_s: float = 0.2
    max_delay_s: float = 2.0

    @classmethod
    def from_env(cls) -> RetryPolicy:
        return cls(
            max_attempts=int(os.getenv("NATS_RETRY_MAX_ATTEMPTS", "3")),
            initial_delay_s=int(os.getenv("NATS_RETRY_INITIAL_DELAY_MS", "200")) / 1000.0,
            max_delay_s=int(os.getenv("NATS_RETRY_MAX_DELAY_MS", "2000")) / 1000.0,
        )

    def delay_for(self, attempt_index: int) -> float:
        """`attempt_index` is 0 for the FIRST retry (after the initial
        failed attempt). Returns full-jitter backoff capped at max."""
        base = self.initial_delay_s * (2 ** attempt_index)
        capped = min(base, self.max_delay_s)
        # Full jitter prevents thundering herd on simultaneous retries.
        return random.uniform(0.0, capped)


# ── Circuit breaker ─────────────────────────────────────────────────────


class _State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Window:
    """Rolling outcome window for one breaker. Append-only; trims by age."""

    seconds: float
    events: deque = field(default_factory=deque)  # (ts, success_bool)

    def record(self, success: bool) -> None:
        now = time.monotonic()
        self.events.append((now, success))
        cutoff = now - self.seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def stats(self) -> tuple[int, int]:
        """Returns (total, failures) in the current window."""
        now = time.monotonic()
        cutoff = now - self.seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()
        total = len(self.events)
        failures = sum(1 for _, ok in self.events if not ok)
        return total, failures


@dataclass
class _BreakerState:
    state: _State = _State.CLOSED
    opened_at: float = 0.0
    window: _Window | None = None


@dataclass(frozen=True)
class CircuitBreakerConfig:
    failure_ratio: float = 0.5
    min_requests: int = 5
    window_seconds: float = 30.0
    cooldown_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> CircuitBreakerConfig:
        return cls(
            failure_ratio=float(os.getenv("NATS_BREAKER_FAILURE_RATIO", "0.5")),
            min_requests=int(os.getenv("NATS_BREAKER_MIN_REQUESTS", "5")),
            window_seconds=float(os.getenv("NATS_BREAKER_WINDOW_SECONDS", "30")),
            cooldown_seconds=float(os.getenv("NATS_BREAKER_COOLDOWN_SECONDS", "15")),
        )


class CircuitBreaker:
    """Per-subject breaker. Thread-safe via `asyncio.Lock` (the call sites
    are async). Process-local state — fine for a single-process service,
    fine for the split topology too because each replica gates its own
    requests (NATS itself handles load balancing across replicas)."""

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self._cfg = config or CircuitBreakerConfig.from_env()
        self._states: dict[str, _BreakerState] = {}
        self._lock = asyncio.Lock()

    async def before_call(self, subject: str) -> None:
        """Raises NATSUnavailableError when the breaker is OPEN and still
        within cooldown. When OPEN and the cooldown has elapsed, transitions
        to HALF_OPEN and lets ONE probe through."""
        async with self._lock:
            st = self._states.setdefault(
                subject,
                _BreakerState(window=_Window(seconds=self._cfg.window_seconds)))
            if st.state == _State.OPEN:
                if (time.monotonic() - st.opened_at) >= self._cfg.cooldown_seconds:
                    st.state = _State.HALF_OPEN
                    _log.info("nats_breaker.half_open", subject=subject)
                    increment("nats.breaker.half_open.total", subject=subject)
                else:
                    remaining = self._cfg.cooldown_seconds - (time.monotonic() - st.opened_at)
                    raise NATSUnavailableError(
                        f"circuit breaker OPEN for subject {subject!r}; "
                        f"cooldown remaining {remaining:.1f}s")

    async def record(self, subject: str, success: bool) -> None:
        async with self._lock:
            st = self._states.setdefault(
                subject,
                _BreakerState(window=_Window(seconds=self._cfg.window_seconds)))
            assert st.window is not None
            st.window.record(success)

            if st.state == _State.HALF_OPEN:
                # Probe outcome decides the breaker's next state.
                if success:
                    st.state = _State.CLOSED
                    _log.info("nats_breaker.closed", subject=subject)
                    increment("nats.breaker.closed.total", subject=subject)
                else:
                    st.state = _State.OPEN
                    st.opened_at = time.monotonic()
                    _log.warning("nats_breaker.reopened", subject=subject)
                    increment("nats.breaker.opened.total",
                              subject=subject, reason="probe_failed")
                return

            if st.state == _State.CLOSED:
                total, failures = st.window.stats()
                if (total >= self._cfg.min_requests
                        and failures / total >= self._cfg.failure_ratio):
                    st.state = _State.OPEN
                    st.opened_at = time.monotonic()
                    _log.warning(
                        "nats_breaker.opened",
                        subject=subject,
                        total=total, failures=failures,
                        failure_ratio=round(failures / total, 3),
                        cooldown_s=self._cfg.cooldown_seconds,
                    )
                    # Counter that the SLO alert `CircuitBreakerOpened`
                    # reads via `increase(nats_breaker_opened_total[5m]) > 0`.
                    # Without it the alert can never fire — devil's-play gap.
                    increment("nats.breaker.opened.total",
                              subject=subject, reason="failure_ratio")


# Process-wide singleton — one breaker map shared by every NATS use site.
_breaker_singleton: CircuitBreaker | None = None


def get_circuit_breaker() -> CircuitBreaker:
    global _breaker_singleton
    if _breaker_singleton is None:
        _breaker_singleton = CircuitBreaker()
    return _breaker_singleton


# ── Resilient call wrapper ──────────────────────────────────────────────


async def resilient_call(
    fn: Callable[[], Awaitable[Any]],
    *,
    subject: str,
    tenant_id: str = "",
    retry_policy: RetryPolicy | None = None,
    breaker: CircuitBreaker | None = None,
) -> Any:
    """Run `fn` with retry + circuit-breaker discipline. `fn` is an async
    no-arg callable that performs ONE NATS request and returns the reply
    bytes (or whatever the caller expects).

    Multi-tenant SaaS keying: the breaker is keyed by
    `(subject, tenant_id)` so a noisy tenant cannot trip the breaker
    for every other tenant on the same subject. When `tenant_id` is
    empty (rare; only the deploy-time health probes), the breaker
    falls back to subject-only keying.

    Failure model:
      * `NATSUnavailableError` raised by `fn` → retry up to policy.max_attempts.
      * Any other exception → DO NOT retry (likely a logic bug or
        non-retryable upstream error); record as a failure for the breaker
        and re-raise.
      * Breaker OPEN → fail fast with `NATSUnavailableError`.
    """
    policy = retry_policy or RetryPolicy.from_env()
    cb = breaker or get_circuit_breaker()
    # Per-(subject, tenant) breaker key so a misbehaving tenant only
    # trips ITS slice of the bus, never the whole subject.
    breaker_key = f"{subject}|{tenant_id}" if tenant_id else subject

    with _tracer.start_as_current_span(
        "nats.resilient_call",
        attributes={
            "messaging.system": "nats",
            "messaging.destination.name": subject,
            "oneops.tenant_id": tenant_id,
            "oneops.retry.max_attempts": policy.max_attempts,
        },
    ) as span:
        last_exc: Exception | None = None
        for attempt in range(policy.max_attempts):
            try:
                await cb.before_call(breaker_key)
            except NATSUnavailableError as exc:
                # Fail-fast path. Recorded as a breaker miss but NOT
                # retried — pointless to retry while breaker is open.
                span.set_attribute("oneops.retry.skipped_breaker_open", True)
                _log.warning("nats_resilient.fail_fast_breaker_open",
                             subject=subject, error=str(exc)[:200])
                raise

            # Fast-fail on a known-disconnected NATS client. The
            # underlying nats-py client buffers requests during a
            # reconnect attempt rather than raising — without this
            # check, every attempt would block for the full per-call
            # timeout while NATS is down. We probe the connection state
            # and turn it into an immediate `NATSUnavailableError` so
            # the retry policy + breaker can do their job.
            try:
                from oneops.adapters.nats_client import get_nats_client
                _client = await get_nats_client()
                if not _client.is_connected:
                    raise NATSUnavailableError(
                        f"NATS client is disconnected (subject {subject!r})")
            except NATSUnavailableError as exc:
                last_exc = exc
                await cb.record(breaker_key, success=False)
                if attempt < policy.max_attempts - 1:
                    delay = policy.delay_for(attempt)
                    _log.info(
                        "nats_resilient.retry",
                        subject=subject, attempt=attempt + 1,
                        max_attempts=policy.max_attempts,
                        delay_s=round(delay, 3),
                        reason="client_disconnected",
                    )
                    await asyncio.sleep(delay)
                    continue
                span.set_attribute("oneops.retry.exhausted", True)
                raise

            try:
                result = await fn()
            except NATSUnavailableError as exc:
                last_exc = exc
                await cb.record(breaker_key, success=False)
                if attempt < policy.max_attempts - 1:
                    delay = policy.delay_for(attempt)
                    span.set_attribute(f"oneops.retry.attempt_{attempt}_delay_s",
                                       round(delay, 3))
                    _log.info(
                        "nats_resilient.retry",
                        subject=subject, attempt=attempt + 1,
                        max_attempts=policy.max_attempts,
                        delay_s=round(delay, 3), reason=str(exc)[:200],
                    )
                    await asyncio.sleep(delay)
                    continue
                # Final attempt failed.
                span.set_attribute("oneops.retry.exhausted", True)
                _log.warning(
                    "nats_resilient.exhausted",
                    subject=subject, attempts=policy.max_attempts,
                    error=str(exc)[:200],
                )
                raise
            except Exception:
                # Non-NATS exception → record as failure, don't retry.
                await cb.record(breaker_key, success=False)
                raise
            else:
                await cb.record(breaker_key, success=True)
                if attempt > 0:
                    span.set_attribute("oneops.retry.recovered_at_attempt",
                                       attempt + 1)
                return result
        # Defensive — loop above always returns or raises.
        if last_exc is not None:
            raise last_exc
        raise NATSUnavailableError(
            f"resilient_call exited with no result for {subject!r}")


__all__ = [
    "RetryPolicy",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "get_circuit_breaker",
    "resilient_call",
]
