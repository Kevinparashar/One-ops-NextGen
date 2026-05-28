"""Codec — the single encode/decode path for the protobuf wire contract.

ADR-0001: every inter-service message and every on-disk conversation event is
an `Envelope` carrying a typed payload. This module is the *only* place that
serialises or deserialises that contract — no module hand-rolls protobuf calls
or falls back to ad-hoc JSON on a boundary.

Schema-version window (the N / N-1 rule, MIGRATION.md P2):
  * `CURRENT_SCHEMA_VERSION` — what this build emits.
  * `MIN_SUPPORTED_SCHEMA_VERSION` — the oldest version this build still
    accepts. A rolling deploy always has at most two versions live, so the
    window is [current-1, current].
  * `decode()` rejects anything outside the window with a typed error — a
    message from a too-new producer is refused loudly, never half-parsed.

Protobuf field numbers are permanent (ADR-0001), so within the window a
producer that *added* an optional field does not break an older consumer:
the unknown field is preserved and ignored. `test_codec.py` proves this.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from google.protobuf.message import DecodeError, Message

from oneops.errors import MalformedMessageError, UnsupportedSchemaVersionError
from oneops.codec.generated.oneops.v1 import envelope_pb2 as pb

# ── Schema-version window ────────────────────────────────────────────────

CURRENT_SCHEMA_VERSION = 1
MIN_SUPPORTED_SCHEMA_VERSION = 1   # becomes CURRENT-1 once a v2 schema ships

# message_type enum → typed payload class. The single source of truth for
# payload dispatch — both encode and decode consult it.
_PAYLOAD_BY_TYPE: dict[int, type[Message]] = {
    pb.UC_REQUEST: pb.UCRequest,
    pb.UC_RESPONSE: pb.UCResponse,
    pb.CONVERSATION_EVENT: pb.ConversationEvent,
}
_TYPE_BY_PAYLOAD: dict[type[Message], int] = {v: k for k, v in _PAYLOAD_BY_TYPE.items()}


@dataclass(frozen=True)
class DecodedEnvelope:
    """A validated envelope plus its already-deserialised typed payload."""

    schema_version: int
    message_type: int
    tenant_id: str
    trace_context: str
    idempotency_key: str
    emitted_at_unix_ms: int
    payload: Message


def _now_ms() -> int:
    """Server time, milliseconds. Client timestamps are never trusted for
    ordering (ARCHITECTURE.md §6)."""
    return int(time.time() * 1000)


def encode(
    payload: Message,
    *,
    tenant_id: str,
    trace_context: str = "",
    idempotency_key: str = "",
    schema_version: int = CURRENT_SCHEMA_VERSION,
) -> bytes:
    """Wrap a typed payload in an `Envelope` and serialise to bytes.

    Args:
        payload: a `UCRequest`, `UCResponse`, or `ConversationEvent`.
        tenant_id: tenant scope — mandatory; every message is tenant-scoped.
        trace_context: W3C traceparent for OTel propagation.
        idempotency_key: guards double-execution under at-least-once delivery.
        schema_version: defaults to the current version; an explicit value is
            only for tests exercising the version window.

    Raises:
        MalformedMessageError: `payload` is not a recognised contract message,
            or `tenant_id` is empty.
    """
    message_type = _TYPE_BY_PAYLOAD.get(type(payload))
    if message_type is None:
        raise MalformedMessageError(
            f"cannot encode payload of type {type(payload).__name__} — "
            "not a registered contract message"
        )
    if not tenant_id:
        raise MalformedMessageError("tenant_id is mandatory on every envelope")

    envelope = pb.Envelope(
        schema_version=schema_version,
        message_type=message_type,
        tenant_id=tenant_id,
        trace_context=trace_context,
        idempotency_key=idempotency_key,
        payload=payload.SerializeToString(),
        emitted_at_unix_ms=_now_ms(),
    )
    return envelope.SerializeToString()


def decode(raw: bytes) -> DecodedEnvelope:
    """Parse + validate an envelope and deserialise its typed payload.

    Raises:
        MalformedMessageError: the bytes are not a parseable envelope, the
            payload is unparseable, the message_type is unknown, or tenant_id
            is missing.
        UnsupportedSchemaVersionError: schema_version is outside the
            [MIN_SUPPORTED, CURRENT] window.
    """
    envelope = pb.Envelope()
    try:
        envelope.ParseFromString(raw)
    except DecodeError as exc:
        raise MalformedMessageError("bytes are not a valid Envelope", cause=exc) from exc

    if not (MIN_SUPPORTED_SCHEMA_VERSION <= envelope.schema_version <= CURRENT_SCHEMA_VERSION):
        raise UnsupportedSchemaVersionError(
            f"envelope schema_version={envelope.schema_version} is outside the "
            f"supported window [{MIN_SUPPORTED_SCHEMA_VERSION}, {CURRENT_SCHEMA_VERSION}] — "
            "the producer is too old or too new for this build"
        )
    if not envelope.tenant_id:
        raise MalformedMessageError("decoded envelope has no tenant_id")

    payload_type = _PAYLOAD_BY_TYPE.get(envelope.message_type)
    if payload_type is None:
        raise MalformedMessageError(
            f"envelope message_type={envelope.message_type} is not a known payload type"
        )

    payload = payload_type()
    try:
        payload.ParseFromString(envelope.payload)
    except DecodeError as exc:
        raise MalformedMessageError(
            f"envelope payload is not a valid {payload_type.__name__}", cause=exc
        ) from exc

    return DecodedEnvelope(
        schema_version=envelope.schema_version,
        message_type=envelope.message_type,
        tenant_id=envelope.tenant_id,
        trace_context=envelope.trace_context,
        idempotency_key=envelope.idempotency_key,
        emitted_at_unix_ms=envelope.emitted_at_unix_ms,
        payload=payload,
    )


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "DecodedEnvelope",
    "encode",
    "decode",
]
