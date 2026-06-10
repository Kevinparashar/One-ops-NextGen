"""S3 — `elicit_entity`: the ask → resolve → bind orchestrator.

Hermetic: a fake recent-records store + fake picker + an injected `interrupt_fn`
that stands in for the conversational interrupt (raising on the first pass,
returning the user's reply on resume). No gateway, no DB, no LangGraph runtime.
"""
from __future__ import annotations

import pytest

from oneops.executor.entity_elicitation import elicit_entity
from oneops.router.entity_id import EntityIdNormalizer

pytestmark = pytest.mark.asyncio

_NORM = EntityIdNormalizer({"INC": "incident", "REQ": "request"})
_CANDS = [
    {"ticket_id": "INC0000002", "service_id": "incident",
     "title": "Wifi flaky", "status": "open"},
    {"ticket_id": "REQ0000001", "service_id": "request",
     "title": "New laptop", "status": "pending_approval"},
]
_CTX = {"tenant_id": "T001", "user_id": "U1"}


class _Store:
    def __init__(self, cands, *, fail=False):
        self._c = cands
        self._fail = fail

    async def list_recent_for_user(self, *, tenant_id, user_id, limit=5):  # noqa: ANN001
        if self._fail:
            raise RuntimeError("db down")
        return list(self._c)


class _Picker:
    def __init__(self, ret):
        self._r = ret

    async def pick(self, reply, candidates, *, tenant_id="", user_id=""):  # noqa: ANN001
        return self._r


def _resume_with(answer):
    """interrupt_fn that records what it was asked and returns `answer` (the
    resume path — a real interrupt would raise here on the first pass)."""
    seen = {}

    def fn(question, hints):
        seen["question"] = question
        seen["hints"] = hints
        return answer
    fn.seen = seen                         # type: ignore[attr-defined]
    return fn


async def _run(store, picker, interrupt_fn, *, service_param="service_id"):
    return await elicit_entity(
        param_name="ticket_id", service_param=service_param, context=_CTX,
        store=store, normalizer=_NORM, picker=picker, interrupt_fn=interrupt_fn)


async def test_resume_literal_id_binds_ticket_and_service() -> None:
    fn = _resume_with({"answer": "INC0000002"})
    out = await _run(_Store(_CANDS), _Picker(""), fn)
    assert out == {"ticket_id": "INC0000002", "service_id": "incident"}
    # the user's recent ids were offered as hint chips
    assert fn.seen["hints"] == ["INC0000002", "REQ0000001"]


async def test_resume_contextual_reply_resolved_by_picker() -> None:
    out = await _run(_Store(_CANDS), _Picker("INC0000002"),
                     _resume_with({"answer": "the wifi one"}))
    assert out == {"ticket_id": "INC0000002", "service_id": "incident"}


async def test_resume_unresolved_returns_none() -> None:
    out = await _run(_Store(_CANDS), _Picker(""),
                     _resume_with({"answer": "no idea"}))
    assert out is None


async def test_service_param_omitted_when_tool_has_none() -> None:
    out = await _run(_Store(_CANDS), _Picker(""),
                     _resume_with({"answer": "INC0000002"}), service_param="")
    assert out == {"ticket_id": "INC0000002"}


async def test_store_failure_still_asks_and_literal_resolves() -> None:
    fn = _resume_with({"answer": "INC0000002"})
    out = await _run(_Store(_CANDS, fail=True), _Picker(""), fn)
    # degraded: no candidates → empty hints, but a literal id still resolves
    assert out == {"ticket_id": "INC0000002", "service_id": "incident"}
    assert fn.seen["hints"] == []


async def test_first_pass_interrupt_propagates() -> None:
    class _Paused(Exception):
        pass

    def raises(question, hints):
        raise _Paused()
    with pytest.raises(_Paused):
        await _run(_Store(_CANDS), _Picker(""), raises)
