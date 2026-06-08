"""doc2query agent-card enrichment, gated by an LLM-as-judge.

Pipeline (per agent):
  1. GENERATE  — LLM drafts varied, user-style phrasings the agent SHOULD handle
                 (doc2query): different vocabulary, casual + formal, NO record ids,
                 in-scope only (must not fall under the card's not_when).
  2. JUDGE     — a SECOND LLM call cross-verifies the enriched card is genuinely
                 BETTER than the original: it accepts on-target/novel phrasings,
                 rejects duplicates/off-target/not_when-violating ones, and gives
                 an overall verdict (better | no_improvement | worse) + score.
  3. APPLY     — ONLY with --apply AND verdict=better: writes the accepted
                 examples into the card file (registries/v2/agents/<id>.json).
                 The sync + re-embed pipeline is a SEPARATE, explicit step you
                 run afterwards (database/agent/sync.py) — nothing is triggered
                 here. Default is DRY-RUN: preview + verdict, no writes.

Run:
  .venv/bin/python scripts/agent_doc2query.py --agent uc02_similar_tickets
  .venv/bin/python scripts/agent_doc2query.py --agent uc02_similar_tickets --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
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

# Embedding model for the semantic-novelty filter — MUST match the routing
# substrate (database/agent/worker.py + router/retrieval.py) so "novel vs
# existing" is measured in the same space the retriever will actually use.
_EMB_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-large")
_EMB_DIM = 1536

# Deterministic record-id bouncer. Real ITSM ids are 2-4 letters glued to 4+
# digits (INC0001003, REQ0002002, CHG0003003, CI0004004). A card teaches the
# SHAPE of a request ("this incident"), never a specific record — any draft
# carrying an id is torn up before it can reach the judge. The prompt asks for
# this too (§ generator rules), but a rule the LLM can ignore is not a guarantee.
_RECORD_ID_RE = re.compile(r"\b[A-Za-z]{2,4}\d{4,}\b")

# Above this cosine similarity a draft is a semantic near-duplicate (a reword,
# not new vocabulary) of something the field already covers — the judge rejects
# these as "similar to existing", so we drop them before spending a judge call.
_NOVELTY_MAX_COS = float(os.getenv("DOC2QUERY_NOVELTY_MAX_COS", "0.90"))

# Guard ① — card-size ceilings. The contract has a FLOOR (>=3 use_when, >=5
# examples) but no roof; without one, repeated enrichment turns a card into a
# phrase catalogue (§2.1) that bloats the disambiguator prompt and widens the
# card's retrieval radius so it vacuums queries meant for sibling agents.
_MAX_USE_WHEN = int(os.getenv("DOC2QUERY_MAX_USE_WHEN", "8"))
_MAX_EXAMPLES = int(os.getenv("DOC2QUERY_MAX_EXAMPLES", "12"))

# Guard ② — description schema window (Skill.description is 40-600 chars). A
# sharpened description must stay inside it.
_MIN_DESC, _MAX_DESC = 40, 600

# Guard ③ — cross-agent collision ceiling. A proposed chunk this similar to
# ANOTHER agent's existing chunk would make the two agents confusable at
# retrieval/disambiguation time — so we refuse to write it, even if it is novel
# to THIS card and the judge liked it. This is the check the novelty filter
# (which only compares against the card's OWN entries) cannot make.
_OVERLAP_MAX_COS = float(os.getenv("DOC2QUERY_OVERLAP_MAX_COS", "0.88"))


def _pg_url() -> str:
    m = re.search(r"^POSTGRES_URL=(.+)$", (_ROOT / ".env").read_text(), re.M)
    if not m:
        raise SystemExit("POSTGRES_URL not found in .env")
    return m.group(1).strip().strip('"').strip("'")


def _strip_ids(items: list[str]) -> tuple[list[str], list[str]]:
    """Split drafts into (clean, dropped-for-carrying-a-record-id)."""
    clean, dropped = [], []
    for it in items:
        (dropped if _RECORD_ID_RE.search(it or "") else clean).append(it)
    return clean, dropped


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _filter_novel(
    gw: LlmGateway, existing: list[str], drafts: list[str],
) -> tuple[list[str], list[str]]:
    """Keep only drafts semantically distant from every existing entry AND from
    each other. Returns (kept, dropped-as-near-duplicate). Measured in the same
    embedding space the retriever uses, so "novel" means novel to retrieval."""
    if not drafts:
        return [], []
    try:
        vecs = await gw.embed(existing + drafts, model=_EMB_MODEL,
                              tenant_id="_platform", user_id="doc2query",
                              dimensions=_EMB_DIM)
    except Exception as e:                                        # noqa: BLE001
        # Transient embed outage must NOT kill the enrichment. Degrade openly:
        # skip the novelty cut this run and let the LLM judge be the backstop.
        print(f"  ⚠ novelty filter unavailable ({type(e).__name__}: {e}); "
              f"passing {len(drafts)} draft(s) through to the judge unfiltered")
        return drafts, []
    ex_vecs = vecs[: len(existing)]
    kept, kept_vecs, dropped = [], [], []
    for i, d in enumerate(drafts):
        dv = vecs[len(existing) + i]
        if any(_cos(dv, ev) >= _NOVELTY_MAX_COS for ev in ex_vecs) \
           or any(_cos(dv, kv) >= _NOVELTY_MAX_COS for kv in kept_vecs):
            dropped.append(d)
        else:
            kept.append(d)
            kept_vecs.append(dv)
    return kept, dropped


async def _overlap_against_others(
    gw: LlmGateway, agent_id: str, proposed: list[str],
) -> dict[str, tuple[str, float]]:
    """For each proposed chunk text, find its nearest chunk belonging to ANOTHER
    agent and return {text: (neighbor_agent_id, cosine)} for those at/above the
    collision ceiling. Uses pgvector ANN (HNSW) so it stays cheap at 100s of UCs.

    Degrades openly: if embeddings/DB are unavailable this CANNOT silently pass a
    collision — it warns and returns {} (caller treats as 'unverified', and the
    standalone agent_overlap_report.py remains the backstop)."""
    if not proposed:
        return {}
    try:
        import asyncpg
        vecs = await gw.embed(proposed, model=_EMB_MODEL, tenant_id="_platform",
                              user_id="doc2query", dimensions=_EMB_DIM)
        conn = await asyncpg.connect(_pg_url())
        try:
            hits: dict[str, tuple[str, float]] = {}
            for text, v in zip(proposed, vecs):
                vec_lit = "[" + ",".join(repr(float(x)) for x in v) + "]"
                row = await conn.fetchrow(
                    "SELECT agent_id, 1 - (embedding <=> $1::vector) AS sim "
                    "FROM ai.embeddings_agent WHERE agent_id <> $2 "
                    "ORDER BY embedding <=> $1::vector LIMIT 1",
                    vec_lit, agent_id)
                if row and row["sim"] >= _OVERLAP_MAX_COS:
                    hits[text] = (row["agent_id"], float(row["sim"]))
            return hits
        finally:
            await conn.close()
    except Exception as e:                                        # noqa: BLE001
        print(f"  ⚠ overlap gate could not verify ({type(e).__name__}: {e}); "
              f"run scripts/agent_overlap_report.py before trusting this enrichment")
        return {}


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
        model=model, tenant_id="_platform", user_id="doc2query",
        response_format=ResponseFormat.JSON, request_id="doc2query"))
    return json.loads(resp.content)


_GEN_SYS = """You expand an ITSM/ITOM agent card (doc2query) so it covers how \
real users phrase requests. You enrich THREE fields:

- description: ONE sharpened paragraph (40-600 chars) that says what THIS agent \
  does AND, contrastively, what it does NOT do, so it reads distinctly from \
  sibling agents. Propose an improved description ONLY if you can make it more \
  DISTINCTIVE than the current one; otherwise return "" (empty) to keep it.
- use_when: abstract SCOPE conditions describing when this agent applies, phrased \
  as "the user wants ..." / "a ... request about ...". (NOT example queries.)
- examples: concrete EXAMPLE USER QUERIES, the way a user would actually type them.

Rules for ALL:
- Varied vocabulary (casual + formal, synonyms, paraphrases), DIFFERENT from what \
already exists in that field.
- Clearly IN SCOPE — never write anything that matches the card's not_when \
(those belong to other agents).
- NO specific record ids (write generically, e.g. "this incident", not "INC0001234").
- No duplicates or trivial rewordings of existing entries.
Output strict JSON: {"description": "<improved or empty>", "use_when": ["...", \
"..."], "examples": ["...", "..."]}"""

_JUDGE_SYS = """You are a STRICT reviewer deciding whether proposed additions \
genuinely improve an agent card. Be skeptical — reject freely. You review two \
fields: use_when (scope conditions) and examples (user queries).

ACCEPT an item only if ALL hold:
- right kind: a use_when item is an abstract scope condition; an example is a \
  concrete user query. Reject items in the wrong field.
- on-target: it clearly belongs to THIS agent,
- novel: covers a phrasing or facet not already covered by that field,
- in-scope: does NOT fall under the card's not_when (would route elsewhere),
- clean: carries no specific record id, not a trivial reword.
REJECT otherwise, with a short reason.

CRITICAL — generic record references are CORRECT, never a defect. Cards must be \
GENERIC: at runtime the focused record's id comes from session context, NOT from \
the sentence. So phrasings like "this incident", "this ticket", "this asset", \
"the request" are exactly right. NEVER reject an item for being "too vague" or \
"not specifying which record" — that would contradict the no-record-id rule. A \
present record id is the only id-related reason to reject.

DESCRIPTION — a proposed description is offered only when the generator thinks it \
can sharpen the headline. Accept it (echo it back in accepted_description) ONLY if \
it is MORE DISTINCTIVE than the current one (clearer about what this agent does \
and does not do), stays 40-600 chars, and does not contradict not_when. Otherwise \
return accepted_description as "" to keep the current description.

CONSISTENCY — each proposed item must appear in EXACTLY ONE of accepted_* or \
rejected (never both). The verdict must FOLLOW from the accepts:
  "better"         -> you accepted >=1 genuinely novel, on-target item (or a sharper description),
  "no_improvement" -> you accepted nothing worth adding,
  "worse"          -> the additions add noise / blur scope.

Output strict JSON:
{"accepted_description": "<improved text, or empty to keep current>",
 "accepted_use_when": ["..."],
 "accepted_examples": ["..."],
 "rejected": [{"field":"description|use_when|examples","item":"...","reason":"..."}],
 "verdict": "better|no_improvement|worse",
 "score": 0.0-1.0,
 "rationale": "<one sentence>"}"""


def _skill(body: dict) -> dict:
    skills = body.get("skills") or []
    if not skills:
        raise SystemExit("agent has no skills to enrich")
    return skills[0]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--n", type=int, default=8, help="how many phrasings to draft")
    ap.add_argument("--judge-model", default=None,
                    help="model for the judge (defaults to DOC2QUERY_JUDGE_MODEL "
                         "or gpt-4o — a capable judge gives a stable verdict)")
    ap.add_argument("--enrich-description", action="store_true",
                    help="ALSO propose a description rewrite (OFF by default: a "
                         "rewrite can regress a sharp description into something "
                         "wordier that the judge still scores 'better' — keep it "
                         "human-reviewed). use_when + examples are always enriched.")
    ap.add_argument("--apply", action="store_true",
                    help="write accepted examples to the card IF the judge says 'better'")
    args = ap.parse_args()

    f = _AGENTS / f"{args.agent}.json"
    if not f.exists():
        raise SystemExit(f"no card: {f}")
    card = json.loads(f.read_text())
    body = card["versions"][str(card["active_version"])]
    skill = _skill(body)
    existing_uw = list(skill.get("use_when") or [])
    existing_ex = list(skill.get("examples") or [])

    gw = _gateway()
    model = os.getenv("LLM_DEFAULT_MODEL", "gpt-4o-mini").strip()
    # LLM-as-judge runs on a stronger model than the generator: a weak judge
    # gives noisy, self-contradicting verdicts (accepts items yet votes
    # no_improvement), which swings the gate run-to-run. Stable verdict > cheap.
    judge_model = (args.judge_model
                   or os.getenv("DOC2QUERY_JUDGE_MODEL", "gpt-4o")).strip()

    def _dedupe(items: list, against: list) -> list:
        seen = {x.strip().lower() for x in against}
        out = []
        for it in items:
            s = (it or "").strip()
            if s and s.lower() not in seen:
                out.append(s); seen.add(s.lower())
        return out

    # 1. GENERATE both use_when (scope) and examples (queries)
    gen = await _ask(gw, model, _GEN_SYS, json.dumps({
        "name": skill.get("name"), "description": skill.get("description"),
        "use_when": existing_uw, "not_when": skill.get("not_when"),
        "examples": existing_ex, "how_many_each": args.n}, ensure_ascii=False))
    draft_uw = _dedupe(gen.get("use_when") or [], existing_uw)
    draft_ex = _dedupe(gen.get("examples") or [], existing_ex)
    existing_desc = (skill.get("description") or "").strip()
    draft_desc = (gen.get("description") or "").strip()
    if not args.enrich_description:
        draft_desc = ""                       # description rewrite is opt-in
    elif draft_desc.lower() == existing_desc.lower():
        draft_desc = ""                       # no change proposed

    # Bouncer #1 — deterministic record-id strip (before any judge spend).
    draft_uw, id_uw = _strip_ids(draft_uw)
    draft_ex, id_ex = _strip_ids(draft_ex)
    # Bouncer #2 — semantic-novelty strip (reworded near-duplicates).
    draft_uw, dup_uw = await _filter_novel(gw, existing_uw, draft_uw)
    draft_ex, dup_ex = await _filter_novel(gw, existing_ex, draft_ex)

    print(f"=== doc2query: {args.agent} ===")
    if id_uw or id_ex:
        print(f"  ✂ dropped {len(id_uw) + len(id_ex)} draft(s) carrying record ids "
              f"(use_when={len(id_uw)}, examples={len(id_ex)}):")
        for e in (id_uw + id_ex):
            print(f"      ⛔ {e}")
    if dup_uw or dup_ex:
        print(f"  ✂ dropped {len(dup_uw) + len(dup_ex)} near-duplicate draft(s) "
              f">= {_NOVELTY_MAX_COS} cos (use_when={len(dup_uw)}, examples={len(dup_ex)}):")
        for e in (dup_uw + dup_ex):
            print(f"      ≈ {e}")
    if draft_desc:
        print(f"description: proposed rewrite ({len(draft_desc)} chars)")
        print(f"  ~ (description) {draft_desc}")
    print(f"use_when: {len(existing_uw)} existing, {len(draft_uw)} drafted")
    for e in draft_uw:
        print(f"  + (use_when) {e}")
    print(f"examples: {len(existing_ex)} existing, {len(draft_ex)} drafted")
    for e in draft_ex:
        print(f"  + (example)  {e}")
    if not (draft_uw or draft_ex or draft_desc):
        print("nothing new drafted — stop."); return

    # 2. JUDGE (cross-verify the enrichment is genuinely better) — all 3 fields
    judged = await _ask(gw, judge_model, _JUDGE_SYS, json.dumps({
        "current_description": existing_desc, "not_when": skill.get("not_when"),
        "proposed_description": draft_desc,
        "existing_use_when": existing_uw, "existing_examples": existing_ex,
        "proposed_use_when": draft_uw, "proposed_examples": draft_ex},
        ensure_ascii=False))
    acc_uw = [e.strip() for e in (judged.get("accepted_use_when") or [])
              if e.strip() and e.strip() in draft_uw]
    acc_ex = [e.strip() for e in (judged.get("accepted_examples") or [])
              if e.strip() and e.strip() in draft_ex]
    # Guard ② — a sharpened description is accepted only if the judge echoed one
    # AND it sits inside the schema window. Outside the window => keep current.
    acc_desc = (judged.get("accepted_description") or "").strip()
    if acc_desc and not (_MIN_DESC <= len(acc_desc) <= _MAX_DESC):
        print(f"    ⚠ proposed description {len(acc_desc)}c outside {_MIN_DESC}-{_MAX_DESC}; keeping current")
        acc_desc = ""
    if acc_desc and acc_desc.lower() == existing_desc.lower():
        acc_desc = ""
    verdict = str(judged.get("verdict") or "no_improvement")
    print(f"\n=== LLM-as-judge ({judge_model}) ===  verdict={verdict}  score={judged.get('score')}")
    print(f"  rationale: {judged.get('rationale','')}")
    if acc_desc:
        print(f"  accepted description ({len(acc_desc)}c): {acc_desc}")
    print(f"  accepted use_when ({len(acc_uw)}):")
    for e in acc_uw:
        print(f"    ✅ {e}")
    print(f"  accepted examples ({len(acc_ex)}):")
    for e in acc_ex:
        print(f"    ✅ {e}")
    for r in (judged.get("rejected") or []):
        print(f"    ❌ [{r.get('field','?')}] {r.get('item','')}  — {r.get('reason','')}")

    # Guard ① — CEILING. Never let the card grow past the roof; trim the lowest
    # priority (judge listed strongest first) and say what was dropped.
    uw_room = max(0, _MAX_USE_WHEN - len(existing_uw))
    ex_room = max(0, _MAX_EXAMPLES - len(existing_ex))
    cap_uw, cap_ex = acc_uw[uw_room:], acc_ex[ex_room:]
    acc_uw, acc_ex = acc_uw[:uw_room], acc_ex[:ex_room]
    if cap_uw or cap_ex:
        print(f"\n=== guard ① ceiling ===  use_when<= {_MAX_USE_WHEN}, examples<= {_MAX_EXAMPLES}")
        for e in cap_uw + cap_ex:
            print(f"    ⤵ over ceiling, not added: {e}")

    # Guard ③ — OVERLAP vs OTHER agents. Refuse any accepted chunk that would
    # make this agent confusable with another (the novelty filter only checked
    # against THIS card). Drop the specific colliders; keep the rest.
    candidates = ([acc_desc] if acc_desc else []) + acc_uw + acc_ex
    collisions = await _overlap_against_others(gw, args.agent, candidates)
    if collisions:
        print(f"\n=== guard ③ overlap ===  flag >= {_OVERLAP_MAX_COS} cos vs another agent")
        for text, (nbr, sim) in collisions.items():
            print(f"    ✋ {sim:.3f} ~ {nbr}: {text}")
        if acc_desc in collisions:
            acc_desc = ""
        acc_uw = [e for e in acc_uw if e not in collisions]
        acc_ex = [e for e in acc_ex if e not in collisions]

    # 3. APPLY (only if judge approves + survives the guards + --apply).
    ok = verdict == "better" and bool(acc_desc or acc_uw or acc_ex)
    print(f"\n=== gate ===  {'PASS' if ok else 'HOLD'}  (apply={'on' if args.apply else 'off/dry-run'})")
    if args.apply and ok:
        if acc_desc:
            skill["description"] = acc_desc
        if acc_uw:
            skill["use_when"] = existing_uw + acc_uw
        if acc_ex:
            skill["examples"] = existing_ex + acc_ex
        f.write_text(json.dumps(card, indent=2, ensure_ascii=False) + "\n")
        print(f"  ✏  wrote {'description, ' if acc_desc else ''}+{len(acc_uw)} use_when, "
              f"+{len(acc_ex)} examples to {f.name} "
              f"(now {len(skill.get('use_when', []))} use_when, {len(skill.get('examples', []))} examples)")
        print("  next (separate, explicit): .venv/bin/python database/agent/sync.py  "
              "→ trigger → worker re-embeds")
    elif args.apply and not ok:
        print("  judge did not approve (or guards stripped everything) — nothing written.")
    else:
        print("  dry-run — nothing written. Re-run with --apply once you're happy.")


if __name__ == "__main__":
    asyncio.run(main())
