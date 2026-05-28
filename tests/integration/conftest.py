"""Integration suite fixtures + per-test budget + LLM-gateway helper.

Integration tests run real LLM calls, real Supabase queries, real Dragonfly,
and real NATS. A single multi-turn scenario can issue 4–16 outbound LLM
calls (planner + classifier + summarizer + verifier per turn × N turns) at
~1.5–3s TTFT each plus Supabase round-trips. The unit-suite default of 60s
is correct for unit tests but unrealistic here.

We override the default to 240s for every integration test via the
`pytest_collection_modifyitems` hook — markers added by autouse fixtures
do not take effect because pytest-timeout reads markers during item
collection, BEFORE fixtures run.

Individual tests that need a tighter budget can still override via their
own `@pytest.mark.timeout(N)` — collection iterates markers and only adds
the default when none is present.
"""
from __future__ import annotations

import os

import pytest

# Default per-test wall-clock budget for the integration suite (seconds).
# Tuned for: 4 turns × ~4 LLM calls/turn × 3s TTFT plus Supabase + retries.
INTEGRATION_TEST_TIMEOUT_S = 240


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Apply the integration default timeout to every collected test.

    Skip items that already declare their own timeout marker — those win.
    """
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(INTEGRATION_TEST_TIMEOUT_S))


# ── Shared LLM-gateway routing helper ───────────────────────────────
#
# Integration tests exercise OneOps logic against a real LLM. They are NOT
# tests of LiteLLM's proxy layer. The original (broken) helper checked only
# whether the LiteLLM port was open and skipped redirection when it was —
# missing the case where the proxy is up but auth-broken or model-unmapped,
# which manifests as hung 60s-retry-storm test runs.
#
# Production-grade rule: when OPENAI_API_KEY is set, route every integration
# test at OpenAI directly. One fewer hop, deterministic auth, no proxy drift.
# When the key is absent, tests are expected to be skipped by their own
# `_llm_reachable()` / `pytestmark = ...skipif` guards.

def ensure_real_llm_gateway() -> None:
    """Point LLM_GATEWAY_URL at OpenAI when OPENAI_API_KEY is configured.

    Idempotent — repeated calls are no-ops once the override is in place.
    Clears the cached Settings + gateway singleton so the override actually
    takes effect for any subsequent gateway construction in this process.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return
    if os.environ.get("LLM_GATEWAY_URL") == "https://api.openai.com/v1":
        return  # Already routed; nothing to do.

    os.environ["LLM_GATEWAY_URL"] = "https://api.openai.com/v1"
    os.environ["LLM_GATEWAY_API_KEY"] = api_key
    from oneops.config import get_settings
    get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _route_llm_gateway_to_openai() -> None:
    """At integration-suite startup, route every test at OpenAI directly.

    Runs once per pytest session. Tests that explicitly call
    `ensure_real_llm_gateway()` later are still safe (idempotent).
    Without this fixture, tests that don't call the helper inherit the
    project default (`LLM_GATEWAY_URL=http://localhost:4000` from .env),
    which is the broken LiteLLM proxy in the local stack.
    """
    ensure_real_llm_gateway()
