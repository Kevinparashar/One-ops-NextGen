"""UC-3 answer composer — streaming path (ONEOPS_STREAM_ANSWERS).

When the flag is on the composer streams the answer via gateway.call_stream,
publishing each delta to the turn's event sink as a live preview, while the
validated text stays authoritative. Flag off = the blocking call() path
(covered elsewhere). Hermetic: a fake gateway, no network.
"""
from __future__ import annotations

from oneops.llm.models import LlmResponse, LlmStreamChunk
from oneops.observability.event_sink import close_sink, open_sink
from oneops.use_cases.uc03_kb_lookup.answer_composer import LlmAnswerComposer

_ARTICLES = [{"kb_id": "KB0005001", "title": "Fix VPN", "summary": "vpn",
              "content": "Update the VPN client profile.", "relevance_score": 0.9}]
_ANSWER = "To fix VPN, update the client profile. Source: KB0005001"


class _StreamingGateway:
    """Fake gateway whose call_stream yields the answer in word deltas + a
    terminal chunk carrying the finalized LlmResponse."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def call_stream(self, request):
        parts = self._text.split(" ")
        for i, p in enumerate(parts):
            piece = p if i == len(parts) - 1 else p + " "
            yield LlmStreamChunk(delta=piece)
        yield LlmStreamChunk(done=True, response=LlmResponse(
            content=self._text, model=request.model, prompt_tokens=10,
            completion_tokens=12, cost_usd=0.0, latency_ms=1))

    async def call(self, request):  # not used when streaming flag is on
        raise AssertionError("call() must not run on the streaming path")


async def test_streaming_publishes_tokens_and_returns_validated_text(monkeypatch):
    monkeypatch.setenv("ONEOPS_STREAM_ANSWERS", "1")
    composer = LlmAnswerComposer(_StreamingGateway(_ANSWER))

    rid = "req-stream-1"
    q = open_sink(rid)
    try:
        text = await composer.compose(
            query="how do I fix vpn", articles=_ARTICLES,
            tenant_id="t", user_id="u", request_id=rid)
    finally:
        close_sink(rid)

    # authoritative text returned
    assert "update the client profile" in text.lower()
    # token events were published to the sink (live preview)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    tokens = [e for e in events if e.get("type") == "token"]
    assert len(tokens) > 1
    assert "".join(t["text"] for t in tokens).strip() == _ANSWER


async def test_flag_off_uses_blocking_call(monkeypatch):
    """With the flag off, the streaming gateway's call() is used — and our fake
    raises if call() runs, so we assert the blocking path is taken via a
    call-only gateway."""
    monkeypatch.delenv("ONEOPS_STREAM_ANSWERS", raising=False)

    class _BlockingGateway:
        async def call(self, request):
            return LlmResponse(content=_ANSWER, model=request.model,
                               prompt_tokens=10, completion_tokens=12,
                               cost_usd=0.0, latency_ms=1)

    composer = LlmAnswerComposer(_BlockingGateway())
    text = await composer.compose(
        query="how do I fix vpn", articles=_ARTICLES,
        tenant_id="t", user_id="u", request_id="req-block-1")
    assert "update the client profile" in text.lower()
