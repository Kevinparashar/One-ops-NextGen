"""Evidence-driven not_when sharpening, gated by an LLM-as-judge.

not_when entries are the CONTRASTIVE BOUNDARIES the router's reranker (LLM
disambiguator) reads to decide when NOT to pick an agent and which agent to pick
instead. They are NOT embedded (see src/oneops/embeddings/agent_input.py) — so
this tool changes DECISION quality (measured by scripts/routing_eval.py, the
end-to-end harness), NOT retrieval recall.

Pipeline (per agent W):
  1. EVIDENCE — from the retrieval-eval dump (scripts/retrieval_eval.py --dump),
                gather the real queries where W was WRONGLY ranked above the
                correct agent C. Each query => "this intent belongs to C, not W".
  2. GENERATE — LLM proposes not_when clauses for W that exclude C's intent,
                phrased as PRINCIPLES (intent-level, never copied phrases) ending
                in "(route to <C>)".
  3. JUDGE    — gpt-4o verifies each clause: correct direction, principled,
                non-redundant, valid target, and — the key test — does NOT
                over-exclude (would not reject a query that genuinely belongs to
                W). Overall verdict better | no_improvement | worse.
  4. APPLY    — only with --apply AND verdict=better: appends accepted clauses to
                W's not_when in registries/v2/agents/<W>.json. sync + re-embed is
                a SEPARATE explicit step (database/agent/sync.py); content_hash
                flips but embeddings do NOT change (not_when isn't embedded).

Run:
  .venv/bin/python scripts/sharpen_not_when.py --agent uc01_summarization
  .venv/bin/python scripts/sharpen_not_when.py --agent uc01_summarization --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

from oneops.llm import LlmGateway, LlmMessage, LlmRequest, ResponseFormat  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402

_AGENTS = _ROOT / "registries" / "v2" / "agents"
_MAX_NEW = int(os.getenv("SHARPEN_MAX_NEW_CLAUSES", "3"))  # bloat guard per run


def _gateway() -> LlmGateway:
    return LlmGateway(
        transport=LiteLLMTransport(
            base_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311"),
            api_key=(os.environ.get("LLM_GATEWAY_API_KEY")
                     or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")),
        redact=False)


async def _ask(gw: LlmGateway, model: str, system: str, user: str) -> dict:
    resp = await gw.call(LlmRequest(
        messages=(LlmMessage("system", system), LlmMessage("user", user)),
        model=model, tenant_id="_platform", user_id="sharpen_not_when",
        response_format=ResponseFormat.JSON, request_id="sharpen_not_when"))
    return json.loads(resp.content)


_GEN_SYS = """You sharpen the not_when (out-of-scope) rules of ONE ITSM/ITOM \
routing agent.

not_when entries are CONTRASTIVE BOUNDARIES the router's reranker reads to decide \
when NOT to pick this agent and which agent to pick instead. They are PRINCIPLES, \
phrased as intents — never copied user phrases, never keyword lists (that is a \
phrase catalogue and is forbidden).

You are given:
- THIS agent's card (name, description, use_when, current not_when).
- EVIDENCE: real queries where THIS agent was wrongly ranked above the correct \
agent, each labelled with the correct agent id + that agent's description.

For each distinct way THIS agent is being confused with another agent, propose a \
not_when clause that:
- describes the INTENT that belongs to the other agent (generalise from the \
evidence — capture the underlying request, not the exact words),
- ends with "(route to <correct_agent_id>)" using the real agent id,
- is DISTINCT from this agent's existing not_when (no near-duplicates),
- is NARROW enough that it would NOT exclude a query that genuinely belongs to \
THIS agent. When the boundary is subtle, state the discriminator explicitly \
(e.g. "asks HOW TO fix it" vs "asks WHAT the record says").

Output strict JSON:
{"clauses": [{"text": "<not_when clause ending in (route to <id>)>",
              "targets": "<correct_agent_id>",
              "covers": "<one-line: which confusion this resolves>"}]}
Propose only what the evidence supports. Fewer, sharper clauses beat many vague ones."""

_JUDGE_SYS = """You are a STRICT reviewer of proposed not_when (out-of-scope) \
clauses for one routing agent. Reject freely — a bad exclusion silently \
misroutes valid traffic.

You are given the agent's card (description, use_when, existing not_when), the \
proposed clauses, and the evidence queries that motivated them.

ACCEPT a clause only if ALL hold:
- CORRECT direction: the intent it describes genuinely belongs to the agent named \
in "(route to <id>)", NOT to this agent.
- PRINCIPLED: it is an intent-level rule, not a copied phrase or keyword list.
- NON-REDUNDANT: it adds a boundary the existing not_when does not already cover.
- VALID target: <id> is one of the known agent ids provided.
- SAFE — THE KEY TEST: it would NOT exclude a query that legitimately belongs to \
THIS agent. Construct one plausible in-scope query for this agent and confirm the \
clause does not match it. If it would, REJECT as "over-exclusion".

Then an overall verdict:
  "better"         -> >=1 accepted clause that sharpens a real boundary,
  "no_improvement" -> nothing worth adding,
  "worse"          -> a clause risks over-exclusion / blurs scope.

Each clause appears in EXACTLY ONE of accepted/rejected. Output strict JSON:
{"accepted": [{"text": "...", "targets": "...", "covers": "..."}],
 "rejected": [{"text": "...", "reason": "..."}],
 "verdict": "better|no_improvement|worse",
 "score": 0.0-1.0,
 "rationale": "<one sentence>"}"""


def _load_cards() -> dict:
    cards = {}
    for f in sorted(_AGENTS.glob("*.json")):
        card = json.loads(f.read_text())
        body = card["versions"][str(card["active_version"])]
        skill = (body.get("skills") or [{}])[0]
        cards[card["id"]] = {
            "path": f, "card": card, "skill": skill,
            "name": skill.get("name"), "description": skill.get("description"),
            "use_when": skill.get("use_when") or [],
            "not_when": skill.get("not_when") or []}
    return cards


def _evidence_for(agent_id: str, dump: dict, cards: dict) -> list[dict]:
    """Queries where THIS agent wrongly outranked the correct one."""
    ev: list[dict] = []
    seen = set()
    rows = list(dump.get("hard_negatives") or [])
    rows += list(dump.get("misses") or [])
    for r in rows:
        outrankers = [a for a, _ in (r.get("outranked_by") or [])] \
            or [a for a, _ in (r.get("top5") or [])]
        if agent_id not in outrankers:
            continue
        for c in (r.get("expected") or []):
            if c == agent_id or c not in cards:
                continue
            key = (r["query"], c)
            if key in seen:
                continue
            seen.add(key)
            ev.append({"query": r["query"], "correct_agent": c,
                       "correct_description": cards[c]["description"]})
    return ev


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--evidence",
                    default=os.path.join(tempfile.gettempdir(), "hard_negatives.json"),
                    help="retrieval_eval --dump output")
    ap.add_argument("--judge-model", default=None,
                    help="defaults to DOC2QUERY_JUDGE_MODEL or gpt-4o")
    ap.add_argument("--apply", action="store_true",
                    help="append accepted clauses IF the judge says 'better'")
    args = ap.parse_args()

    cards = _load_cards()
    if args.agent not in cards:
        raise SystemExit(f"no card: {args.agent}")
    me = cards[args.agent]
    active_ids = set(cards)

    dump = json.loads(Path(args.evidence).read_text()) if Path(args.evidence).exists() else {}
    evidence = _evidence_for(args.agent, dump, cards)

    print(f"=== sharpen not_when: {args.agent} ===")
    print(f"existing not_when: {len(me['not_when'])} clauses")
    print(f"evidence (queries where {args.agent} wrongly outranked the right agent): {len(evidence)}")
    for e in evidence:
        print(f"  • {e['query']!r}  → should be {e['correct_agent']}")
    if not evidence:
        print("no evidence for this agent — nothing to sharpen."); return

    gw = _gateway()
    gen_model = os.getenv("LLM_DEFAULT_MODEL", "gpt-4o-mini").strip()
    judge_model = (args.judge_model or os.getenv("DOC2QUERY_JUDGE_MODEL", "gpt-4o")).strip()

    # 1. GENERATE
    gen = await _ask(gw, gen_model, _GEN_SYS, json.dumps({
        "name": me["name"], "description": me["description"],
        "use_when": me["use_when"], "current_not_when": me["not_when"],
        "evidence": evidence, "known_agent_ids": sorted(active_ids)},
        ensure_ascii=False))
    proposed = [c for c in (gen.get("clauses") or [])
                if (c.get("text") or "").strip()
                and c.get("targets") in active_ids
                and f"(route to {c.get('targets')})" in (c.get("text") or "")
                and c["text"].strip() not in me["not_when"]]
    print(f"\nproposed clauses: {len(proposed)}")
    for c in proposed:
        print(f"  + {c['text']}\n      covers: {c.get('covers','')}")
    if not proposed:
        print("nothing valid proposed — stop."); return

    # 2. JUDGE
    judged = await _ask(gw, judge_model, _JUDGE_SYS, json.dumps({
        "description": me["description"], "use_when": me["use_when"],
        "existing_not_when": me["not_when"], "proposed_clauses": proposed,
        "evidence": evidence, "known_agent_ids": sorted(active_ids)},
        ensure_ascii=False))
    proposed_texts = {c["text"].strip() for c in proposed}
    accepted = [a for a in (judged.get("accepted") or [])
                if (a.get("text") or "").strip() in proposed_texts
                and a.get("targets") in active_ids][:_MAX_NEW]
    verdict = str(judged.get("verdict") or "no_improvement")
    print(f"\n=== LLM-as-judge ({judge_model}) ===  verdict={verdict}  score={judged.get('score')}")
    print(f"  rationale: {judged.get('rationale','')}")
    print(f"  accepted ({len(accepted)}):")
    for a in accepted:
        print(f"    ✅ {a['text']}")
    for r in (judged.get("rejected") or []):
        print(f"    ❌ {r.get('text','')}  — {r.get('reason','')}")

    # 3. APPLY (gated)
    ok = verdict == "better" and bool(accepted)
    print(f"\n=== gate ===  {'PASS' if ok else 'HOLD'}  (apply={'on' if args.apply else 'off/dry-run'})")
    if args.apply and ok:
        me["skill"]["not_when"] = me["not_when"] + [a["text"].strip() for a in accepted]
        me["path"].write_text(json.dumps(me["card"], indent=2, ensure_ascii=False) + "\n")
        print(f"  ✏  appended {len(accepted)} not_when clause(s) to {me['path'].name} "
              f"(now {len(me['skill']['not_when'])})")
        print("  next: .venv/bin/python database/agent/sync.py  → content_hash flips, "
              "embeddings UNCHANGED (not_when isn't embedded); then routing_eval to verify")
    elif args.apply and not ok:
        print("  judge did not approve — nothing written.")
    else:
        print("  dry-run — nothing written. Re-run with --apply once you're happy.")


if __name__ == "__main__":
    asyncio.run(main())
