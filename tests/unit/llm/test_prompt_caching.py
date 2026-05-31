"""Prompt-cache wiring (provider-side ephemeral cache).

Verifies the full chain:

  * `LlmMessage.cache_control=True` is the substrate marker.
  * `LiteLLMTransport.complete()` emits the Anthropic content-block format
    with `cache_control: {"type": "ephemeral"}` ONLY when any message
    asks for it — token-identical body otherwise.
  * Cache-token usage fields (`cache_read_input_tokens` /
    `cache_creation_input_tokens`) round-trip from the provider through
    `TransportResult` and `LlmResponse`.
  * `LlmResponse.prompt_cache_hit_ratio` reports the share of input
    tokens served from cache.
"""
from __future__ import annotations

import pytest

from oneops.llm.models import (
    LlmMessage,
    LlmRequest,
    LlmResponse,
)

# ── LlmMessage marker ──────────────────────────────────────────────────


def test_llm_message_cache_control_defaults_false():
    m = LlmMessage(role="user", content="hi")
    assert m.cache_control is False


def test_llm_message_cache_control_can_be_set():
    m = LlmMessage(role="system", content="...", cache_control=True)
    assert m.cache_control is True


# ── LiteLLMTransport body shape ────────────────────────────────────────


class _FakeAsyncResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        import json
        return json.dumps(self._payload)


class _FakeAsyncClient:
    """Records the body of each .post() so we can assert on it."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.last_call: dict = {}

    async def post(self, path, json=None):
        self.last_call = {"path": path, "body": json}
        return _FakeAsyncResponse(200, self._payload)

    async def aclose(self):
        pass


@pytest.fixture
def standard_payload():
    return {
        "choices": [{
            "message": {"content": '{"summary": "ok"}'},
            "finish_reason": "stop",
        }],
        "model": "anthropic/claude-3-5-haiku-latest",
        "usage": {
            "prompt_tokens": 1500,
            "completion_tokens": 80,
            "cache_creation_input_tokens": 1200,
            "cache_read_input_tokens": 0,
        },
    }


async def test_no_cache_control_means_flat_message_body(standard_payload):
    from oneops.llm.transport import LiteLLMTransport
    t = LiteLLMTransport(base_url="http://x", api_key="k")
    t._client = _FakeAsyncClient(standard_payload)               # type: ignore[assignment]
    await t.complete(LlmRequest(
        messages=(LlmMessage("system", "policy text"),
                  LlmMessage("user", "do thing")),
        model="anthropic/claude-3-5-haiku-latest",
        tenant_id="T001",
    ))
    body = t._client.last_call["body"]                            # type: ignore[union-attr]
    # Flat strings — provider gets the legacy shape.
    assert body["messages"][0] == {"role": "system", "content": "policy text"}
    assert body["messages"][1] == {"role": "user", "content": "do thing"}


async def test_cache_control_emits_content_blocks(standard_payload):
    from oneops.llm.transport import LiteLLMTransport
    t = LiteLLMTransport(base_url="http://x", api_key="k")
    t._client = _FakeAsyncClient(standard_payload)               # type: ignore[assignment]
    await t.complete(LlmRequest(
        messages=(LlmMessage("system", "policy text", cache_control=True),
                  LlmMessage("user", "do thing")),
        model="anthropic/claude-3-5-haiku-latest",
        tenant_id="T001",
    ))
    body = t._client.last_call["body"]                            # type: ignore[union-attr]
    # System message switches to content-block shape with cache_control.
    sys_msg = body["messages"][0]
    assert sys_msg["role"] == "system"
    assert isinstance(sys_msg["content"], list)
    assert sys_msg["content"][0]["type"] == "text"
    assert sys_msg["content"][0]["text"] == "policy text"
    assert sys_msg["content"][0]["cache_control"] == {"type": "ephemeral"}
    # User message stays flat (not cached, changes per call).
    assert body["messages"][1] == {"role": "user", "content": "do thing"}


# ── cache-token round-trip from usage ──────────────────────────────────


async def test_cache_creation_tokens_surface_in_transport_result(standard_payload):
    from oneops.llm.transport import LiteLLMTransport
    t = LiteLLMTransport(base_url="http://x", api_key="k")
    t._client = _FakeAsyncClient(standard_payload)               # type: ignore[assignment]
    out = await t.complete(LlmRequest(
        messages=(LlmMessage("system", "...", cache_control=True),
                  LlmMessage("user", "...")),
        model="anthropic/claude-3-5-haiku-latest",
        tenant_id="T001",
    ))
    assert out.cache_creation_input_tokens == 1200
    assert out.cache_read_input_tokens == 0


async def test_cache_read_tokens_surface_on_second_call():
    """Hot prefix → the provider returns `cache_read_input_tokens` on the
    second identical call. The transport surfaces it untouched."""
    from oneops.llm.transport import LiteLLMTransport
    payload_hot = {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "model": "anthropic/claude-3-5-haiku-latest",
        "usage": {
            "prompt_tokens": 1500,
            "completion_tokens": 80,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 1200,
        },
    }
    t = LiteLLMTransport(base_url="http://x", api_key="k")
    t._client = _FakeAsyncClient(payload_hot)                    # type: ignore[assignment]
    out = await t.complete(LlmRequest(
        messages=(LlmMessage("system", "...", cache_control=True),
                  LlmMessage("user", "...")),
        model="anthropic/claude-3-5-haiku-latest",
        tenant_id="T001",
    ))
    assert out.cache_read_input_tokens == 1200
    assert out.cache_creation_input_tokens == 0


async def test_openai_cached_tokens_shape_is_also_understood():
    """OpenAI exposes cache via `prompt_tokens_details.cached_tokens`.
    The transport reads it the same way — different provider, same shape."""
    payload_oai = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "model": "gpt-4o-mini",
        "usage": {
            "prompt_tokens": 2000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 1700},
        },
    }
    from oneops.llm.transport import LiteLLMTransport
    t = LiteLLMTransport(base_url="http://x", api_key="k")
    t._client = _FakeAsyncClient(payload_oai)                    # type: ignore[assignment]
    out = await t.complete(LlmRequest(
        messages=(LlmMessage("user", "hi"),),                    # no cache_control
        model="gpt-4o-mini",
        tenant_id="T001",
    ))
    assert out.cache_read_input_tokens == 1700                   # OpenAI shape parsed


# ── LlmResponse hit ratio ──────────────────────────────────────────────


def test_prompt_cache_hit_ratio_zero_without_cache():
    r = LlmResponse(
        content="ok", model="m", prompt_tokens=1000, completion_tokens=50,
        cost_usd=0.0, latency_ms=100)
    assert r.prompt_cache_hit_ratio == 0.0


def test_prompt_cache_hit_ratio_partial():
    r = LlmResponse(
        content="ok", model="m", prompt_tokens=1000, completion_tokens=50,
        cost_usd=0.0, latency_ms=100, cache_read_input_tokens=600)
    assert r.prompt_cache_hit_ratio == 0.6


def test_prompt_cache_hit_ratio_full():
    r = LlmResponse(
        content="ok", model="m", prompt_tokens=1000, completion_tokens=50,
        cost_usd=0.0, latency_ms=100, cache_read_input_tokens=1000)
    assert r.prompt_cache_hit_ratio == 1.0


def test_prompt_cache_hit_ratio_capped_at_one():
    """Defensive: a buggy provider reporting MORE cache hits than prompt
    tokens still caps the ratio at 1.0 — dashboards never report >100%."""
    r = LlmResponse(
        content="ok", model="m", prompt_tokens=100, completion_tokens=50,
        cost_usd=0.0, latency_ms=100, cache_read_input_tokens=200)
    assert r.prompt_cache_hit_ratio == 1.0
