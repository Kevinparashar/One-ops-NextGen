"""Service-token (JWT) tests — mint/verify, expiry, tamper, clock skew.

The exit criterion is "signed service-JWT verification proven on an internal
boundary": a valid token verifies to the right identity, and every way a token
can be wrong (expired, tampered, wrong secret, wrong type, no secret) is
rejected with a typed error.
"""
from __future__ import annotations

import time

import jwt
import pytest

from oneops.authz.tokens import (
    mint_service_token,
    verify_service_token,
)
from oneops.errors import ConfigError, InvalidServiceTokenError

# >= 32 bytes — HS256 minimum recommended key length (RFC 7518 §3.2).
_SECRET = "test-secret-do-not-use-in-prod-0123456789"


@pytest.fixture(autouse=True)
def _set_secret(monkeypatch):
    monkeypatch.setenv("AUTHZ_JWT_SECRET", _SECRET)


def test_mint_then_verify_round_trips_identity():
    token = mint_service_token("router-service", ttl_seconds=300)
    identity = verify_service_token(token)
    assert identity.service_name == "router-service"
    assert identity.expires_at > identity.issued_at


def test_expired_token_is_rejected():
    token = mint_service_token("router-service", ttl_seconds=1)
    time.sleep(1.2)
    # leeway=0 so the 1s-TTL token is unambiguously expired.
    with pytest.raises(InvalidServiceTokenError, match="expired"):
        verify_service_token(token, leeway_seconds=0)


def test_clock_skew_leeway_accepts_a_just_expired_token():
    token = mint_service_token("router-service", ttl_seconds=1)
    time.sleep(1.2)
    # Within the leeway window the token is still accepted (clock-skew tolerance).
    identity = verify_service_token(token, leeway_seconds=30)
    assert identity.service_name == "router-service"


def test_tampered_token_is_rejected():
    token = mint_service_token("router-service")
    # Flip a character in the payload segment — signature no longer matches.
    head, payload, sig = token.split(".")
    tampered = f"{head}.{payload[:-2]}XX.{sig}"
    with pytest.raises(InvalidServiceTokenError):
        verify_service_token(tampered)


def test_token_signed_with_a_different_secret_is_rejected():
    forged = jwt.encode(
        {"iss": "oneops", "sub": "evil", "typ": "service",
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        "a-different-secret-also-32-bytes-1234567", algorithm="HS256")
    with pytest.raises(InvalidServiceTokenError):
        verify_service_token(forged)


def test_token_with_wrong_type_claim_is_rejected():
    bad = jwt.encode(
        {"iss": "oneops", "sub": "router", "typ": "user_session",
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        _SECRET, algorithm="HS256")
    with pytest.raises(InvalidServiceTokenError, match="typ"):
        verify_service_token(bad)


def test_token_with_wrong_issuer_is_rejected():
    bad = jwt.encode(
        {"iss": "somebody-else", "sub": "router", "typ": "service",
         "iat": int(time.time()), "exp": int(time.time()) + 300},
        _SECRET, algorithm="HS256")
    with pytest.raises(InvalidServiceTokenError):
        verify_service_token(bad)


def test_empty_token_is_rejected():
    with pytest.raises(InvalidServiceTokenError, match="empty"):
        verify_service_token("")


def test_missing_secret_fails_loud(monkeypatch):
    monkeypatch.delenv("AUTHZ_JWT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="AUTHZ_JWT_SECRET"):
        mint_service_token("router-service")
