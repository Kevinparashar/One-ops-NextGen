"""Registry scalability benchmark — proves (or disproves) the 1000-UC / 10000-tool claim.

Generates a synthetic `registries/v2`-shaped tree (N agents + M tools) from the real
schema (cloned from a live record, so it's schema-valid), then measures:
  * boot: `load_registry(tmp, check_integrity=True)` — discovery (rglob) + parse + integrity
  * lookup: mean latency of `tools.get(id)` (exposes the per-call rglob in FileBackend.get)

Run:  .venv/bin/python scripts/registry_scale_bench.py
This is a DIAGNOSTIC, not a test — it writes only to a TemporaryDirectory and is
never imported by the app. It exists to quantify the registry's scaling behavior
before any optimization (Step 1 of the registry-scalability work).
"""
from __future__ import annotations

import copy
import json
import random
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from oneops.registry.loader import load_registry

_REPO = Path(__file__).resolve().parents[1]
_AGENT_TMPL = _REPO / "registries/v2/agents/uc01_summarization.json"
_TOOL_TMPL = _REPO / "registries/v2/tools/uc01_summarization/get_ticket_details.json"


def _clone(template: dict, new_id: str, *, strip_tool_refs: bool) -> dict:
    d = copy.deepcopy(template)
    d["id"] = new_id
    ver = d["versions"][str(d["active_version"])]
    ver["id"] = new_id
    if strip_tool_refs:
        ver["tool_refs"] = []          # keep integrity trivially satisfiable
        ver.pop("skills", None)
        ver.pop("fast_path", None)
    return d


def _materialise(root: Path, n_agents: int, n_tools: int) -> None:
    agent_tmpl = json.loads(_AGENT_TMPL.read_text())
    tool_tmpl = json.loads(_TOOL_TMPL.read_text())
    adir = root / "agents"
    tdir = root / "tools" / "bench"
    adir.mkdir(parents=True)
    tdir.mkdir(parents=True)
    for i in range(n_agents):
        aid = f"uc_bench_{i:05d}"
        (adir / f"{aid}.json").write_text(
            json.dumps(_clone(agent_tmpl, aid, strip_tool_refs=True)))
    for j in range(n_tools):
        tid = f"tool_bench_{j:05d}"
        (tdir / f"{tid}.json").write_text(
            json.dumps(_clone(tool_tmpl, tid, strip_tool_refs=False)))


def _bench(n_agents: int, n_tools: int, *, lookups: int = 50) -> dict:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "v2"
        # provide the non-agent/tool files load_registry may expect by symlinking
        # the real ones (glossary/policy/field_policy/service-schema) so the loader
        # has its supporting data; agents/tools are the synthetic scale set.
        root.mkdir(parents=True)
        for extra in ("glossary.json", "policy_rules.json", "field_policy.json",
                      "service-schema.json", "schemas", "display_specs"):
            src = _REPO / "registries/v2" / extra
            if src.exists():
                dst = root / extra
                if src.is_dir():
                    dst.symlink_to(src)
                else:
                    dst.write_text(src.read_text())
        _materialise(root, n_agents, n_tools)

        t0 = time.monotonic()
        reg = load_registry(str(root), check_integrity=True)
        boot_s = time.monotonic() - t0

        ids = [f"tool_bench_{random.randint(0, n_tools - 1):05d}"
               for _ in range(lookups)]
        t1 = time.monotonic()
        for tid in ids:
            reg.tools.get(tid)
        get_ms = (time.monotonic() - t1) / lookups * 1000.0
        return {"agents": n_agents, "tools": n_tools,
                "boot_s": round(boot_s, 2), "get_ms": round(get_ms, 1)}


if __name__ == "__main__":
    random.seed(0)
    print(f"{'agents':>7} {'tools':>7} {'boot_s':>8} {'get_ms':>8}")
    for n_a, n_t in [(10, 50), (100, 1000), (1000, 10000)]:
        r = _bench(n_a, n_t)
        print(f"{r['agents']:>7} {r['tools']:>7} {r['boot_s']:>8} {r['get_ms']:>8}")
