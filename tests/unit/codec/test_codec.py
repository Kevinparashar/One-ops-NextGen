"""Contract tests for the protobuf codec (ADR-0001, P2).

These exercise the real codec end to end — encode produces bytes, decode
consumes them — no mock of the system under test. The forward-compatibility
test proves the N / N-1 guarantee on the actual wire format.
"""
from __future__ import annotations

import pytest

from oneops.codec import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    decode,
    encode,
    messages as msg,
)
from oneops.errors import MalformedMessageError, UnsupportedSchemaVersionError


# ── round-trip ───────────────────────────────────────────────────────────


def test_uc_request_round_trips_intact():
    req = msg.UCRequest(
        request_id="r-001", session_id="s-001", user_id="u-1", role="employee",
        locale="en-US", agent_id="uc01_summarization", intent="summary",
        parameters={"ticket_id": "INC0048213", "service_id": "incident"},
        message="summarize INC0048213")
    raw = encode(req, tenant_id="tenant-a", trace_context="tp-123",
                 idempotency_key="idem-9")
    out = decode(raw)

    assert out.tenant_id == "tenant-a"
    assert out.trace_context == "tp-123"
    assert out.idempotency_key == "idem-9"
    assert out.message_type == msg.UC_REQUEST
    assert out.schema_version == CURRENT_SCHEMA_VERSION
    assert out.emitted_at_unix_ms > 0
    assert isinstance(out.payload, msg.UCRequest)
    assert out.payload.request_id == "r-001"
    assert out.payload.agent_id == "uc01_summarization"
    assert dict(out.payload.parameters) == {
        "ticket_id": "INC0048213", "service_id": "incident"}
    assert out.payload.message == "summarize INC0048213"


def test_uc_response_round_trips_intact():
    resp = msg.UCResponse(
        request_id="r-001", status=msg.EXECUTED, user_response="Done.",
        latency_ms=842, executed_tools=["get_ticket_details", "summarize_entity"])
    out = decode(encode(resp, tenant_id="tenant-a"))
    assert isinstance(out.payload, msg.UCResponse)
    assert out.payload.status == msg.EXECUTED
    assert list(out.payload.executed_tools) == ["get_ticket_details", "summarize_entity"]
    assert out.payload.latency_ms == 842


def test_conversation_event_round_trips_intact():
    ev = msg.ConversationEvent(
        session_id="s-1", turn_role="user", content="hi", turn_index=3,
        occurred_at_unix_ms=1_700_000_000_000)
    out = decode(encode(ev, tenant_id="tenant-a"))
    assert isinstance(out.payload, msg.ConversationEvent)
    assert out.payload.turn_role == "user"
    assert out.payload.turn_index == 3


# ── encode guards ────────────────────────────────────────────────────────


def test_encode_rejects_non_contract_payload():
    with pytest.raises(MalformedMessageError, match="not a registered contract message"):
        encode(object(), tenant_id="tenant-a")        # type: ignore[arg-type]


def test_encode_rejects_empty_tenant_id():
    with pytest.raises(MalformedMessageError, match="tenant_id is mandatory"):
        encode(msg.UCRequest(request_id="r"), tenant_id="")


# ── decode guards ────────────────────────────────────────────────────────


def test_decode_rejects_garbage_bytes():
    with pytest.raises(MalformedMessageError):
        decode(b"\xff\xff not a protobuf message \x00\x01")


def test_decode_rejects_schema_version_below_window():
    raw = encode(msg.UCRequest(request_id="r"), tenant_id="tenant-a",
                 schema_version=MIN_SUPPORTED_SCHEMA_VERSION - 1)
    with pytest.raises(UnsupportedSchemaVersionError, match="outside the supported window"):
        decode(raw)


def test_decode_rejects_schema_version_above_window():
    raw = encode(msg.UCRequest(request_id="r"), tenant_id="tenant-a",
                 schema_version=CURRENT_SCHEMA_VERSION + 1)
    with pytest.raises(UnsupportedSchemaVersionError, match="too old or too new"):
        decode(raw)


def test_decode_accepts_every_version_in_window():
    for v in range(MIN_SUPPORTED_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION + 1):
        raw = encode(msg.UCRequest(request_id="r"), tenant_id="tenant-a",
                     schema_version=v)
        assert decode(raw).schema_version == v


def test_decode_rejects_envelope_without_tenant_id():
    # Build an envelope directly with no tenant_id — bypasses encode()'s guard
    # to prove decode() independently enforces the invariant.
    env = msg.Envelope(schema_version=CURRENT_SCHEMA_VERSION,
                       message_type=msg.UC_REQUEST,
                       payload=msg.UCRequest(request_id="r").SerializeToString())
    with pytest.raises(MalformedMessageError, match="no tenant_id"):
        decode(env.SerializeToString())


def test_decode_rejects_unknown_message_type():
    env = msg.Envelope(schema_version=CURRENT_SCHEMA_VERSION,
                       message_type=msg.MESSAGE_TYPE_UNSPECIFIED,
                       tenant_id="tenant-a", payload=b"")
    with pytest.raises(MalformedMessageError, match="not a known payload type"):
        decode(env.SerializeToString())


def test_decode_rejects_corrupt_payload():
    env = msg.Envelope(schema_version=CURRENT_SCHEMA_VERSION,
                       message_type=msg.UC_REQUEST, tenant_id="tenant-a",
                       payload=b"\xff\xff\xff not a UCRequest")
    with pytest.raises(MalformedMessageError, match="not a valid UCRequest"):
        decode(env.SerializeToString())


# ── N / N-1 forward compatibility (the real wire-format guarantee) ───────


def _varint(n: int) -> bytes:
    """Encode an int as a protobuf varint."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def test_decode_tolerates_an_unknown_field_from_a_newer_producer():
    """A future schema version may *add* a field. Protobuf field numbers are
    permanent, so a message a newer producer emits — carrying a field this
    build has never seen — must still decode here, with the unknown field
    silently ignored. This IS the N / N-1 guarantee on the wire."""
    req = msg.UCRequest(request_id="r-future", agent_id="uc01_summarization")
    raw = encode(req, tenant_id="tenant-a")

    # Append an unknown field: field number 999, wire type 0 (varint), value 42.
    # Concatenating valid protobuf is valid protobuf — fields merge.
    unknown_tag = (999 << 3) | 0
    forged = raw + _varint(unknown_tag) + _varint(42)

    out = decode(forged)                              # must not raise
    assert out.tenant_id == "tenant-a"
    assert isinstance(out.payload, msg.UCRequest)
    assert out.payload.request_id == "r-future"
    assert out.payload.agent_id == "uc01_summarization"


def test_old_envelope_without_a_later_field_still_decodes():
    """The reverse direction: a message an *older* producer emitted, lacking a
    field this build knows about, decodes with that field at its default — no
    error. Proven by omitting `message` on UCRequest."""
    req = msg.UCRequest(request_id="r-old")           # `message` field left unset
    out = decode(encode(req, tenant_id="tenant-a"))
    assert out.payload.request_id == "r-old"
    assert out.payload.message == ""                  # absent field → default
