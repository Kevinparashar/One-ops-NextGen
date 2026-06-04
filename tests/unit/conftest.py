"""Unit-suite test isolation + marker policy.

Two cross-cutting fixes (see docs/production-readiness-audit.md P0-1):

1. **Marker policy** — auto-mark every test under ``tests/unit/`` as ``unit``
   unless it explicitly opts into a heavier lane (``integration`` / ``slow`` /
   ``stress``). Previously only ~19 tests were hand-marked, so ``pytest -m unit``
   (the CI gate's unit stage) validated ~1% of the suite. With this hook,
   ``-m unit`` selects the whole real unit suite and ``-m "not integration"``
   cleanly excludes the heavier tests.

2. **Settings isolation** — clear the process-wide ``Settings`` ``lru_cache``
   around every test. ``get_settings()`` is ``@lru_cache``-d; a test that reads
   it under ``monkeypatch``-ed env poisons the cache for every later test
   (``monkeypatch`` reverts the env var, but not the cache). That cross-test
   leak made downstream tests build an app with another test's settings. Clearing
   the cache before and after each test makes settings reflect each test's own
   environment — no behavior change to product code, test-isolation only.
"""
from __future__ import annotations

import pytest

from oneops.config import get_settings

_HEAVIER_LANES = frozenset({"integration", "slow", "stress"})


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Tag every unit-dir test ``unit`` unless it opted into a heavier lane."""
    for item in items:
        if _HEAVIER_LANES.isdisjoint(item.keywords):
            item.add_marker(pytest.mark.unit)


@pytest.fixture(autouse=True)
def _isolate_settings_cache():
    """Reset the cached Settings singleton around each test (see module docstring)."""
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
