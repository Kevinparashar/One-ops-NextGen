"""Codec layer — protobuf wire/disk contract (ADR-0001, P2).

Public surface:
    from oneops.codec import encode, decode, DecodedEnvelope
    from oneops.codec import CURRENT_SCHEMA_VERSION
    from oneops.codec import messages as msg     # generated protobuf types

`messages` re-exports the generated `envelope_pb2` module so callers construct
`UCRequest` / `UCResponse` / `ConversationEvent` without importing the deep
generated path.
"""
from __future__ import annotations

from oneops.codec.codec import (
    CURRENT_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
    DecodedEnvelope,
    decode,
    encode,
)
from oneops.codec.generated.oneops.v1 import envelope_pb2 as messages

__all__ = [
    "encode",
    "decode",
    "DecodedEnvelope",
    "CURRENT_SCHEMA_VERSION",
    "MIN_SUPPORTED_SCHEMA_VERSION",
    "messages",
]
