"""PMG evidence — lifecycle state machine end-to-end proof (2026-05-31).

Production-grade verification harness for the manager axis-1 deliverable.
Runs against an isolated copy of the live `registries/v2` so the live
service is never touched — same shape as the live registry, same loader,
same audit emit path.

What this script proves, in order:

  1. Boot-time lifecycle inventory log fires with correct counts.
  2. A draft agent is invisible to `list_active()` (router can't pick it).
  3. Activating that draft fires `registry.lifecycle.transition` audit emit
     AND the agent now appears in `list_active()`.
  4. Deprecating the agent removes it from `list_active()` but keeps it
     callable via `get()`, AND every `get()` emits a `deprecation_used`
     event.
  5. Retiring the agent removes it from `get_optional()` AND from
     `list_active()`.
  6. Final lifecycle summary matches expected counts.

Output goes to stdout (captured by `ops/pmg-evidence/verify-all.sh` into
`ops/pmg-evidence/lifecycle.log`). Each step writes a one-line
status: "✅ STEP N — ..." with the asserted invariant.

Exit code:
  0 = all 6 steps verified
  1 = any step failed (assertion or exception)
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from io import StringIO
from pathlib import Path

import structlog


def main() -> int:
    # Send structlog output to stdout so it's captured in the evidence log
    log_buffer = StringIO()
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(
                key_order=["event", "kind", "record_id", "version", "to_status"],
            ),
        ],
        logger_factory=structlog.WriteLoggerFactory(file=log_buffer),
    )

    src = Path("registries/v2")
    if not src.exists():
        print("FAIL: registries/v2 does not exist — run from repo root", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as td:
        dst = Path(td) / "v2"
        shutil.copytree(src, dst)

        # Lazy imports so structlog config above takes effect
        from oneops.registry.service import RegistryService
        from oneops.registry.models import (
            AbacTags,
            ActivationCondition,
            AgentRecord,
            ConditionOperator,
            ConditionSignal,
            DeterminismLevel,
            ExecutionTier,
            RecordStatus,
            RoutingShape,
        )

        svc = RegistryService.from_path(str(dst))
        print("═══ PMG EVIDENCE — Lifecycle State Machine ═══")
        print()

        # STEP 1 — Boot inventory log
        print("─── STEP 1: emit_boot_lifecycle_log fires per-kind counts ───")
        svc.emit_boot_lifecycle_log()
        boot_lines = [line for line in log_buffer.getvalue().splitlines()
                      if "registry.lifecycle.boot" in line]
        assert len(boot_lines) == 3, f"expected 3 boot lines (agents/tools/schemas), got: {boot_lines}"
        for line in boot_lines:
            print(f"  {line}")
        before = svc.lifecycle_summary()["agents"]
        print(f"  ✅ STEP 1 — boot inventory: {before}")
        print()

        # STEP 2 — Create a demo agent in DRAFT, verify list_active excludes it
        print("─── STEP 2: DRAFT record is invisible to list_active() ───")
        demo_agent = AgentRecord(
            id="uc99_demo_lifecycle", version=1, owner="team-pmg-evidence",
            description="Demo agent created by PMG evidence harness — never serves real traffic.",
            intent_family="entity_summary", routing_shape=RoutingShape.SINGLE,
            activation_condition=ActivationCondition(
                operator=ConditionOperator.LEAF,
                signal=ConditionSignal.INTENT_IN, values=("demo_pmg_evidence",)),
            abac_tags=AbacTags(tier=ExecutionTier.READ),
            determinism_level=DeterminismLevel.LOW)
        svc.agents.create(demo_agent)
        actives = [a.id for a in svc.agents.list_active()]
        assert "uc99_demo_lifecycle" not in actives, \
            f"draft must NOT be in list_active; got: {actives}"
        summary = svc.lifecycle_summary()["agents"]
        assert summary["draft"] == 1, summary
        print(f"  list_active excludes draft: ✓")
        print(f"  lifecycle_summary: {summary}")
        print(f"  ✅ STEP 2 — DRAFT correctly invisible to router")
        print()

        # STEP 3 — Activate, verify audit emit + list_active inclusion
        print("─── STEP 3: activate() emits transition audit + record appears active ───")
        log_buffer.seek(0)
        log_buffer.truncate()
        svc.agents.activate("uc99_demo_lifecycle", 1)
        audit_lines = [line for line in log_buffer.getvalue().splitlines()
                       if "transition" in line and "uc99_demo_lifecycle" in line]
        assert audit_lines, f"expected transition audit emit; got: {log_buffer.getvalue()}"
        for line in audit_lines: print(f"  {line}")
        actives = [a.id for a in svc.agents.list_active()]
        assert "uc99_demo_lifecycle" in actives, f"expected in active; got: {actives}"
        print(f"  ✅ STEP 3 — activated + audit emit + in list_active")
        print()

        # STEP 4 — Deprecate, verify list_active removes BUT get() still works + warning
        print("─── STEP 4: deprecate() removes from list_active but get() still works ───")
        log_buffer.seek(0)
        log_buffer.truncate()
        svc.agents.deprecate("uc99_demo_lifecycle", 1)
        transition_lines = [line for line in log_buffer.getvalue().splitlines()
                            if "transition" in line and "deprecated" in line]
        assert transition_lines, "expected deprecation transition emit"
        for line in transition_lines: print(f"  {line}")
        actives = [a.id for a in svc.agents.list_active()]
        assert "uc99_demo_lifecycle" not in actives, "must NOT be in list_active"
        # Now get() — should fire deprecation_used
        log_buffer.seek(0)
        log_buffer.truncate()
        r = svc.agents.get("uc99_demo_lifecycle")
        assert r.status == RecordStatus.DEPRECATED, f"status: {r.status}"
        used_lines = [line for line in log_buffer.getvalue().splitlines()
                      if "deprecation_used" in line]
        assert used_lines, f"expected deprecation_used on get(); got: {log_buffer.getvalue()}"
        for line in used_lines: print(f"  {line}")
        print(f"  ✅ STEP 4 — deprecated + invisible to router + still callable + warning emitted")
        print()

        # STEP 5 — Retire, verify get_optional returns None + list_active stays clean
        print("─── STEP 5: retire() makes record fully invisible ───")
        log_buffer.seek(0)
        log_buffer.truncate()
        svc.agents.retire("uc99_demo_lifecycle", 1)
        transition_lines = [line for line in log_buffer.getvalue().splitlines()
                            if "transition" in line and "retired" in line]
        assert transition_lines, "expected retirement transition emit"
        for line in transition_lines: print(f"  {line}")
        actives = [a.id for a in svc.agents.list_active()]
        assert "uc99_demo_lifecycle" not in actives
        opt = svc.agents.get_optional("uc99_demo_lifecycle")
        assert opt is None, f"get_optional must return None for retired; got: {opt}"
        print(f"  ✅ STEP 5 — retired + get_optional returns None")
        print()

        # STEP 6 — Final inventory
        print("─── STEP 6: final lifecycle summary ───")
        final = svc.lifecycle_summary()["agents"]
        print(f"  before:  {before}")
        print(f"  after:   {final}")
        assert final["active"] == before["active"], "live UCs must remain active"
        assert final["retired"] == 1, f"expected 1 retired (the demo); got: {final}"
        print(f"  ✅ STEP 6 — counts match expected end-state")
        print()
        print("═══ ALL 6 STEPS VERIFIED — lifecycle state machine is production-grade ═══")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n❌ ASSERTION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:                                            # noqa: BLE001
        print(f"\n❌ ERROR: {e!r}", file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)
