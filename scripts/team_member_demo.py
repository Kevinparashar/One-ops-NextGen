#!/usr/bin/env python3
"""Team / member-selector — the manager→member routing theory, end to end.

Mirrors the product's AiTeam(manager + members) model on top of the validated
NextGen router. A Team groups a subset of agents as MEMBERS; the manager's job
is to pick WHICH member handles a query — via retrieve → rerank → abstain scoped
to that team's members (reusing GatewayEmbedder + PgVectorRetriever +
LlmDisambiguator + the abstain floor). Then it DISPATCHES the selected member to
the live system for a real answer.

This is the "which team member?" decision engine spec'd earlier, made runnable:
  query → [team chosen] → MemberSelector.select(query, team.members)
        → SELECT member(s) | CLARIFY | HANDLE_SELF → dispatch → response

Offline selection (Postgres + gateway); --execute also dispatches to /api/chat.
Run:  .venv/bin/python scripts/team_member_demo.py [--execute]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

import asyncpg  # noqa: E402

from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402
from oneops.registry.loader import load_registry  # noqa: E402
from oneops.router.disambiguation import LlmDisambiguator  # noqa: E402
from oneops.router.retrieval import (  # noqa: E402
    GatewayEmbedder, PgVectorRetriever, configure_hnsw_connection)

# Teams = manager + member subset (mirrors AiTeam.members). The point: selection
# is SCOPED to a team's members, so the same query routes differently per team
# (and abstains when no member fits — e.g. a summarize query hitting the Ops team).
TEAMS = {
    "Support Team": ["uc01_summarization", "uc02_similar_tickets", "uc03_kb_lookup"],
    "Ops Team": ["uc05_triage", "uc08_fulfillment"],
}

DEMO = {
    "Support Team": [
        "summarize INC0008008",
        "are there tickets like INC0008008",
        "how do I fix the VPN client",
        "tell me a joke",                       # off-domain → HANDLE_SELF
    ],
    "Ops Team": [
        "triage INC0008008 and assign it",
        "I need a new laptop",
        "summarize INC0008008",                 # no Ops member fits → CLARIFY
    ],
}


def _pg_url() -> str:
    m = re.search(r"^POSTGRES_URL=(.+)$", (_ROOT / ".env").read_text(), re.M)
    if not m:
        raise SystemExit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


def _gateway() -> LlmGateway:
    return LlmGateway(transport=LiteLLMTransport(
        base_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311"),
        api_key=(os.environ.get("LLM_GATEWAY_API_KEY")
                 or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")), redact=False)


class MemberSelector:
    """The manager's 'which member?' brain — retrieve → rerank → abstain, scoped
    to one team's members. Returns ('SELECT', members), ('CLARIFY', None), or
    ('HANDLE_SELF', None)."""

    def __init__(self, retriever, disambiguator, tenant: str, top_k: int = 10):
        self._retr = retriever
        self._dis = disambiguator
        self._tenant = tenant
        self._top_k = top_k

    async def select(self, query: str, members: list[str]) -> tuple[str, list[str], str]:
        cands = await self._retr.retrieve(query, tenant_id=self._tenant, top_k=self._top_k)
        scoped = [c for c in cands if c.agent_id in members]   # scope to THIS team
        if not scoped:
            return ("CLARIFY", [], "no member of this team matched the request")
        res = await self._dis.disambiguate(query, scoped, request_ctx={})
        chosen = [a for a in res.selected_agent_ids if a in members]
        if not chosen:
            # off-domain refusal vs weak/ambiguous abstain both land here
            why = res.rationale or "nothing in this team clears the confidence floor"
            return ("HANDLE_SELF" if "off" in (res.rationale or "").lower() else "CLARIFY",
                    [], why)
        return ("SELECT", chosen, res.rationale or "")


def _dispatch(query: str, i: int, forced: list[str]) -> tuple[list[str], str, str]:
    """Dispatch the manager's SELECTED member directly via the pre-routed path
    (forced_agent_ids) — the executor skips global routing and runs that member,
    so execution matches selection (and button-only members run in-team)."""
    hdr = {"content-type": "application/json", "x-tenant-id": "T001",
           "x-user-id": "oneops", "x-role": "service_desk_agent"}
    payload: dict = {"message": query, "session_id": f"team-demo-{i}"}
    if forced:
        payload["forced_agent_ids"] = forced
    req = urllib.request.Request(
        "http://localhost:8765/api/chat",
        data=json.dumps(payload).encode(),
        headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:        # noqa: S310
        resp = json.loads(r.read().decode())
    seen: list[str] = []
    for s in (resp.get("step_results") or []):
        a = s.get("agent_id")
        if a and a not in seen:
            seen.append(a)
    return seen, (resp.get("final_status") or ""), (resp.get("final_response") or "")[:90]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="also dispatch SELECT cases to /api/chat for a real answer")
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    args = ap.parse_args()

    registry = load_registry()
    gw = _gateway()
    floor = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=1, max_size=4, init=configure_hnsw_connection)
    retr = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)
    # strict_fit: a team's members are a FIXED set, so the reranker must refuse
    # when none fit (policy §Identity/Scope L74 + §7 L315 + §Planner L466) rather
    # than force-pick the least-bad — the manager then clarifies / re-routes.
    dis = LlmDisambiguator(gw, registry=registry,
                           abstain_min_score=(float(floor) if floor else None),
                           strict_fit=True)
    sel = MemberSelector(retr, dis, args.tenant)

    print(f"=== Team member-selector — end to end (abstain={floor or 'off'}, "
          f"execute={args.execute}) ===")
    i = 0
    try:
        for team, members in TEAMS.items():
            i = await _run_team(team, members, sel, i, args.execute)
    finally:
        await pool.close()
    return 0


async def _run_team(team: str, members: list[str], sel, start_i: int,
                    execute: bool) -> int:
    """Run every demo query for one team, printing each manager verdict.
    Returns the running case counter after this team."""
    print(f"\n### {team}   members: {', '.join(members)}")
    i = start_i
    for q in DEMO[team]:
        i += 1
        verdict, chosen, why = await sel.select(q, members)
        if verdict != "SELECT":
            print(f"  {q!r:<42} → manager {verdict}  ({why[:70]})")
            continue
        line = f"  {q!r:<42} → manager SELECTS {'+'.join(chosen)}"
        if execute:
            ex, st, snip = _dispatch(q, i, chosen)
            match = "MATCH" if set(chosen) & set(ex) else f"exec={ex}"
            line += f"\n        executed → {ex or '[]'} [{match}] status={st}  «{snip}»"
        print(line)
    return i


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
