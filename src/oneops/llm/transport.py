"""LLM transport — the actual provider call, behind the gateway.

`LlmTransport` is a Protocol. The gateway owns quota / redaction / cost /
retry / fallback; the transport owns only "send this request to a provider,
return the completion".

  * `EchoTransport` — deterministic, no network. Returns a canned completion
    (or echoes the last user message) and deterministic embedding vectors.
    Backs the unit suite and local dev. A real implementation of the Protocol,
    not a mock — it simply doesn't call a provider.
  * `LiteLLMTransport` — production: an HTTP call to the LiteLLM proxy, the one
    process that holds provider keys. Env-gated; not exercised without infra.

The gateway is the *only* importer of a transport — no other module talks to a
provider, directly or via a transport. The CI gate (`test_no_direct_provider`)
enforces it.
"""
from __future__ import annotations

import hashlib
from typing import Any, Protocol

from oneops.errors import LLMUpstreamError
from oneops.llm.models import LlmRequest, TransportResult


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) — exact counts come from a real
    provider; this keeps cost accounting meaningful under `EchoTransport`."""
    return max(1, len(text) // 4)


class LlmTransport(Protocol):
    async def complete(self, request: LlmRequest) -> TransportResult:
        """Send a completion request to the provider."""
        ...

    async def embed(self, texts: list[str], *, model: str,
                    dimensions: int | None = None) -> list[list[float]]:
        """Embed texts. Embeddings egress through the gateway too.

        `dimensions` is the OpenAI / Azure / LiteLLM-compatible matryoshka
        reduction parameter — for `text-embedding-3-large` the default is
        3072 but stored KB vectors may be reduced (e.g. 1536). Transports
        that don't support it must ignore it (the provider does the same)."""
        ...


class EchoTransport:
    """Deterministic transport — no network. Optionally fails the first
    `fail_times` calls (to exercise the gateway's retry / fallback)."""

    def __init__(self, *, canned: str | None = None, fail_times: int = 0,
                 embed_dims: int = 8) -> None:
        self._canned = canned
        self._fail_remaining = fail_times
        self._embed_dims = embed_dims

    async def complete(self, request: LlmRequest) -> TransportResult:
        if self._fail_remaining > 0:
            self._fail_remaining -= 1
            raise LLMUpstreamError("EchoTransport: simulated transient failure")
        if self._canned is not None:
            content = self._canned
        else:
            last_user = next(
                (m.content for m in reversed(request.messages) if m.role == "user"),
                "")
            content = f"echo: {last_user}"
        prompt_tokens = sum(_estimate_tokens(m.content) for m in request.messages)
        return TransportResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=_estimate_tokens(content),
            finish_reason="stop",
            actual_model=request.model,
        )

    async def embed(self, texts: list[str], *, model: str,
                    dimensions: int | None = None) -> list[list[float]]:
        """Deterministic pseudo-embeddings — a hash-seeded vector per text, so
        the same text always yields the same vector."""
        dims = int(dimensions) if dimensions else self._embed_dims
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [(digest[i % len(digest)] / 255.0) for i in range(dims)]
            vectors.append(vec)
        return vectors


class LiteLLMTransport:
    """Production transport — HTTP to the LiteLLM proxy (the brief's gateway
    technology, OpenAI-compatible `/chat/completions` and `/embeddings`).

    A thin adapter: every gateway behaviour (quota, retry, fallback,
    redaction, cost accounting) lives in `LlmGateway`, not here. The
    transport's only job is one round-trip and a typed `TransportResult`.

    Production invariants:
      * HTTP via `httpx.AsyncClient` — connection pooling per process.
      * Timeout is per-request (`timeout_s`); the gateway's retry policy
        sits on top.
      * `response_format=json` is mapped to LiteLLM's `response_format`
        with `{"type": "json_object"}` so providers that support it emit
        strict JSON. Providers that don't simply ignore the field — caller
        validates the parsed JSON either way.
      * Errors raise a typed `OneOpsError`; the gateway maps these to its
        retry-vs-failover rules.
    """

    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 60.0) -> None:
        if not base_url:
            raise ValueError("LiteLLMTransport.base_url is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s
        # Lazy client — only built when needed so import time is cheap
        # (FaaS cold-start friendly). One client per process; httpx's
        # AsyncClient is safe for concurrent use. The client is bound to the
        # event loop it is created on, so we also remember that loop and
        # rebuild if we are ever called from a different/closed loop.
        self._client: Any | None = None
        self._client_loop: Any | None = None

    async def _get_client(self) -> Any:
        import asyncio
        loop = asyncio.get_running_loop()
        # An httpx.AsyncClient is pinned to the event loop it was built on.
        # Under uvicorn there is a single long-lived loop, so the client is
        # created once and reused (connection pooling preserved). But some
        # callers — notably the Starlette TestClient — drive each request on a
        # fresh, short-lived loop; reusing a client bound to a now-closed loop
        # raises "Event loop is closed". Detect a loop change and rebuild on the
        # current loop rather than reuse a dead client.
        # Only rebuild a client WE created (i.e. `_client_loop` is recorded) when
        # the loop has changed. A client injected directly (e.g. tests set
        # `t._client = fake`, leaving `_client_loop` None) is honoured as-is.
        if (self._client is not None and self._client_loop is not None
                and self._client_loop is not loop):
            self._client = None          # stale client's loop is gone; drop the
            self._client_loop = None     # reference (cannot aclose on a dead loop)
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_s),
                headers={
                    "authorization": f"Bearer {self._api_key}" if self._api_key else "",
                    "content-type": "application/json",
                },
            )
            self._client_loop = loop
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._client_loop = None

    async def complete(self, request: LlmRequest) -> TransportResult:
        client = await self._get_client()
        # Prompt-cache hints: if any message has `cache_control=True`, emit
        # the Anthropic content-block format with `cache_control` markers.
        # LiteLLM passes this through to Anthropic; providers that don't
        # support prompt caching see flat-string content (the non-cached
        # path below). The mixed-shape branching keeps the simple case
        # token-identical with the pre-caching behaviour.
        messages_payload = _build_messages_payload(request.messages)
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages_payload,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        # JSON mode: ask the provider for strict JSON when the caller
        # requested it. Providers that don't honour the flag still return
        # text; the caller validates either way.
        if request.response_format.value == "json":
            body["response_format"] = {"type": "json_object"}
        from oneops.errors import LLMTimeoutError, LLMUpstreamError
        try:
            payload = await self._post_chat(client, body)
        except (LLMTimeoutError, LLMUpstreamError):
            raise
        except Exception as exc:                       # noqa: BLE001 — boundary
            raise LLMUpstreamError(
                f"LiteLLM proxy: unexpected error {type(exc).__name__}: {exc}",
                cause=exc) from exc
        return _parse_completion(payload, request)

    async def _post_chat(self, client: Any, body: dict[str, Any]) -> dict[str, Any]:
        """POST /chat/completions, mapping transport failures to typed errors
        (timeout → LLMTimeoutError; HTTP / 4xx-5xx → LLMUpstreamError)."""
        import httpx

        from oneops.errors import LLMTimeoutError, LLMUpstreamError
        try:
            resp = await client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"LiteLLM proxy timed out after {self._timeout_s:.0f}s",
                cause=exc) from exc
        except httpx.HTTPError as exc:
            raise LLMUpstreamError(
                f"LiteLLM proxy HTTP error: {exc}", cause=exc) from exc
        if resp.status_code >= 400:
            raise LLMUpstreamError(
                f"LiteLLM proxy returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def embed(self, texts: list[str], *, model: str,
                    dimensions: int | None = None) -> list[list[float]]:
        client = await self._get_client()
        body: dict[str, Any] = {"model": model, "input": texts}
        if dimensions:
            body["dimensions"] = int(dimensions)
        try:
            resp = await client.post("/embeddings", json=body)
        except Exception as exc:                       # noqa: BLE001 — boundary
            from oneops.errors import LLMUpstreamError
            raise LLMUpstreamError(
                f"LiteLLM proxy embed error: {exc}", cause=exc) from exc
        if resp.status_code >= 400:
            from oneops.errors import LLMUpstreamError
            raise LLMUpstreamError(
                f"LiteLLM proxy embed returned {resp.status_code}: "
                f"{resp.text[:200]}")
        payload = resp.json()
        data = payload.get("data") or []
        return [list(map(float, row.get("embedding") or [])) for row in data]


def _build_messages_payload(messages: Any) -> list[dict[str, Any]]:
    """Anthropic prompt-cache content-block shape for messages flagged
    cache_control=True (LiteLLM passes it through to Anthropic); flat-string
    content otherwise — token-identical with the pre-caching path."""
    if not any(getattr(m, "cache_control", False) for m in messages):
        return [{"role": m.role, "content": m.content} for m in messages]
    payload: list[dict[str, Any]] = []
    for m in messages:
        if getattr(m, "cache_control", False):
            payload.append({
                "role": m.role,
                "content": [{
                    "type": "text",
                    "text": m.content,
                    "cache_control": {"type": "ephemeral"},
                }],
            })
        else:
            payload.append({"role": m.role, "content": m.content})
    return payload


def _parse_completion(payload: dict[str, Any], request: LlmRequest) -> TransportResult:
    """Map an OpenAI-compatible completion payload to a TransportResult. Cache-
    token fields surface differently across providers (Anthropic via LiteLLM:
    cache_read/creation_input_tokens; OpenAI: prompt_tokens_details.cached_tokens)
    — read whatever is present, absence → 0."""
    from oneops.errors import LLMUpstreamError
    choices = payload.get("choices") or []
    if not choices:
        raise LLMUpstreamError("LiteLLM proxy returned no choices")
    choice = choices[0]
    content = (choice.get("message") or {}).get("content") or ""
    finish_reason = choice.get("finish_reason") or "stop"
    usage = payload.get("usage") or {}
    cache_read = int(
        usage.get("cache_read_input_tokens")
        or ((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        or 0
    )
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    return TransportResult(
        content=str(content),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        finish_reason=str(finish_reason),
        actual_model=str(payload.get("model") or request.model),
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


__all__ = ["LlmTransport", "EchoTransport", "LiteLLMTransport"]
