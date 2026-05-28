"""CI gate — no module bypasses the LLM Gateway.

The brief: "no direct provider SDK calls anywhere else". Every model call goes
through `oneops.llm.LlmGateway`. This test scans the new-architecture packages
and fails if any of them imports a provider SDK (`openai`, `anthropic`,
`cohere`, ...) directly. The gateway itself reaches a provider only via the
LiteLLM proxy over HTTP — not the SDK — so even `oneops/llm/` must be clean.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# The packages of the new architecture (P1-P8). As later phases add packages,
# extend this list — the gate must cover every new-system module.
_NEW_PACKAGES = [
    "registry", "codec", "session", "authz", "router", "executor",
    "toolrunner", "llm",
]

# Provider SDKs that must never be imported outside the gateway's transport.
_FORBIDDEN = re.compile(
    r"^\s*(?:import|from)\s+(openai|anthropic|cohere|google\.generativeai|litellm)\b",
    re.MULTILINE,
)

_SRC = Path(__file__).resolve().parents[3] / "src" / "oneops"


def _scan(package: str) -> list[str]:
    offenders: list[str] = []
    pkg_dir = _SRC / package
    for py in pkg_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for match in _FORBIDDEN.finditer(text):
            offenders.append(f"{py.relative_to(_SRC)} imports '{match.group(1)}'")
    return offenders


@pytest.mark.parametrize("package", _NEW_PACKAGES)
def test_package_has_no_direct_provider_import(package):
    offenders = _scan(package)
    assert not offenders, (
        "direct provider-SDK import(s) found — every model call must go "
        "through oneops.llm.LlmGateway:\n  " + "\n  ".join(offenders))


def test_the_gate_actually_scans_files():
    # Guard against the gate silently passing because it found nothing to scan.
    assert (_SRC / "llm" / "gateway.py").is_file()
    assert sum(1 for _ in (_SRC / "router").rglob("*.py")) > 0
