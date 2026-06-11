"""Live verification of the UC-8 approval flow against the real DB.

Drives the REAL gate (`tools._apply_approval_gate`) and the REAL non-chat
approve (`approval.decide_approval`) end-to-end, snapshotting all three tables
(`itsm.request` / `itsm.request_item` / `itsm.approval`) at each step. Proves
the approval matrix + approver resolution + park + decide are wired and update
data correctly.

Writes a DEMO_-prefixed SR/RITM and removes everything it created on exit
(prod tables left clean). Findings: docs/verification/uc08-approval-live-verification.md

Run:  python dev/verify_uc08_approval.py
Env:  POSTGRES_URL (read from .env if present). UC08_APPROVAL_ENABLED is not
      required — the gate function is called directly.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

# Demo identifiers — DEMO_ prefix so they are unmistakable and easy to purge.
TENANT = "T001"
CATALOG_ITEM = "CAT_HR_PORTAL_ACCESS"   # category 'access' -> cat_access -> owning_group
REQUESTER = "USR00008"
REQ_ID = "REQ_APPRDEMO1"
RITM_ID = "RITM_APPRDEMO1"


def _load_dsn() -> str:
    dsn = os.getenv("POSTGRES_URL", "")
    if not dsn:
        env = Path(__file__).resolve().parents[1] / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("POSTGRES_URL="):
                    dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not dsn:
        raise SystemExit("POSTGRES_URL not set (and no .env)")
    return dsn.split("?")[0]


async def _snapshot(conn: asyncpg.Connection, label: str) -> None:
    req = await conn.fetchrow(
        "SELECT request_id, status, stage FROM itsm.request WHERE request_id=$1",
        REQ_ID)
    ri = await conn.fetchrow(
        "SELECT ritm_id, state, approval_state FROM itsm.request_item "
        "WHERE ritm_id=$1", RITM_ID)
    ap = await conn.fetch(
        "SELECT requested_from, state, decision, approval_type, decided_by "
        "FROM itsm.approval WHERE ritm_id=$1 ORDER BY created_at", RITM_ID)
    print(f"\n===== {label} =====")
    print("  request      :", dict(req) if req else None)
    print("  request_item :", dict(ri) if ri else None)
    print(f"  approval rows: {len(ap)}")
    for a in ap:
        print("      ", dict(a))


async def _purge(conn: asyncpg.Connection) -> None:
    await conn.execute("DELETE FROM itsm.approval WHERE ritm_id=$1", RITM_ID)
    await conn.execute("DELETE FROM itsm.request_item WHERE ritm_id=$1", RITM_ID)
    await conn.execute("DELETE FROM itsm.request WHERE request_id=$1", REQ_ID)


async def main() -> None:
    dsn = _load_dsn()
    # Import after env is resolved; the gate uses an injectable connection provider.
    from oneops.use_cases.uc08_fulfillment import approval as _approval
    from oneops.use_cases.uc08_fulfillment import tools

    pool = await asyncpg.create_pool(dsn, ssl="require", min_size=1, max_size=3)

    async def _fresh_conn() -> asyncpg.Connection:        # the gate closes it
        return await asyncpg.connect(dsn, ssl="require")
    tools.set_connection_provider(_fresh_conn)

    try:
        # ── Step 0: a real SR + RITM, before the gate ────────────────────
        async with pool.acquire() as c:
            await _purge(c)
            await c.execute(
                "INSERT INTO itsm.request(tenant_id, request_id, title, status, "
                "requested_by, created_at, updated_at) "
                "VALUES($1,$2,$3,'open',$4,now(),now())",
                TENANT, REQ_ID, "DEMO HR portal access", REQUESTER)
            await c.execute(
                "INSERT INTO itsm.request_item(tenant_id, ritm_id, request_id, "
                "catalog_item_id, requested_for, opened_by, state, approval_state, "
                "opened_at, updated_at) "
                "VALUES($1,$2,$3,$4,$5,$5,'requested','not_required',now(),now())",
                TENANT, RITM_ID, REQ_ID, CATALOG_ITEM, REQUESTER)
            await _snapshot(c, "STEP 0 — SR+RITM created (BEFORE gate)")

        # ── Step 1: the REAL approval gate (parks if the matrix requires) ─
        gated = await tools._apply_approval_gate(
            tenant_id=TENANT, requester_id=REQUESTER, catalog_id=CATALOG_ITEM,
            ritm_id=RITM_ID, request_id=REQ_ID)
        print("\n>>> GATE:", {k: gated.get(k) for k in
                              ("status", "dispatched", "approver_count")}
              if gated else "None (no approval required)")
        async with pool.acquire() as c:
            await _snapshot(c, "STEP 1 — AFTER gate: PARKED")

        # ── Step 2: the REAL non-chat approve (any_one) ──────────────────
        async with pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT approval_id, requested_from FROM itsm.approval "
                "WHERE ritm_id=$1 ORDER BY created_at LIMIT 1", RITM_ID)
        if row:
            conn = await asyncpg.connect(dsn, ssl="require")
            try:
                out = await _approval.decide_approval(
                    approval_id=row["approval_id"], decision="approved",
                    decided_by=row["requested_from"], tenant_id=TENANT, conn=conn)
                print(f"\n>>> decide_approval(approved by {row['requested_from']}): "
                      f"ok={out.ok} state={out.state} should_dispatch={out.should_dispatch}")
            finally:
                await conn.close()
        async with pool.acquire() as c:
            await _snapshot(c, "STEP 2 — AFTER approve")
    finally:
        async with pool.acquire() as c:
            await _purge(c)
        print("\n[cleanup] demo rows removed — prod tables left clean.")
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
