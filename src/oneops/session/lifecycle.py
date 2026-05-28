"""Session lifecycle — server-owned create / touch / list / delete.

The `SessionEventStore` (store.py) holds *conversation events*. Lifecycle
metadata (`created_at` / `last_active_at` / `expires_at` / `state`
/ `user_id`) is a separate concern owned here so the event log stays
append-only and the lifecycle gets its own retention rules.

Server-driven model:
  * Client never mints a session id. `POST /api/sessions` is the only way
    to create one, so two clients cannot collide and a stale localStorage
    cannot resurrect a dead session.
  * Sliding idle TTL — `touch(tid, sid)` on every successful chat turn
    bumps the metadata-key TTL to `SESSION_IDLE_TTL_MINUTES` from now. A
    session that goes 30 min without a turn auto-expires from Dragonfly.
  * Per-user index (`oneops:session:by_user:{tid}:{uid}`) is a sorted set
    keyed by `last_active_at` (descending) → cheap "list my recent
    conversations" for the sidebar.

Tenant isolation: every key is prefixed with `tenant_id`. A session id
cannot be retrieved or touched from a different tenant.

Failure shape: every operation is best-effort with a typed return. A
Dragonfly outage cannot wedge the chat — the chat path falls through
to in-flight create when lifecycle returns None.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from oneops.observability import get_logger, get_tracer

# Charset-safe regex for client-supplied session_ids. Length-bounded to
# prevent abuse; charset matches what the server emits (`sess_<hex>`)
# plus common client conventions (underscores, hyphens, digits, letters).
# A failed match falls through to fresh-mint — never silent corruption.
_CLIENT_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _is_safe_client_session_id(sid: str) -> bool:
    """True if `sid` is a non-empty, charset-safe, length-bounded token
    suitable for adoption as a session_id. The check is structural — no
    semantic interpretation of the value, no list of "known good" forms."""
    return bool(sid) and bool(_CLIENT_SID_RE.match(sid))

_log = get_logger("oneops.session.lifecycle")
_tracer = get_tracer("oneops.session.lifecycle")


def _meta_key(tenant_id: str, session_id: str) -> str:
    return f"oneops:session:meta:{tenant_id}:{session_id}"


def _user_index_key(tenant_id: str, user_id: str) -> str:
    return f"oneops:session:by_user:{tenant_id}:{user_id}"


@dataclass(frozen=True)
class SessionMeta:
    """All lifecycle metadata for one session. Returned by `get()`."""
    session_id: str
    tenant_id: str
    user_id: str
    created_at_unix_ms: int
    last_active_at_unix_ms: int
    expires_at_unix_ms: int
    state: str = "active"                          # "active" | "closed"
    title: str = ""                                # first-user-message preview
    turn_count: int = 0

    def is_expired(self, *, now_unix_ms: int | None = None) -> bool:
        now = now_unix_ms if now_unix_ms is not None else int(time.time() * 1000)
        return now >= self.expires_at_unix_ms

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "created_at_unix_ms": self.created_at_unix_ms,
            "last_active_at_unix_ms": self.last_active_at_unix_ms,
            "expires_at_unix_ms": self.expires_at_unix_ms,
            "state": self.state,
            "title": self.title,
            "turn_count": self.turn_count,
        }


class _NullLifecycle:
    """Fallback when Dragonfly is unreachable at construction time.

    Every method is a no-op that returns a "lifecycle unavailable" shape.
    The chat path falls back to its prior behaviour (client-side
    session_id) so a Dragonfly outage cannot down the whole service."""
    async def create(self, *, tenant_id: str, user_id: str,
                     title: str = "",
                     session_id: str | None = None,
                     ) -> SessionMeta | None: return None
    async def get(self, *, tenant_id: str, session_id: str
                  ) -> SessionMeta | None: return None
    async def touch(self, *, tenant_id: str, session_id: str,
                    user_id: str = "", title: str = "",
                    bump_turn_count: bool = True) -> SessionMeta | None: return None
    async def list_for_user(self, *, tenant_id: str, user_id: str,
                            limit: int = 20) -> list[SessionMeta]: return []
    async def delete(self, *, tenant_id: str, session_id: str
                     ) -> bool: return False


class DragonflyLifecycle:
    """Production lifecycle store over Dragonfly (Redis protocol).

    Keys:
      * `oneops:session:meta:{tid}:{sid}` (HASH, sliding TTL) — metadata.
      * `oneops:session:by_user:{tid}:{uid}` (ZSET, sliding TTL) — index
        of session_ids by `last_active_at_unix_ms` (the score), so the
        sidebar can list "this user's 20 most recent sessions" in one
        ZREVRANGE.
    """

    def __init__(self, client: Any, *,
                 idle_ttl_seconds: int = 1800,            # 30 min default
                 list_index_ttl_seconds: int = 7 * 24 * 3600) -> None:
        self._redis = client
        self._idle = idle_ttl_seconds
        self._index_ttl = list_index_ttl_seconds

    @classmethod
    def from_settings(cls) -> "DragonflyLifecycle | _NullLifecycle":
        """Build over a client from `DRAGONFLY_URL`; falls back to a
        no-op lifecycle if Dragonfly can't be constructed."""
        try:
            import redis.asyncio as aioredis
            from oneops.config import get_settings
            settings = get_settings()
            client = aioredis.from_url(
                getattr(settings, "dragonfly_url", "redis://localhost:6379/0"),
                decode_responses=False,
            )
            idle_min = int(os.getenv("SESSION_IDLE_TTL_MINUTES", "30"))
            return cls(client, idle_ttl_seconds=idle_min * 60)
        except Exception as exc:
            _log.warning("session.lifecycle.init_failed",
                         error=str(exc)[:160])
            return _NullLifecycle()

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _new_session_id() -> str:
        return f"sess_{uuid.uuid4().hex[:24]}"

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _expiry_ms(self, last_active_ms: int) -> int:
        return last_active_ms + self._idle * 1000

    @staticmethod
    def _decode(raw: Any) -> str:
        if raw is None:
            return ""
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def _meta_from_hash(self, h: dict[bytes, bytes], *,
                        session_id: str, tenant_id: str) -> SessionMeta | None:
        if not h:
            return None
        # bytes-keyed dict from Dragonfly
        g = lambda k: self._decode(h.get(k.encode()) if isinstance(list(h.keys())[0], bytes) else h.get(k))
        try:
            return SessionMeta(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=g("user_id"),
                created_at_unix_ms=int(g("created_at_unix_ms") or 0),
                last_active_at_unix_ms=int(g("last_active_at_unix_ms") or 0),
                expires_at_unix_ms=int(g("expires_at_unix_ms") or 0),
                state=g("state") or "active",
                title=g("title") or "",
                turn_count=int(g("turn_count") or 0),
            )
        except (ValueError, TypeError) as exc:
            _log.warning("session.lifecycle.meta_decode_failed",
                         session_id=session_id, error=str(exc)[:160])
            return None

    # ── API ───────────────────────────────────────────────────────────────

    async def create(self, *, tenant_id: str, user_id: str,
                     title: str = "",
                     session_id: str | None = None,
                     ) -> SessionMeta | None:
        """Create a session.

        When `session_id` is provided and passes structural validation
        (`_is_safe_client_session_id`), it is **adopted** — the new session
        is keyed by the caller-supplied id. This is how the chat door
        becomes symmetric with the rest of the substrate: LangGraph
        thread_ids and per-UC cache keys all accept caller-supplied ids
        with the same charset/length contract, so a curl/test/partner
        client behaves identically to the frontend on first turn.

        When `session_id` is None or fails validation, a fresh id is
        minted (the existing behaviour). The caller is informed of the
        actual id via the returned `SessionMeta.session_id` — clients
        should always re-stamp from the response, never assume the
        request value was kept.

        Tenant isolation is preserved: every key is composed
        `(tenant_id, session_id)`, so a client cannot adopt an id in
        another tenant's namespace. The risk of within-tenant id
        collision is bounded by the 128-bit hex space when clients
        follow the canonical `sess_<32-hex>` convention.
        """
        if not tenant_id or not user_id:
            return None
        with _tracer.start_as_current_span(
            "session.lifecycle.create",
            attributes={"oneops.tenant_id": tenant_id,
                        "oneops.user_id": user_id},
        ) as span:
            now = self._now_ms()
            if session_id and _is_safe_client_session_id(session_id):
                sid = session_id
                span.set_attribute("session.id_source", "client_adopted")
            else:
                sid = self._new_session_id()
                span.set_attribute(
                    "session.id_source",
                    "server_minted_invalid_client_id" if session_id
                    else "server_minted_no_client_id")
            expires = self._expiry_ms(now)
            meta = SessionMeta(
                session_id=sid, tenant_id=tenant_id, user_id=user_id,
                created_at_unix_ms=now, last_active_at_unix_ms=now,
                expires_at_unix_ms=expires, state="active",
                title=title or "", turn_count=0,
            )
            try:
                pipe = self._redis.pipeline()
                pipe.hset(_meta_key(tenant_id, sid), mapping={
                    "user_id": meta.user_id,
                    "created_at_unix_ms": meta.created_at_unix_ms,
                    "last_active_at_unix_ms": meta.last_active_at_unix_ms,
                    "expires_at_unix_ms": meta.expires_at_unix_ms,
                    "state": meta.state,
                    "title": meta.title,
                    "turn_count": meta.turn_count,
                })
                pipe.expire(_meta_key(tenant_id, sid), self._idle)
                pipe.zadd(_user_index_key(tenant_id, user_id), {sid: float(now)})
                pipe.expire(_user_index_key(tenant_id, user_id), self._index_ttl)
                await pipe.execute()
            except Exception as exc:
                _log.warning("session.lifecycle.create_failed",
                             error=str(exc)[:160])
                return None
            return meta

    async def get(self, *, tenant_id: str, session_id: str
                  ) -> SessionMeta | None:
        if not tenant_id or not session_id:
            return None
        try:
            h = await self._redis.hgetall(_meta_key(tenant_id, session_id))
        except Exception as exc:
            _log.warning("session.lifecycle.get_failed",
                         session_id=session_id, error=str(exc)[:160])
            return None
        meta = self._meta_from_hash(h, session_id=session_id, tenant_id=tenant_id)
        if meta is None or meta.is_expired():
            return None
        return meta

    async def touch(self, *, tenant_id: str, session_id: str,
                    user_id: str = "", title: str = "",
                    bump_turn_count: bool = True) -> SessionMeta | None:
        """Slide the TTL and update last_active_at + turn_count.

        Idempotent on the same turn — safe to call from each step of a
        multi-step plan; the metadata reflects the LAST call within the
        idle window. Title is only written when currently empty (so the
        first user message becomes the session title and subsequent
        turns don't churn it)."""
        if not tenant_id or not session_id:
            return None
        with _tracer.start_as_current_span(
            "session.lifecycle.touch",
            attributes={"oneops.tenant_id": tenant_id,
                        "session.id": session_id},
        ):
            now = self._now_ms()
            expires = self._expiry_ms(now)
            try:
                # Read first so we don't clobber an empty title with a later turn's text.
                h = await self._redis.hgetall(_meta_key(tenant_id, session_id))
                existing = self._meta_from_hash(
                    h, session_id=session_id, tenant_id=tenant_id)
                if existing is None:
                    # Touch on a non-existent session — common at first turn after
                    # mint, race-OK. Build the meta the same way create() would.
                    if not user_id:
                        return None
                    return await self.create(
                        tenant_id=tenant_id, user_id=user_id, title=title)
                if existing.state == "closed":
                    return None
                new_title = existing.title or title
                new_turn_count = existing.turn_count + (1 if bump_turn_count else 0)
                pipe = self._redis.pipeline()
                pipe.hset(_meta_key(tenant_id, session_id), mapping={
                    "last_active_at_unix_ms": now,
                    "expires_at_unix_ms": expires,
                    "title": new_title,
                    "turn_count": new_turn_count,
                })
                pipe.expire(_meta_key(tenant_id, session_id), self._idle)
                pipe.zadd(_user_index_key(tenant_id, existing.user_id),
                          {session_id: float(now)})
                pipe.expire(_user_index_key(tenant_id, existing.user_id),
                            self._index_ttl)
                await pipe.execute()
            except Exception as exc:
                _log.warning("session.lifecycle.touch_failed",
                             session_id=session_id, error=str(exc)[:160])
                return None
            return SessionMeta(
                session_id=session_id, tenant_id=tenant_id,
                user_id=existing.user_id,
                created_at_unix_ms=existing.created_at_unix_ms,
                last_active_at_unix_ms=now,
                expires_at_unix_ms=expires,
                state=existing.state, title=new_title,
                turn_count=new_turn_count)

    async def list_for_user(self, *, tenant_id: str, user_id: str,
                            limit: int = 20) -> list[SessionMeta]:
        if not tenant_id or not user_id:
            return []
        try:
            sids = await self._redis.zrevrange(
                _user_index_key(tenant_id, user_id), 0, max(0, limit - 1))
        except Exception as exc:
            _log.warning("session.lifecycle.list_failed",
                         error=str(exc)[:160])
            return []
        out: list[SessionMeta] = []
        for raw in sids:
            sid = self._decode(raw)
            meta = await self.get(tenant_id=tenant_id, session_id=sid)
            if meta is not None:
                out.append(meta)
            else:
                # Expired or deleted — drop from the index to keep it tidy.
                try:
                    await self._redis.zrem(
                        _user_index_key(tenant_id, user_id), sid)
                except Exception:
                    pass
        return out

    async def delete(self, *, tenant_id: str, session_id: str) -> bool:
        if not tenant_id or not session_id:
            return False
        try:
            # Need user_id to remove from the index — read meta first.
            h = await self._redis.hgetall(_meta_key(tenant_id, session_id))
            meta = self._meta_from_hash(
                h, session_id=session_id, tenant_id=tenant_id)
            pipe = self._redis.pipeline()
            pipe.delete(_meta_key(tenant_id, session_id))
            if meta is not None:
                pipe.zrem(_user_index_key(tenant_id, meta.user_id), session_id)
            await pipe.execute()
        except Exception as exc:
            _log.warning("session.lifecycle.delete_failed",
                         session_id=session_id, error=str(exc)[:160])
            return False
        return True


_lifecycle: Any = None


def get_lifecycle() -> Any:
    """Process-wide lifecycle store. Lazily constructed from settings."""
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = DragonflyLifecycle.from_settings()
    return _lifecycle


def set_lifecycle(impl: Any) -> None:
    """Tests inject a fake; FaaS wiring injects a custom backend."""
    global _lifecycle
    _lifecycle = impl


__all__ = [
    "SessionMeta",
    "DragonflyLifecycle",
    "get_lifecycle",
    "set_lifecycle",
]
