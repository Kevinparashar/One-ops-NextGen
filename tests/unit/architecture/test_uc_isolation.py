"""Enforce the UC-isolation rule (locked 2026-05-29).

Every src/oneops/use_cases/<uc>/ module owns its own code. A UC MUST NOT
import from a sibling UC. The platform substrate (llm, policy, tenancy,
observability, registry, embeddings.triage_input) is the only shared
surface and is explicitly allowlisted.

If this test fails, the offending import name + file are reported. Fix
by copying the needed code into the offending UC (full isolation) or
by promoting it into the platform substrate via an explicit ADR.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_USE_CASES_DIR = _REPO_ROOT / "src" / "oneops" / "use_cases"


def _iter_uc_modules() -> list[Path]:
    if not _USE_CASES_DIR.exists():
        return []
    return [p for p in _USE_CASES_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def _own_uc(path: Path) -> str:
    rel = path.relative_to(_USE_CASES_DIR).parts
    return rel[0] if rel else ""


@pytest.mark.parametrize("path", _iter_uc_modules(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_no_cross_uc_imports(path: Path) -> None:
    """Each use_case module must not import from a sibling use_case."""
    own = _own_uc(path)
    tree = ast.parse(path.read_text())
    violations: list[str] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        for name in names:
            if not name.startswith("oneops.use_cases."):
                continue
            other = name.split(".")[2]
            if other != own and other != "_shared":
                violations.append(name)
    assert not violations, (
        f"{path.relative_to(_REPO_ROOT)} imports from sibling UC(s): {violations}. "
        "Copy the needed code into this UC (isolation rule) or promote it into "
        "the platform substrate via an ADR."
    )
