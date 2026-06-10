"""Step 0 — UC-8 approval feature flag (`UC08_APPROVAL_ENABLED`).

Verifies the kill-switch defaults OFF, parses truthy/falsy aliases, and fails
safe (→ False) on garbage. With the flag off the whole approval feature is inert,
so this is the single guard the rest of Phase 1 hangs off.
"""
from __future__ import annotations

import pytest

from oneops.use_cases.uc08_fulfillment.approval import approval_enabled

_FLAG = "UC08_APPROVAL_ENABLED"


def test_default_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env → feature OFF (the live flow stays unchanged)."""
    monkeypatch.delenv(_FLAG, raising=False)
    assert approval_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "Yes", "on", " t ", "y"])
def test_truthy_aliases_enable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_FLAG, raw)
    assert approval_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " f ", "n"])
def test_falsy_aliases_disable(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(_FLAG, raw)
    assert approval_enabled() is False


# ── Devil's play: garbage / empty must fail SAFE (→ False), never raise ──
@pytest.mark.parametrize("raw", ["", "  ", "maybe", "enabled", "2", "yess", "✓"])
def test_garbage_falls_back_to_off(
    monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    monkeypatch.setenv(_FLAG, raw)
    assert approval_enabled() is False


def test_flip_is_read_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read on every call → can be flipped at runtime (no import caching)."""
    monkeypatch.delenv(_FLAG, raising=False)
    assert approval_enabled() is False
    monkeypatch.setenv(_FLAG, "true")
    assert approval_enabled() is True
    monkeypatch.setenv(_FLAG, "false")
    assert approval_enabled() is False
