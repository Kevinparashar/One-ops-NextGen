"""UC-2 ID resolution — reuse the platform's `EntityIdNormalizer`.

UC-2 supports two input shapes:
  1. Canonical prefix + digits — "INC0001234" / "REQ0001234"  → unambiguous
  2. Bare digits + service_id  — "0001234" + "incident"        → composed

Anything else is rejected at the boundary with a `ResolveError` that the
caller turns into a 400. The normalizer is the same one the router uses, so
behaviour stays consistent across button + chat (rule §2.1 — one source of
truth, no UC-2-only regex).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from oneops.router.entity_id import EntityIdNormalizer

# UC-2 v1 scope. Mirrors `supported_services` in the agent catalog.
_SUPPORTED: frozenset[str] = frozenset({"incident", "request"})


@dataclass(frozen=True, slots=True)
class ResolvedTicket:
    """Output of `resolve()` — canonical entity_id + the service it lives in."""

    entity_id: str
    service_id: str  # narrowed to "incident" | "request" by validate path


class ResolveError(ValueError):
    """Boundary error — message is safe to surface to the caller verbatim."""


_BARE_DIGITS = re.compile(r"^\s*\d{1,32}\s*$")
_KNOWN_PREFIXES_V2 = ("INC", "REQ")


def _zero_pad(body: str) -> str:
    """Match the substrate's ID width (e.g. INC0001234)."""
    return body.zfill(7) if len(body) < 7 else body


def resolve(
    raw_id: str,
    service_id_hint: str | None,
    *,
    normalizer: EntityIdNormalizer | None = None,
) -> ResolvedTicket:
    """Return a canonical (entity_id, service_id) or raise `ResolveError`.

    Decision order:
      1. Reject empty/whitespace.
      2. If the input is bare digits, REQUIRE service_id; compose "INC…"/"REQ…".
      3. Otherwise, normalize with `EntityIdNormalizer` (handles separators,
         case, common typos like "INC-0001-234"). The resulting service_id
         must be in UC-2's scope.
      4. If both prefix and service_id_hint are supplied, they must agree.
    """
    if raw_id is None or not str(raw_id).strip():
        raise ResolveError("ticket_id is required")

    s = str(raw_id).strip()
    hint = (service_id_hint or "").strip().lower() or None
    if hint is not None and hint not in _SUPPORTED:
        raise ResolveError(
            f"service_id must be one of {sorted(_SUPPORTED)}; got {hint!r}")

    # Bare digits — needs service_id to disambiguate.
    if _BARE_DIGITS.match(s):
        if hint is None:
            raise ResolveError(
                "bare ticket number without service_id is ambiguous — "
                "provide service_id ('incident' or 'request'), or include the "
                "prefix (e.g. INC0001234, REQ0001234)")
        body = _zero_pad(s.strip())
        prefix = "INC" if hint == "incident" else "REQ"
        return ResolvedTicket(entity_id=prefix + body, service_id=hint)

    # Normalised path — let the router's normalizer handle separators/case/etc.
    return _resolve_normalised(s, hint, normalizer)


def _resolve_normalised(
    s: str, hint: str | None, normalizer: EntityIdNormalizer | None,
) -> ResolvedTicket:
    """Normalise a prefixed id via EntityIdNormalizer and enforce UC-2 scope:
    the resolved service must be supported, and must agree with `hint` when one
    was supplied. Raises ResolveError on any violation."""
    norm = (normalizer or EntityIdNormalizer.from_registry_file()).normalize(s)
    if not norm.ok or norm.entity is None:
        raise ResolveError(norm.reason or "could not parse ticket_id")

    resolved_service = norm.entity.service_id
    if resolved_service not in _SUPPORTED:
        raise ResolveError(
            f"UC-2 supports {sorted(_SUPPORTED)}; got service_id="
            f"{resolved_service!r} from {norm.entity.entity_id!r}")

    if hint is not None and hint != resolved_service:
        raise ResolveError(
            f"service_id={hint!r} contradicts the prefix in "
            f"{norm.entity.entity_id!r} (service inferred: {resolved_service!r})")

    return ResolvedTicket(entity_id=norm.entity.entity_id,
                          service_id=resolved_service)


__all__ = ["resolve", "ResolveError", "ResolvedTicket"]
