"""Production-grade seed for `uc_capabilities`.

Source-of-truth precedence (LATER overrides EARLIER):

  1. registries/agent-registry.json + capability-registry.json
     — populates rows for agents whose handlers haven't been built yet
       (future UCs). These rows are inserted with `active=false` so the
       shortlister doesn't return them. They become `active=true` the
       moment a handler is registered (via the autodiscover self-heal).

  2. Live handler manifests from `oneops.invoker.base._handlers`
     — AUTHORITATIVE for agents with code. Replaces / supplements rows
       from step 1 with the actual capability_to_intent keys the handler
       exposes. Inserted with `active=true`.

This split prevents the over-generation problem: JSON registry sometimes
declares per-op rows (`read`, `action`) that aren't real routing
capabilities; the handler manifest is the contract the planner uses.

Behaviors:
  - DEFAULT  : insert missing rows; never overwrite existing principle text
               (authors' edits to principle_description survive re-seed).
  - --force  : full rewrite — overwrites every column, sets embedding=NULL
               (Phase 2 must re-embed).
  - --dry-run: print proposed rows, write nothing.

Run:
    cd "POC copy 4"
    .venv/bin/python -u tools/seed_uc_capabilities.py
    .venv/bin/python -u tools/seed_uc_capabilities.py --force
    .venv/bin/python -u tools/seed_uc_capabilities.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
load_dotenv(_ROOT / ".env")

from oneops.routing.uc_capability_catalog import (  # noqa: E402
    apply_migration,
    validate_uc_catalog_against_handlers,
)


def _load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _principle_from_registry(agent: dict, capability: dict, services: list[str], intents: list[str]) -> str:
    """Semantic mission text built from JSON-registry descriptions."""
    parts = []
    agent_desc = (agent.get("description") or "").strip()
    cap_desc = (capability.get("description") or "").strip()
    if agent_desc: parts.append(f"AGENT MISSION: {agent_desc}")
    if cap_desc:   parts.append(f"CAPABILITY: {cap_desc}")
    if services:   parts.append(f"SUPPORTED SERVICES: {', '.join(services)}")
    if intents:    parts.append(f"SUPPORTED INTENTS: {', '.join(intents)}")
    text = "\n\n".join(parts)
    if len(text) < 80:
        text += "\n\n" + ("." * (80 - len(text)))
    return text


def _principle_from_handler(agent_id: str, uc_id: str, cap_id: str,
                            services: list[str], intents: list[str],
                            agent_desc: str = "") -> str:
    """Semantic mission text for programmatically-registered handlers
    (no JSON entry). Authors override by editing the row after seed."""
    parts = [
        f"AGENT MISSION: {agent_desc or f'{agent_id} ({uc_id}) — programmatically registered.'}",
        f"CAPABILITY: {cap_id} — routing key declared by the handler's "
        f"capability_to_intent map. The planner emits PlanSteps with this "
        f"capability_id when routing to this agent.",
    ]
    if services: parts.append(f"SUPPORTED SERVICES: {', '.join(services)}")
    if intents:  parts.append(f"SUPPORTED INTENTS: {', '.join(intents)}")
    text = "\n\n".join(parts)
    if len(text) < 80:
        text += "\n\n" + ("." * (80 - len(text)))
    return text


def _exec_type_from_ops(ops: list[str]) -> str:
    has_action = "action" in ops
    has_read = any(o in ops for o in ("read", "summary", "field_read", "lookup_kb"))
    if has_action and has_read: return "mixed"
    if has_action:              return "action"
    return "read"


# Curated semantic descriptions for programmatic-only UCs. Editing these
# is the human-author intervention point. Future programmatic UCs append here.
# Curated principle overrides — PURE SEMANTIC MISSIONS, no phrase catalogs.
# Each describes what the capability IS and its boundary with adjacent
# capabilities. Per `feedback_descriptions_principle_not_phrases`:
# describing user phrasings here would make retrieval brittle to paraphrase.
_PROGRAMMATIC_UC_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "conversational_agent": {
        "conversational": (
            "Responds to messages whose ONLY purpose is social or interpersonal: "
            "opening a conversation, closing one, expressing gratitude or "
            "courtesy, or asking what the assistant itself is built to do as a "
            "product. The defining test: there is nothing in the world to "
            "fetch, nothing to read, no system behavior to troubleshoot, no "
            "issue to resolve, no entity to discuss, no reference material to "
            "retrieve. The moment the user describes a system behavior, names "
            "any technology or product, asks how anything works, asks how to "
            "do or fix anything, or implies any unresolved situation, this "
            "capability does not apply — those belong to data-fetching or "
            "knowledge-retrieval capabilities."
        ),
    },
    "kb_lookup_agent": {
        "kb_lookup": (
            "Retrieves reference information from the published Knowledge Base "
            "to answer the user's information need. Applies whenever the user "
            "is seeking knowledge that lives in documented articles: how a "
            "system or service works, how to configure it, how to use it, how "
            "to troubleshoot symptoms or behaviors, how to resolve a class of "
            "problem, what the policy or procedure is for a task, or general "
            "guidance about a technology, application, or workflow. Strongly "
            "applies to any user message that describes a behavior, symptom, "
            "or unresolved situation involving a technology, product, or "
            "process — because the answer is documented, not stored in a "
            "specific ticket's state. Boundary: distinct from summary (which "
            "reports state of a SPECIFIC named ticket / asset / CI identified "
            "by id), distinct from conversational (which has no information "
            "intent at all)."
        ),
        "find_related_kb": (
            "Returns Knowledge Base articles linked to a named ITSM entity "
            "(incident, problem, change, configuration item). Driven by "
            "explicit linkage in the data, not by semantic similarity. "
            "Boundary: invoked when the user has already identified the "
            "entity and is asking what reference material is associated with "
            "it, distinct from lookup_kb (which finds articles by topic)."
        ),
        "field_read": (
            "Reads a single attribute of a Knowledge Base article in active "
            "focus — its publication state, freshness, authorship, "
            "engagement metrics, or metadata. Boundary: applies only when "
            "the active subject is a KB article; for ITSM records the "
            "equivalent lives on summarization_agent."
        ),
        "get_kb_article": (
            "Returns the full text of a Knowledge Base article identified by "
            "its canonical id. Distinct from lookup_kb (which searches by "
            "topic) — this is invoked when the user has a specific article "
            "id and wants the contents."
        ),
    },
    "summarization_agent": {
        "summary": (
            "Reports the current state, context, and narrative of a SPECIFIC "
            "NAMED ITSM record — an incident, request, problem, change, "
            "asset, or configuration item — that the user has identified by "
            "its canonical id (typically prefixed INC, REQ, SR, PBM, PRB, CHG, "
            "AST, or CI) or that is already in active focus from the prior "
            "turn. Produces a structured narrative covering status, "
            "ownership, history, related entities, and key attributes. "
            "Strongly applies whenever the user references a specific record "
            "by id and wants its details, even if the user's verb is general "
            "('look up', 'pull up', 'show', 'tell me about', 'find', 'open') "
            "— the presence of a canonical record id is the decisive signal. "
            "Boundary: distinct from field_read (which returns one attribute "
            "rather than the full picture), distinct from kb_lookup (which "
            "retrieves reference articles by topic, not state of a record), "
            "distinct from conversational (which has no entity reference)."
        ),
        "uc01_summarize": (
            "Alias for the summary capability — same routing semantics. "
            "Reports the current state and narrative of a SPECIFIC NAMED ITSM "
            "record identified by canonical id (INC, REQ, SR, PBM, PRB, CHG, "
            "AST, CI) or in active focus."
        ),
        "field_read": (
            "Returns ONE specific attribute of a NAMED ITSM record in active "
            "focus or named by id — its priority, status, assigned owner, "
            "SLA, parent problem, related changes, approval state, or any "
            "other single-field reading. Strongly applies when the user "
            "asks a short bare-attribute question following a prior turn "
            "that established focus on an incident, request, problem, "
            "change, asset, or CI. Boundary: distinct from summary (full "
            "picture rather than one attribute), distinct from kb_lookup "
            "(reference article rather than ticket attribute)."
        ),
    },
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pg_url = os.environ.get("POSTGRES_URL")
    if not pg_url:
        print("ERROR: POSTGRES_URL not set", file=sys.stderr); return 1

    agent_reg = _load_json(_ROOT / "registries" / "agent-registry.json")
    cap_reg = _load_json(_ROOT / "registries" / "capability-registry.json")
    agents_by_id = {a["agent_id"]: a for a in agent_reg.get("agents", [])}
    caps_by_id = {c["capability_id"]: c for c in cap_reg.get("capabilities", [])}

    pool = await asyncpg.create_pool(pg_url, min_size=1, max_size=3)
    print("=== Phase 1 seed (production-grade) ===\n")
    await apply_migration(pool)
    print("  ✓ migration applied\n")

    # Discover handler manifests
    try:
        from oneops.use_cases import autodiscover
        autodiscover()
    except Exception as e:  # noqa: BLE001
        print(f"  (autodiscover failed: {e})")
    from oneops.invoker.base import _handlers  # noqa: PLC2701

    # ── Build proposed row set ───────────────────────────────────────
    proposed: list[dict] = []

    # Pass 1 — every registered handler (authoritative for live UCs)
    for agent_id, manifest in _handlers.items():
        uc_id = getattr(manifest, "uc_id", None) or agent_id
        services = sorted(set(manifest.service_prefixes.values())) if manifest.service_prefixes else []
        intents = sorted(manifest.supported_intents) if manifest.supported_intents else []
        cap_to_intent = manifest.capability_to_intent or {}
        cap_ids = sorted(set(cap_to_intent.keys()))
        if not cap_ids:
            cap_ids = [agent_id]   # programmatic agent with no capability_to_intent
        for cap_id in cap_ids:
            json_agent = agents_by_id.get(agent_id, {})
            json_cap = caps_by_id.get(cap_id, {})
            curated = _PROGRAMMATIC_UC_DESCRIPTIONS.get(agent_id, {}).get(cap_id)
            if curated:
                principle = (f"AGENT MISSION: {curated}\n\n"
                             f"CAPABILITY: {cap_id} — routing key for this agent's handler.\n\n"
                             f"SUPPORTED SERVICES: {', '.join(services) or '(none)'}\n\n"
                             f"SUPPORTED INTENTS: {', '.join(intents) or '(none)'}")
            elif json_agent or json_cap:
                principle = _principle_from_registry(json_agent, json_cap, services, intents)
            else:
                principle = _principle_from_handler(agent_id, uc_id, cap_id, services, intents)
            proposed.append({
                "agent_id":  agent_id, "capability_id": cap_id, "uc_id": uc_id,
                "principle_description": principle,
                "supported_services": services, "supported_intents": intents,
                "execution_type": _exec_type_from_ops(intents) if intents else "read",
                "tags": json_cap.get("tags") or [],
                "active": True,
            })

    handler_keys = {(p["agent_id"], p["capability_id"]) for p in proposed}

    # Pass 2 — JSON registry agents that have NO live handler (future UCs)
    # Insert with active=false so they don't appear in shortlist until their
    # handler is registered (autodiscover self-heal will flip active=true).
    for agent_id, agent in agents_by_id.items():
        if agent_id in _handlers:
            continue   # already covered by pass 1
        services = agent.get("supported_services") or []
        ops = agent.get("operation_types") or []
        # JSON registry's `capability_id` field (singular) is the primary one
        primary = agent.get("capability_id")
        candidates = [primary] if primary else []
        # NOTE: we deliberately do NOT add operation_types as capability_ids —
        # operation_types are execution kinds (read/action), not routing keys.
        for cap_id in candidates:
            key = (agent_id, cap_id)
            if key in handler_keys:
                continue
            json_cap = caps_by_id.get(cap_id, {})
            principle = _principle_from_registry(agent, json_cap, services, ops)
            proposed.append({
                "agent_id":  agent_id, "capability_id": cap_id,
                "uc_id":     f"uc_{agent_id.replace('_agent','')}",
                "principle_description": principle,
                "supported_services": services, "supported_intents": ops,
                "execution_type": _exec_type_from_ops(ops),
                "tags": json_cap.get("tags") or [],
                "active": False,   # ← key: not active until handler exists
            })

    print(f"Proposed rows: {len(proposed)} "
          f"({sum(1 for p in proposed if p['active'])} active, "
          f"{sum(1 for p in proposed if not p['active'])} inactive-future)\n")

    if args.dry_run:
        for p in proposed:
            mark = "●" if p["active"] else "○"
            print(f"  {mark} {p['agent_id']:28s} / {p['capability_id']:24s}  exec={p['execution_type']:6s}")
        await pool.close()
        return 0

    # ── Write ─────────────────────────────────────────────────────────
    async with pool.acquire() as c:
        async with c.transaction():
            for p in proposed:
                if args.force:
                    await c.execute(
                        """
                        INSERT INTO uc_capabilities
                          (agent_id, capability_id, uc_id, principle_description,
                           supported_services, supported_intents, execution_type,
                           tags, active, embedding, embedding_updated_at, updated_at)
                        VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8::jsonb,$9,
                                NULL, NULL, now())
                        ON CONFLICT (agent_id, capability_id) DO UPDATE
                        SET uc_id = EXCLUDED.uc_id,
                            principle_description = EXCLUDED.principle_description,
                            supported_services    = EXCLUDED.supported_services,
                            supported_intents     = EXCLUDED.supported_intents,
                            execution_type        = EXCLUDED.execution_type,
                            tags                  = EXCLUDED.tags,
                            active                = EXCLUDED.active,
                            embedding             = NULL,
                            embedding_updated_at  = NULL,
                            updated_at            = now()
                        """,
                        p["agent_id"], p["capability_id"], p["uc_id"],
                        p["principle_description"],
                        json.dumps(p["supported_services"]),
                        json.dumps(p["supported_intents"]),
                        p["execution_type"],
                        json.dumps(p["tags"]),
                        p["active"],
                    )
                else:
                    await c.execute(
                        """
                        INSERT INTO uc_capabilities
                          (agent_id, capability_id, uc_id, principle_description,
                           supported_services, supported_intents, execution_type,
                           tags, active)
                        VALUES ($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7,$8::jsonb,$9)
                        ON CONFLICT (agent_id, capability_id) DO NOTHING
                        """,
                        p["agent_id"], p["capability_id"], p["uc_id"],
                        p["principle_description"],
                        json.dumps(p["supported_services"]),
                        json.dumps(p["supported_intents"]),
                        p["execution_type"],
                        json.dumps(p["tags"]),
                        p["active"],
                    )

    # ── Verify ────────────────────────────────────────────────────────
    n_act = await pool.fetchval("SELECT count(*) FROM uc_capabilities WHERE active=true")
    n_inact = await pool.fetchval("SELECT count(*) FROM uc_capabilities WHERE active=false")
    print(f"After seed: {n_act} active, {n_inact} inactive (future UCs)\n")

    report = await validate_uc_catalog_against_handlers(pool)
    print("Drift check:")
    print(f"  ok:                   {len(report['ok'])}")
    print(f"  missing_handler:      {report['missing_handler']}")
    print(f"  missing_in_catalog:   {report['missing_in_catalog']}")
    print(f"  missing_embedding:    {len(report['missing_embedding'])}  "
          f"(Phase 2 will fill)")

    await pool.close()
    print("\n=== ✓ PHASE 1 SEED COMPLETE ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
