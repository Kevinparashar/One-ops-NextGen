"""Skill-card CONTRACT lint — the production quality bar, enforced as a gate.

`test_agent_skills.py` locks that the schema validates and the cards EXIST.
This file locks that every declared skill card meets the *routing-quality*
contract we author cards to (see docs/architecture/agent-skills-spec.md and the 11-field
authoring checklist):

  - a routing description rich enough to embed/disambiguate on (not a stub),
    but within the schema ceiling,
  - scope (`use_when`) and out-of-scope (`not_when`) both present and plural,
  - enough trigger `examples` to be illustrative without becoming a phrase
    catalogue (§2.1),
  - tags present,
  - REFERENTIAL INTEGRITY: every "route to <agent_id>" hint inside `not_when`
    names a real active agent. This closes the one gap called out when the
    cards were authored — prose agent-references were not integrity-checked, so
    a renamed/removed agent could leave a stale cross-wire silently.

The thresholds are deliberately the floor, not the current values, so they
gate regressions (a card stripped back to a stub) without churning on every
edit. Data-driven over `list_active()` so it auto-covers new UCs at scale.
"""
from __future__ import annotations

import re

import pytest

from oneops.registry.loader import load_registry

# Contract floor — the minimum a production card must carry.
_MIN_DESC = 40          # below this is a stub, not a routing description
_MAX_DESC = 600         # Skill.description schema ceiling
_MIN_USE_WHEN = 3
_MIN_NOT_WHEN = 3
_MIN_EXAMPLES = 5
_MIN_TAGS = 3
# Ceilings (Guard ①): a card past these is a phrase catalogue (§2.1), not a
# routing description — it bloats the disambiguator prompt and widens the card's
# retrieval radius so it vacuums queries meant for sibling agents. doc2query
# enforces the same numbers at enrichment time (DOC2QUERY_MAX_*).
_MAX_USE_WHEN = 8
_MAX_EXAMPLES = 12

# Matches the cross-wire convention used in not_when: "... (route to uc03_kb_lookup)".
_ROUTE_REF = re.compile(r"route to ([a-z][a-z0-9_]+)")


@pytest.fixture(scope="module")
def registry():
    return load_registry()


@pytest.fixture(scope="module")
def carded_agents(registry):
    """Active agents that declare at least one skill card."""
    return [a for a in registry.agents.list_active() if a.skills]


def test_some_agents_declare_skill_cards(carded_agents):
    # Guards the fixture itself: if this empties out, every per-card test would
    # vacuously pass. We expect the five active UCs to carry cards today.
    assert len(carded_agents) >= 5


def test_every_card_meets_the_contract_floor(carded_agents):
    failures: list[str] = []
    for a in carded_agents:
        for s in a.skills:
            tag = f"{a.id}/{s.id}"
            if not (s.name and s.name.strip()):
                failures.append(f"{tag}: empty name")
            if not (_MIN_DESC <= len(s.description) <= _MAX_DESC):
                failures.append(
                    f"{tag}: description {len(s.description)}c "
                    f"(want {_MIN_DESC}-{_MAX_DESC})")
            if not (_MIN_USE_WHEN <= len(s.use_when) <= _MAX_USE_WHEN):
                failures.append(f"{tag}: use_when={len(s.use_when)} (want {_MIN_USE_WHEN}-{_MAX_USE_WHEN})")
            if len(s.not_when) < _MIN_NOT_WHEN:
                failures.append(f"{tag}: not_when={len(s.not_when)} (want >={_MIN_NOT_WHEN})")
            if not (_MIN_EXAMPLES <= len(s.examples) <= _MAX_EXAMPLES):
                failures.append(f"{tag}: examples={len(s.examples)} (want {_MIN_EXAMPLES}-{_MAX_EXAMPLES})")
            if len(s.tags) < _MIN_TAGS:
                failures.append(f"{tag}: tags={len(s.tags)} (want >={_MIN_TAGS})")
    assert not failures, "skill-card contract violations:\n  " + "\n  ".join(failures)


def test_not_when_route_refs_resolve_to_active_agents(registry, carded_agents):
    """Every 'route to <agent_id>' in a not_when entry names a real active agent.

    This is the integrity check that prose cross-wires lacked. Generic targets
    (e.g. 'route to an action/fulfilment agent') carry no canonical id and are
    intentionally skipped — only id-shaped references are validated.
    """
    active_ids = {a.id for a in registry.agents.list_active()}
    failures: list[str] = []
    for a in carded_agents:
        for s in a.skills:
            for clause in s.not_when:
                for ref in _ROUTE_REF.findall(clause):
                    # id-shaped refs only: real agent ids contain an underscore
                    # and a digit (ucNN_*). Prose nouns like "an" won't match
                    # this shape, so we don't false-positive on them.
                    if not re.search(r"\d", ref) and "_" not in ref:
                        continue
                    if ref not in active_ids:
                        failures.append(
                            f"{a.id}/{s.id}: not_when routes to '{ref}' "
                            f"which is not an active agent")
    assert not failures, "stale not_when cross-wires:\n  " + "\n  ".join(failures)


def test_every_card_declares_domain_explicitly():
    """Every agent card must EXPLICITLY declare `domain` in each version body.

    The model defaults `domain` to 'itsm', so an ITOM card that forgets the tag
    would silently default to 'itsm' and be mis-scoped at retrieval. This gate
    reads the RAW card files (not the defaulted model) so the missing key is
    caught at authoring time. Auto-covers every future ITOM card.
    """
    import glob
    import json
    import os

    root = os.getenv("REGISTRY_ROOT", "registries/v2")
    files = sorted(glob.glob(os.path.join(root, "agents", "*.json")))
    assert files, f"no agent cards under {root}/agents"
    failures: list[str] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            card = json.loads(fh.read())
        for vnum, body in (card.get("versions") or {}).items():
            if not (str(body.get("domain") or "").strip()):
                failures.append(f"{os.path.basename(f)} v{vnum}: missing explicit 'domain' tag")
    assert not failures, (
        "cards missing explicit domain tag (declare \"domain\": \"itsm\"/\"itom...\"):\n  "
        + "\n  ".join(failures))


def test_card_examples_do_not_duplicate_not_when_text(carded_agents):
    """Examples are trigger phrases; not_when are exclusions. A card that lists
    the same string in both is self-contradicting. Cheap guard against copy
    mistakes during authoring."""
    failures: list[str] = []
    for a in carded_agents:
        for s in a.skills:
            ex = {e.strip().lower() for e in s.examples}
            nw = {n.strip().lower() for n in s.not_when}
            overlap = ex & nw
            if overlap:
                failures.append(f"{a.id}/{s.id}: {sorted(overlap)} in BOTH examples and not_when")
    assert not failures, "example/not_when contradictions:\n  " + "\n  ".join(failures)
