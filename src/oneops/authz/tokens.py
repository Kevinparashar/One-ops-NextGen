"""Service-to-service identity — signed JWTs for internal boundaries.

ARCHITECTURE.md §9: AuthZ on *every* boundary. A user request is authenticated
at the API Gateway; internal NATS calls between services carry a short-lived
**service JWT** so a receiver verifies the *caller service's* identity before
acting — internal traffic is not implicitly trusted.

Choice (documented): **HS256 with a shared secret.** A symmetric secret is the
right fit for a single trust domain where all services are operated together;
it needs no key-distribution infrastructure. If services later span trust
domains, move to RS256/ES256 — `verify_service_token` is the only seam.

The secret comes from `AUTHZ_JWT_SECRET`. There is **no default** — minting or
verifying without it raises `ConfigError`. An insecure fallback secret is worse
than a loud failure.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt

from oneops.errors import ConfigError, InvalidServiceTokenError

_ALGORITHM = "HS256"
_ISSUER = "oneops"
_TOKEN_TYPE = "service"
DEFAULT_TOKEN_TTL_SECONDS = 300
DEFAULT_CLOCK_SKEW_LEEWAY_SECONDS = 30


@dataclass(frozen=True)
class ServiceIdentity:
    """The verified identity of a calling service."""

    service_name: str
    issued_at: int
    expires_at: int


def _secret() -> str:
    secret = os.getenv("AUTHZ_JWT_SECRET")
    if not secret:
        raise ConfigError(
            "AUTHZ_JWT_SECRET is not set — service-token mint/verify cannot "
            "proceed. Set it from the secret manager; there is no default."
        )
    return secret


def mint_service_token(
    service_name: str, *, ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
) -> str:
    """Mint a short-lived service JWT identifying `service_name`."""
    if not service_name:
        raise ValueError("service_name is mandatory")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be > 0")
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "sub": service_name,
        "typ": _TOKEN_TYPE,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def verify_service_token(
    token: str, *, leeway_seconds: int = DEFAULT_CLOCK_SKEW_LEEWAY_SECONDS
) -> ServiceIdentity:
    """Verify a service JWT and return the caller's identity.

    Checks the HS256 signature, expiry (with `leeway_seconds` for clock skew),
    the issuer, and the `typ` claim. Any failure raises
    `InvalidServiceTokenError` — never a partial/ambiguous accept.
    """
    if not token:
        raise InvalidServiceTokenError("empty service token")
    try:
        claims = jwt.decode(
            token, _secret(), algorithms=[_ALGORITHM], issuer=_ISSUER,
            leeway=leeway_seconds, options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidServiceTokenError("service token has expired", cause=exc) from exc
    except jwt.InvalidTokenError as exc:
        # Covers bad signature, wrong issuer, missing claims, malformed token.
        raise InvalidServiceTokenError(
            f"service token failed verification: {exc}", cause=exc
        ) from exc

    if claims.get("typ") != _TOKEN_TYPE:
        raise InvalidServiceTokenError(
            f"token typ is {claims.get('typ')!r}, expected {_TOKEN_TYPE!r}"
        )
    return ServiceIdentity(
        service_name=str(claims["sub"]),
        issued_at=int(claims["iat"]),
        expires_at=int(claims["exp"]),
    )


__all__ = [
    "ServiceIdentity",
    "mint_service_token",
    "verify_service_token",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "DEFAULT_CLOCK_SKEW_LEEWAY_SECONDS",
]
