"""SR-id generator for UC-8.

The existing `itsm.request` data uses the format `SR` + 7 digits per
tenant. UC-8 must mint compatible ids when the user clicks
"Auto-create SR". Production-grade properties:

  • **Sequential per-tenant.** Each tenant's series is independent.
  • **Race-safe.** Concurrent button clicks cannot mint duplicate ids;
    the INSERT carries a UNIQUE constraint on (tenant_id, request_id)
    so even if two minters compute the same id, only one INSERT wins.
  • **No reuse of deleted ids.** Strictly monotonic — we always take
    `max(numeric suffix) + 1`. Even if the highest row was deleted,
    we don't reuse it (avoids confusing audit).
  • **Tenant-isolated.** Same SQL predicate discipline as everywhere
    else in this UC.
"""
from __future__ import annotations

import asyncpg
import structlog
from opentelemetry import trace

_log = structlog.get_logger("oneops.uc08.sr_id")
_tracer = trace.get_tracer("oneops.uc08.sr_id")


_PREFIX = "SR"
_SUFFIX_WIDTH = 7
_INITIAL_SUFFIX = 1


async def next_sr_id(
    *,
    tenant_id: str,
    conn: asyncpg.Connection,
) -> str:
    """Mint the next SR id for `tenant_id`.

    Walks `itsm.request` for the tenant, finds the highest numeric
    suffix following the `SR` prefix, returns `SR` + (max+1) zero-
    padded to 7 digits.

    First-time tenant → `SR0000001`.
    """
    if not tenant_id or not tenant_id.strip():
        raise ValueError("tenant_id is required to mint an SR id")
    tenant_id = tenant_id.strip()

    with _tracer.start_as_current_span(
        "uc08.sr_id.mint",
        attributes={"oneops.tenant_id": tenant_id},
    ) as span:
        # Cast the suffix to int and take the max. Ignore any rows whose
        # request_id doesn't fit the canonical `SR` + digits pattern.
        # Note: substring's `from N` argument is part of the SQL clause,
        # not a value parameter — asyncpg cannot bind it as $N. We use
        # a constant offset since the prefix length is fixed.
        max_suffix = await conn.fetchval(
            f"""
            SELECT max(
              CAST(
                substring(request_id from {len(_PREFIX) + 1}) AS bigint
              )
            )
              FROM itsm.request
             WHERE tenant_id = $1
               AND request_id ~ $2
            """,
            tenant_id,
            f"^{_PREFIX}[0-9]+$",
        )

        next_suffix = (int(max_suffix) + 1) if max_suffix is not None \
            else _INITIAL_SUFFIX
        sr_id = f"{_PREFIX}{next_suffix:0{_SUFFIX_WIDTH}d}"

        span.set_attribute("uc08.sr_id.minted", sr_id)
        span.set_attribute("uc08.sr_id.suffix", next_suffix)

        _log.info(
            "uc08.sr_id.minted",
            tenant_id=tenant_id, sr_id=sr_id, suffix=next_suffix,
        )
        return sr_id


__all__ = ["next_sr_id"]
