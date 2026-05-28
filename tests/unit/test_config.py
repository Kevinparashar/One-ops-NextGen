"""Smoke tests for config loading. No external deps."""
from __future__ import annotations

import pytest

from oneops.config import Settings, get_settings


@pytest.mark.unit
def test_settings_loads_defaults() -> None:
    settings = get_settings()
    assert isinstance(settings, Settings)
    assert settings.service_name
    assert settings.llm_default_model
    assert settings.dragonfly_url.startswith("redis://")
    assert settings.environment in {"local", "dev", "staging", "prod"}


@pytest.mark.unit
def test_settings_is_singleton() -> None:
    assert get_settings() is get_settings()


@pytest.mark.unit
def test_api_key_is_redacted_in_repr() -> None:
    """SecretStr ensures the API key never shows up in logs / tracebacks."""
    settings = get_settings()
    assert "test-key" not in repr(settings)
    assert "*" in repr(settings.llm_gateway_api_key) or "Secret" in repr(settings.llm_gateway_api_key)
