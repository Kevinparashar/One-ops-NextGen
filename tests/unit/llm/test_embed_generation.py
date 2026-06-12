"""Embeddings render as Langfuse GENERATIONS (2026-06-12 observability fix).

Before the fix `gateway.embed()` opened an `llm.embed` span but never marked it
a generation, so embeddings (router retrieval, uc02/uc03/uc08 query embeds) were
invisible in the Langfuse generations view — no model / tokens / cost. These
tests pin the marking via an in-memory span exporter, hermetic (EchoTransport,
no network).
"""
from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from oneops.llm import EchoTransport, LlmGateway


def _embed_span(exporter: InMemorySpanExporter):
    return next(s for s in exporter.get_finished_spans() if s.name == "llm.embed")


@pytest.mark.asyncio
async def test_embed_span_is_marked_a_generation_with_model_and_tokens(monkeypatch):
    # Content flag off: non-content dimensions (type/model/tokens/cost) must
    # still be emitted so the generation renders in the trace tree.
    monkeypatch.delenv("LANGFUSE_CAPTURE_CONTENT", raising=False)
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()

    gw = LlmGateway(EchoTransport(embed_dims=8))
    vecs = await gw.embed(["find similar vpn tickets"], model="text-embedding-3-large",
                          tenant_id="T001", user_id="u1", dimensions=8)
    assert len(vecs) == 1 and len(vecs[0]) == 8

    span = _embed_span(exporter)
    attrs = dict(span.attributes or {})
    assert attrs.get("langfuse.observation.type") == "generation"
    assert attrs.get("gen_ai.request.model") == "text-embedding-3-large"
    # input-token estimate is non-zero; cost is recorded (always-on dimensions).
    assert int(attrs.get("gen_ai.usage.input_tokens", 0)) >= 1
    assert "gen_ai.usage.cost" in attrs
    # Content-gated I/O must be ABSENT when the flag is off.
    assert "langfuse.observation.input" not in attrs


@pytest.mark.asyncio
async def test_embed_generation_captures_redacted_io_when_content_on(monkeypatch):
    monkeypatch.setenv("LANGFUSE_CAPTURE_CONTENT", "true")
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    exporter.clear()

    gw = LlmGateway(EchoTransport(embed_dims=4))
    await gw.embed(["reset my password"], model="text-embedding-3-large",
                   tenant_id="T001", dimensions=4)

    attrs = dict(_embed_span(exporter).attributes or {})
    assert attrs.get("langfuse.observation.type") == "generation"
    # Input present (the query text); output is the vector GEOMETRY, never floats.
    assert "langfuse.observation.input" in attrs
    out = str(attrs.get("langfuse.observation.output", ""))
    assert "dimensions" in out and "4" in out
