"""S2 — `resolve_reply`: layered, LLM-led resolution of a clarification reply.

Hermetic: a real `EntityIdNormalizer` built from a tiny prefix map (literal
layer) + an injected fake picker (the LLM layer). Covers every branch and the
production guards — the resolver never acts on an id outside the grounded set,
and the picker prompt is a PRINCIPLE, not a phrase catalog (§2.1).
"""
from __future__ import annotations

import pytest

from oneops.executor.entity_elicitation import (
    build_pick_messages,
    parse_pick,
    resolve_reply,
)
from oneops.router.entity_id import EntityIdNormalizer

pytestmark = pytest.mark.asyncio

_NORM = EntityIdNormalizer({"INC": "incident", "REQ": "request"})
_CANDS = [
    {"ticket_id": "INC0000002", "service_id": "incident",
     "title": "Wifi flaky", "status": "open"},
    {"ticket_id": "REQ0000001", "service_id": "request",
     "title": "New laptop", "status": "pending_approval"},
]


def _picker(returns: str):
    calls = {"n": 0}

    async def pick(reply, candidates):     # noqa: ANN001
        calls["n"] += 1
        return returns
    pick.calls = calls                     # type: ignore[attr-defined]
    return pick


async def test_literal_id_resolves_without_llm() -> None:
    pick = _picker("SHOULD_NOT_BE_CALLED")
    out = await resolve_reply(reply="please use INC0000002", candidates=_CANDS,
                              normalizer=_NORM, pick=pick)
    assert out.resolved and out.method == "literal"
    assert out.ticket_id == "INC0000002" and out.service_id == "incident"
    assert pick.calls["n"] == 0            # literal short-circuits the LLM


async def test_contextual_reply_resolved_by_picker() -> None:
    out = await resolve_reply(reply="the wifi one", candidates=_CANDS,
                              normalizer=_NORM, pick=_picker("INC0000002"))
    assert out.resolved and out.method == "llm"
    assert out.ticket_id == "INC0000002" and out.service_id == "incident"


async def test_empty_reply_unresolved() -> None:
    out = await resolve_reply(reply="   ", candidates=_CANDS,
                              normalizer=_NORM, pick=_picker("INC0000002"))
    assert not out.resolved and out.method == "none"


async def test_no_candidates_does_not_guess() -> None:
    pick = _picker("INC0000002")
    out = await resolve_reply(reply="my last ticket", candidates=[],
                              normalizer=_NORM, pick=pick)
    assert not out.resolved and pick.calls["n"] == 0


async def test_picker_blank_is_unresolved() -> None:
    out = await resolve_reply(reply="something vague", candidates=_CANDS,
                              normalizer=_NORM, pick=_picker(""))
    assert not out.resolved


async def test_picker_id_outside_candidate_set_rejected() -> None:
    # The model must never win with a fabricated id (§2.7).
    out = await resolve_reply(reply="the third one", candidates=_CANDS,
                              normalizer=_NORM, pick=_picker("INC9999999"))
    assert not out.resolved
    assert "outside the candidate set" in out.reason


async def test_picker_error_degrades_to_unresolved() -> None:
    async def boom(reply, candidates):     # noqa: ANN001
        raise RuntimeError("gateway down")
    out = await resolve_reply(reply="the open one", candidates=_CANDS,
                              normalizer=_NORM, pick=boom)
    assert not out.resolved and out.reason == "resolver error"


async def test_parse_pick_guards() -> None:
    assert parse_pick('{"ticket_id": "INC0000002"}', _CANDS) == "INC0000002"
    assert parse_pick('{"ticket_id": null}', _CANDS) == ""
    assert parse_pick('{"ticket_id": "INC9999999"}', _CANDS) == ""   # not grounded
    assert parse_pick("not json", _CANDS) == ""
    assert parse_pick("{}", _CANDS) == ""


async def test_pick_prompt_is_principle_not_phrase_catalog() -> None:
    msgs = build_pick_messages("the last one", _CANDS)
    system = msgs[0].content
    # States the resolution PRINCIPLE...
    assert "MEANING" in system and "not by matching keywords" in system
    # ...and grounds on the real candidates in the user turn.
    assert "INC0000002" in msgs[1].content and "Wifi flaky" in msgs[1].content
    # NOT a phrase→value catalog: no hardcoded id mappings, no demo answers.
    assert "INC0000002" not in system and "REQ0000001" not in system
