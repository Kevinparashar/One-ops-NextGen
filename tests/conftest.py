"""Pytest fixtures shared across unit + integration suites.

Concurrency: pytest-asyncio runs each test in a fresh event loop by default.
Singletons (LLMGateway, Redis client) are torn down per-session to avoid leakage.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load .env BEFORE any test reads os.environ. pydantic-settings already does this
# for Settings, but raw os.getenv() in service-reachability prechecks does not.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env", override=False)

# Force test-friendly defaults BEFORE any oneops imports load Settings.
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("LLM_GATEWAY_URL", "http://localhost:4000")
os.environ.setdefault("LLM_GATEWAY_API_KEY", "test-key")
os.environ.setdefault("DRAGONFLY_URL", "redis://localhost:6379/0")
# Disable OTLP span export in tests — avoids retry noise when Tempo isn't running.
# Tests still produce spans (for in-process assertions); they just never leave the process.
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""
os.environ.setdefault("OTEL_TRACES_SAMPLER_ARG", "0.0")


@pytest.fixture(scope="session", autouse=True)
def _init_observability() -> None:
    """Initialize structlog + OTEL once per session. No-op span exporter in tests."""
    from oneops.observability import setup_observability
    setup_observability()


def has_service(host: str, port: int, timeout: float = 0.5) -> bool:
    """Check if a TCP service is reachable. Used to skip integration tests when stack is down."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False
