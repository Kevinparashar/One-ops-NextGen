"""Typed error hierarchy.

All exceptions raised by the OneOps codebase inherit from OneOpsError.
This lets entry handlers map errors to consistent NATS response status codes
without leaking implementation details to the Bridge service or end users.
"""
from __future__ import annotations


class OneOpsError(Exception):
    """Base for every error this service raises."""

    code: str = "ONEOPS_ERROR"

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


# ── Configuration / startup ──────────────────────────────────────────────
class ConfigError(OneOpsError):
    code = "CONFIG_ERROR"


# ── Upstream / external ──────────────────────────────────────────────────
class UpstreamError(OneOpsError):
    """A dependency we don't own returned an error or timed out."""

    code = "UPSTREAM_ERROR"


class LLMUpstreamError(UpstreamError):
    code = "LLM_UPSTREAM_ERROR"


class LLMTimeoutError(LLMUpstreamError):
    code = "LLM_TIMEOUT"


class LLMRateLimitError(LLMUpstreamError):
    code = "LLM_RATE_LIMIT"


class CacheUnavailableError(UpstreamError):
    code = "CACHE_UNAVAILABLE"


class NATSUnavailableError(UpstreamError):
    code = "NATS_UNAVAILABLE"


# ── Internal / contract ──────────────────────────────────────────────────
class InvalidPayloadError(OneOpsError):
    code = "INVALID_PAYLOAD"


class InvariantError(OneOpsError):
    """Observability/contract invariant violated by an executed response."""

    code = "INVARIANT_VIOLATION"


# ── Tool layer ───────────────────────────────────────────────────────────
class ToolNotFoundError(OneOpsError):
    code = "TOOL_NOT_FOUND"


class ToolPermissionError(OneOpsError):
    code = "TOOL_PERMISSION_DENIED"


# ── Registry layer (P1) ──────────────────────────────────────────────────
class RegistryError(OneOpsError):
    """Base for registry-layer faults."""

    code = "REGISTRY_ERROR"


class RecordNotFoundError(RegistryError):
    code = "REGISTRY_RECORD_NOT_FOUND"


class RecordConflictError(RegistryError):
    """A create/update collided with an existing version or id."""

    code = "REGISTRY_RECORD_CONFLICT"


class RecordValidationError(RegistryError):
    """A record failed schema validation or a cross-record integrity rule."""

    code = "REGISTRY_RECORD_INVALID"


class RegistryIntegrityError(RegistryError):
    """A cross-record invariant is broken (dangling ref, dependency cycle)."""

    code = "REGISTRY_INTEGRITY_VIOLATION"


class RegistryDuplicateIdError(RegistryError):
    """Same record_id appears in multiple subfolders under the same kind.

    Raised by the file backend's recursive walker when two files with the
    same `record_id.json` are discovered under e.g. `tools/uc01_summarization/`
    and `tools/shared/`. Silent override would break the substrate's
    "agents-are-data" guarantee; fail loud at boot instead.
    """

    code = "REGISTRY_DUPLICATE_ID"


# ── Codec layer (P2) ─────────────────────────────────────────────────────
class CodecError(OneOpsError):
    """Base for wire/disk codec faults (ADR-0001 protobuf envelope)."""

    code = "CODEC_ERROR"


class MalformedMessageError(CodecError):
    """Bytes could not be parsed as a valid envelope or typed payload."""

    code = "CODEC_MALFORMED_MESSAGE"


class UnsupportedSchemaVersionError(CodecError):
    """The envelope's schema_version is outside the supported N / N-1 window."""

    code = "CODEC_UNSUPPORTED_SCHEMA_VERSION"


# ── AuthZ layer (P4) ─────────────────────────────────────────────────────
class AuthzError(OneOpsError):
    """Base for authorization-layer faults."""

    code = "AUTHZ_ERROR"


class InvalidServiceTokenError(AuthzError):
    """A service JWT failed signature, expiry, or claim verification."""

    code = "AUTHZ_INVALID_SERVICE_TOKEN"


# ── Tool-runner layer (P7) ───────────────────────────────────────────────
class ToolRunnerError(OneOpsError):
    """Base for tool-execution faults."""

    code = "TOOL_RUNNER_ERROR"


class ToolHandlerError(ToolRunnerError):
    """A tool's handler_ref could not be resolved to a callable."""

    code = "TOOL_HANDLER_UNRESOLVED"


class ToolTimeoutError(ToolRunnerError):
    """A tool exceeded its declared timeout and was cancelled."""

    code = "TOOL_TIMEOUT"


# ── LLM Gateway layer (P8) ───────────────────────────────────────────────
class LLMGatewayError(OneOpsError):
    """Base for LLM-gateway faults."""

    code = "LLM_GATEWAY_ERROR"


class QuotaExceededError(LLMGatewayError):
    """A tenant has exhausted its LLM quota for the window."""

    code = "LLM_QUOTA_EXCEEDED"


__all__ = [
    "OneOpsError",
    "ConfigError",
    "UpstreamError",
    "LLMUpstreamError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "CacheUnavailableError",
    "NATSUnavailableError",
    "InvalidPayloadError",
    "InvariantError",
    "ToolNotFoundError",
    "ToolPermissionError",
    "RegistryError",
    "RecordNotFoundError",
    "RecordConflictError",
    "RecordValidationError",
    "RegistryIntegrityError",
    "CodecError",
    "MalformedMessageError",
    "UnsupportedSchemaVersionError",
    "AuthzError",
    "InvalidServiceTokenError",
    "ToolRunnerError",
    "ToolHandlerError",
    "ToolTimeoutError",
    "LLMGatewayError",
    "QuotaExceededError",
]
