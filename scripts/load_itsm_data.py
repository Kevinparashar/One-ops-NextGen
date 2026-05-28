"""Load data/itsm/*.json into the NextGen-ai `itsm` schema.

Connects only via POSTGRES_URL from .env (the pinned NextGen-ai target).
Loads in foreign-key dependency order, inside one transaction — any bad row
(dangling or cross-tenant reference) makes the whole load roll back, so the
database is never left half-populated. `ON CONFLICT DO NOTHING` makes a
re-run safe.

Run:  .venv/bin/python scripts/load_itsm_data.py
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "itsm"

# table -> ordered (column, kind). kind drives value conversion.
#   plain str | b(bool) | i(int) | ts | dt(date) | A(text[]) | J[]/J{} (jsonb)
SPEC: dict[str, list[tuple[str, str]]] = {
    "sys_user": [("tenant_id","s"),("user_id","s"),("name","s"),("email","s"),
        ("role","s"),("department","s"),("location","s"),("manager_id","s"),
        ("vip","b"),("locale","s"),("is_active","b")],
    "catalog_item": [("tenant_id","s"),("catalog_item_id","s"),("name","s"),
        ("description","s"),("category","s"),("owner_group","s"),
        ("estimated_total_minutes","i"),("tasks","J[]")],
    "onboarding_template": [("tenant_id","s"),("template_id","s"),("name","s"),
        ("description","s"),("department","s"),("default_catalog_item_id","s"),
        ("required_inputs","A"),("tasks","J[]")],
    "cmdb_ci": [("tenant_id","s"),("ci_id","s"),("ci_name","s"),("ci_type","s"),
        ("environment","s"),("status","s"),("owner","s"),("location","s"),
        ("criticality","s"),("relationships","J[]"),("attributes","J{}")],
    "asset": [("tenant_id","s"),("asset_id","s"),("asset_name","s"),
        ("asset_class","s"),("subtype","s"),("model","s"),("vendor","s"),
        ("serial_number","s"),("assigned_to","s"),("linked_ci","s"),
        ("location","s"),("status","s"),("purchase_date","dt"),
        ("warranty_expiry","dt")],
    "problem": [("tenant_id","s"),("problem_id","s"),("title","s"),
        ("description","s"),("status","s"),("priority","s"),("category","s"),
        ("root_cause","s"),("workaround","s"),("known_error","b"),
        ("related_incidents","A"),("related_changes","A"),("owner","s"),
        ("created_at","ts"),("updated_at","ts")],
    "change": [("tenant_id","s"),("change_id","s"),("title","s"),
        ("description","s"),("state","s"),("change_type","s"),
        ("risk_level","s"),("impact","s"),("approval_status","s"),
        ("approved_by","A"),("requested_by","s"),("assigned_to","s"),
        ("assignment_group","s"),("affected_ci","A"),("related_problem","s"),
        ("planned_start","ts"),("planned_end","ts"),("actual_start","ts"),
        ("actual_end","ts"),("created_at","ts"),("updated_at","ts")],
    "incident": [("tenant_id","s"),("incident_id","s"),("title","s"),
        ("description","s"),("status","s"),("priority","s"),("severity","s"),
        ("impact","s"),("urgency","s"),("category","s"),("subcategory","s"),
        ("service_name","s"),("reported_by","s"),("assigned_to","s"),
        ("assignment_group","s"),("ci_id","s"),("linked_ci_ids","A"),
        ("related_problem","s"),("related_change","s"),("attachments","J[]"),
        ("work_notes","J[]"),("comments","J[]"),("sla_due","ts"),
        ("sla_breached","b"),("created_at","ts"),("updated_at","ts"),
        ("resolved_at","ts")],
    "request": [("tenant_id","s"),("request_id","s"),("title","s"),
        ("description","s"),("status","s"),("stage","s"),("priority","s"),
        ("category","s"),("catalog_item_id","s"),("requested_for","s"),
        ("requested_by","s"),("approved_by","A"),("assigned_to","s"),
        ("assignment_group","s"),("ci_id","s"),("sla_due","ts"),
        ("sla_breached","b"),("comments","J[]"),("created_at","ts"),
        ("updated_at","ts"),("fulfilled_at","ts")],
    "kb_knowledge": [("tenant_id","s"),("kb_id","s"),("title","s"),
        ("summary","s"),("content","s"),("category","s"),("tags","A"),
        ("state","s"),("audience","s"),("created_by","s"),("created_at","ts"),
        ("updated_at","ts"),("views","i"),("helpful_votes","i"),
        ("related_ci_ids","A"),("related_incidents","A")],
}
# FK dependency order.
ORDER = ["sys_user", "catalog_item", "onboarding_template", "cmdb_ci", "asset",
         "problem", "change", "incident", "request", "kb_knowledge"]


def _ts(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _dt(v):
    return date.fromisoformat(v) if v else None


def convert(value, kind):
    if kind == "s":
        return value
    if kind == "b":
        return bool(value) if value is not None else False
    if kind == "i":
        return int(value) if value is not None else None
    if kind == "ts":
        return _ts(value)
    if kind == "dt":
        return _dt(value)
    if kind == "A":
        return list(value) if value else []
    if kind == "J[]":
        return json.dumps(value if value is not None else [])
    if kind == "J{}":
        return json.dumps(value if value is not None else {})
    raise ValueError(f"unknown kind {kind}")


async def main() -> None:
    url = re.search(r"^POSTGRES_URL=(.+)$",
                    (ROOT / ".env").read_text(), re.M).group(1).strip()
    ref = re.search(r"postgres\.([a-z0-9]+):", url).group(1)
    conn = await asyncpg.connect(dsn=url, timeout=20)
    try:
        db = await conn.fetchval("SELECT current_database()")
        print("── PRE-FLIGHT ──")
        print(f"project ref : {ref} (NextGen-ai)   database: {db}")
        have = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='itsm'")
        print(f"itsm tables present : {have}")
        if have != len(SPEC):
            raise SystemExit("itsm schema not ready — run the migration first.")

        print("\n── LOADING (single transaction, dependency order) ──")
        async with conn.transaction():
            for table in ORDER:
                spec = SPEC[table]
                rows = json.loads((DATA / f"{table}.json").read_text())
                cols = [c for c, _ in spec]
                ph = ", ".join(f"${i+1}" for i in range(len(cols)))
                sql = (f"INSERT INTO itsm.{table} ({', '.join(cols)}) "
                       f"VALUES ({ph}) ON CONFLICT DO NOTHING")
                values = [
                    tuple(convert(r.get(c), k) for c, k in spec)
                    for r in rows
                ]
                await conn.executemany(sql, values)
                print(f"  {table:22s} {len(values):4d} rows")

        print("\n── VERIFICATION ──")
        total = 0
        for table in ORDER:
            n = await conn.fetchval(f"SELECT count(*) FROM itsm.{table}")
            per = await conn.fetch(
                f"SELECT tenant_id, count(*) c FROM itsm.{table} "
                f"GROUP BY tenant_id ORDER BY tenant_id")
            total += n
            split = " ".join(f"{r['tenant_id']}={r['c']}" for r in per)
            print(f"  {table:22s} {n:4d}   [{split}]")
        print(f"\n  TOTAL ROWS LOADED : {total}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
