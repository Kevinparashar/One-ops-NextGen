#!/usr/bin/env python3
"""Full-funnel routing eval — the REAL Router.route() (decompose → rewrite →
retrieve → Stage-3 activation/ABAC filter → preroute → disambiguate).

The offline retrieve+rerank harness under-tested SETS (no decompose) and
mis-scored uc05/uc08 (no Stage-3 filter — those are API/button-only, filtered
from chat). This builds the production Router exactly as api/app.py does and
calls route() per query, so the score reflects the real chat path.

Scope: chat-routable scenarios only (uc01/02/03 + sets + off-domain). uc05/uc08
are excluded — they are not chat-reachable and must be tested on their API path.
Queries are the same UNSEEN real-user set as routing_eval100. Parallelized.

Run:  .venv/bin/python scripts/routing_eval_fullfunnel.py [--concurrency 10]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

import asyncpg  # noqa: E402
from routing_eval100 import DATASET as _ALL  # noqa: E402
from routing_eval100 import NONE, _s, _verdict

from oneops.authz.models import Principal  # noqa: E402
from oneops.authz.service import AuthzService  # noqa: E402
from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402
from oneops.registry.loader import load_registry  # noqa: E402
from oneops.router.decompose import LlmDecomposer  # noqa: E402
from oneops.router.disambiguation import LlmDisambiguator  # noqa: E402
from oneops.router.entity_id import EntityIdNormalizer  # noqa: E402
from oneops.router.glossary import Glossary  # noqa: E402
from oneops.router.retrieval import (  # noqa: E402
    GatewayEmbedder,
    PgVectorRetriever,
    configure_hnsw_connection,
)
from oneops.router.rewrite import LlmRewriter  # noqa: E402
from oneops.router.router import Router  # noqa: E402
from oneops.router.signals import RequestSignals  # noqa: E402

# chat-routable only — uc05/uc08 are API/button-only (filtered by Stage-3 in chat)
DATASET = [(q, e, c) for (q, e, c) in _ALL if c not in ("uc05", "uc08")]


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


def _format_misroute(
    q: str, exp: object, cat: str, chosen: set, err: str | None,
) -> str:
    """One mis-route line: expected vs got (got = ERROR text / chosen set / none)."""
    exp_l = (_s("+".join(sorted(exp))) if isinstance(exp, (set, list, tuple))
             else _s(exp) if exp != NONE else "none")
    got_l = ("ERROR:" + err) if err else ("+".join(sorted(_s(a) for a in chosen)) or "none")
    return f"  ✗ {q!r}\n      want {exp_l}  got {got_l}  [{cat}]"


def _summarize(results: list, floor: str) -> int:
    """Fold per-query results into overall + by-category accuracy, print the
    report, and return the exit code (0 iff every query routed correctly)."""
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    ok_total = 0
    misroutes: list[str] = []
    for q, exp, cat, chosen, err in results:
        ok = (not err) and _verdict(exp, chosen)
        by_cat[cat][1] += 1
        by_cat[cat][0] += int(ok)
        ok_total += int(ok)
        if not ok:
            misroutes.append(_format_misroute(q, exp, cat, chosen, err))

    n = len(results)
    print(f"=== FULL-FUNNEL routing — {n} chat-routable UNSEEN queries "
          f"(real route(): decompose + Stage-3 + rerank), abstain={floor or 'off'} ===\n")
    print(f"OVERALL: {ok_total}/{n} = {ok_total/n*100:.1f}%\n")
    print("BY CATEGORY:")
    for cat in sorted(by_cat):
        ok, tot = by_cat[cat]
        print(f"  {cat:<16} {ok}/{tot} = {ok/tot*100:3.0f}%")
    if misroutes:
        print(f"\nMISROUTES ({len(misroutes)}):")
        print("\n".join(misroutes))
    return 0 if ok_total == n else 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.getenv("ONEOPS_EVAL_TENANT", "T001"))
    ap.add_argument("--role", default=os.getenv("ONEOPS_EVAL_ROLE", "service_desk_agent"))
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args()

    registry = load_registry()
    gw = _gateway()
    model = os.getenv("LLM_DEFAULT_MODEL", "gpt-4o-mini").strip()
    floor = os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_SCORE", "").strip()
    pool = await asyncpg.create_pool(
        _pg_url(), min_size=2, max_size=args.concurrency + 2,
        init=configure_hnsw_connection)
    retriever = PgVectorRetriever(registry, embedder=GatewayEmbedder(gw), pool=pool)
    disambiguator = LlmDisambiguator(
        gw, model=model, registry=registry,
        abstain_min_score=(float(floor) if floor else None),
        abstain_min_margin=float(os.getenv("ONEOPS_ROUTER_ABSTAIN_MIN_MARGIN", "0") or "0"))
    router = Router(
        registry=registry, glossary=Glossary.from_file(), retriever=retriever,
        disambiguator=disambiguator, authz=AuthzService.create(),
        rewriter=LlmRewriter(gw, model=model), decomposer=LlmDecomposer(gw, model=model))
    normalizer = EntityIdNormalizer.from_registry_file()
    sem = asyncio.Semaphore(args.concurrency)

    async def _run_one(q: str, exp: object, cat: str) -> tuple:
        async with sem:
            try:
                ex = normalizer.extract(q)
                present = tuple((e.entity_id, e.service_id) for e in ex.entities)
                principal = Principal(tenant_id=args.tenant, user_id="oneops", role=args.role)
                signals = RequestSignals(role=args.role, tenant_id=args.tenant,
                                         present_entities=present)
                res = await router.route(q, principal=principal, signals=signals,
                                         conversation_history=[], request_ctx={})
                chosen = set(res.plan.agent_ids) if res.plan is not None else set()
                return (q, exp, cat, chosen, None)
            except Exception as e:  # noqa: BLE001
                return (q, exp, cat, set(), str(e)[:140])

    try:
        results = await asyncio.gather(*[_run_one(q, e, c) for q, e, c in DATASET])
    finally:
        await pool.close()

    return _summarize(results, floor)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
