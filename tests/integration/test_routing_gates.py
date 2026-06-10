"""Routing-quality CI gates — recall, held-out accuracy, counter-example regression.

These run the three eval harnesses against LIVE Postgres + the LLM gateway and
assert FLOORS, so a card / prompt / embedding change that degrades routing fails
CI instead of shipping. This is the "measure, not guess" backstop the routing
research calls mandatory at scale (and the doc's eval-loop).

  * counter_example_eval.py — a known confusion must NEVER land on a forbidden
    look-alike agent (hard gate; exit 0 == clean).
  * retrieval_eval.py       — the correct agent must stay in the top-K shortlist
    (recall@5 floor). If this drops, the embeddings/cards are the problem.
  * routing_holdout_eval.py — end-to-end routing accuracy on UNSEEN queries
    (held-out floor). If this drops, the reranker/abstain config regressed.

They cost LLM calls + need infra, so they are gated behind RUN_ROUTING_EVAL=1
(plus POSTGRES_URL). Default unit/integration runs skip them at collection — the
expected state in hermetic CI.

Run:
  RUN_ROUTING_EVAL=1 .venv/bin/python -m pytest -m routing_eval -v
  make eval
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# Load .env so the POSTGRES_URL skip-guard sees the configured DSN even when it
# isn't exported in the shell (lets `make eval` work standalone). RUN_ROUTING_EVAL
# stays a command-line opt-in (override=False won't clobber it).
try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except Exception:  # noqa: BLE001
    pass

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.routing_eval,
    pytest.mark.skipif(
        os.getenv("RUN_ROUTING_EVAL") != "1" or not os.getenv("POSTGRES_URL", "").strip(),
        reason="routing-quality gates need RUN_ROUTING_EVAL=1 + POSTGRES_URL + a live "
               "LLM gateway (expected-skipped in hermetic CI / unit runs)"),
]

# Regression floors — the bar a change must clear. Tune via env as scale grows;
# a drop below any of these fails CI, which is the point.
_RECALL5_FLOOR = float(os.getenv("ROUTING_RECALL5_FLOOR", "0.95"))
_HOLDOUT_FLOOR = float(os.getenv("ROUTING_HOLDOUT_FLOOR", "0.90"))


def _run(script: str, *args: str, timeout: int) -> tuple[int, str]:
    """Run an eval harness with the same interpreter, from the repo root."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(_ROOT / "scripts" / script), *args],
        cwd=str(_ROOT), capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _pct(pattern: str, text: str) -> float:
    m = re.search(pattern, text)
    assert m, f"could not parse {pattern!r} from harness output:\n…{text[-1800:]}"
    return float(m.group(1)) / 100.0


@pytest.mark.timeout(600)
def test_counter_example_no_misroutes():
    """Hard gate: no curated counter-example may route to a must_not_route agent."""
    rc, out = _run("counter_example_eval.py", timeout=580)
    assert rc == 0, (
        "counter-example REGRESSION — a known confusion landed on a forbidden "
        f"agent (exit {rc}):\n…{out[-2200:]}")


@pytest.mark.timeout(600)
def test_retrieval_recall_floor():
    """Search quality: the correct agent must stay within the top-5 shortlist."""
    _, out = _run("retrieval_eval.py", "--top-k", "5", timeout=580)
    recall5 = _pct(r"recall@5\s+\d+/\d+ =\s*([\d.]+)%", out)
    assert recall5 >= _RECALL5_FLOOR, (
        f"recall@5 {recall5:.1%} < floor {_RECALL5_FLOOR:.0%} — embeddings/cards "
        f"regressed (right agent falling out of the shortlist):\n…{out[-1200:]}")


@pytest.mark.timeout(900)
def test_holdout_accuracy_floor():
    """End-to-end routing on UNSEEN queries must clear the held-out floor."""
    _, out = _run("routing_holdout_eval.py", timeout=880)
    new_acc = _pct(r"NEW[^\n]*?=\s*([\d.]+)%", out)
    assert new_acc >= _HOLDOUT_FLOOR, (
        f"held-out routing accuracy {new_acc:.1%} < floor {_HOLDOUT_FLOOR:.0%} — "
        f"reranker/abstain config regressed:\n…{out[-1200:]}")
