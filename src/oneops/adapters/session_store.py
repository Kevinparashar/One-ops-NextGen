"""Session store — Dragonfly-backed multi-turn state.

Three keyspaces per session:
  session:{sid}    — conversation history (list[ConversationTurn] JSON)
  focus:{sid}      — active/mentioned/anchor subject + pending_clarification
  canonical:{sid}  — last_successful_use_case, turn_index, last_tool_results, recent_results

Concurrency model:
- Multiple worker replicas may serve the same session at the same time.
- Single-key writes (set / setex) are atomic at Dragonfly.
- Multi-field updates (update_focus, update_canonical_state) use Lua scripts so
  read-modify-write happens server-side under a single key lock; no Python race.
- Sliding TTL: every read AND every write refreshes the TTL via EXPIRE.

Lifecycle:
    store = await get_session_store()
    await store.append_history(sid, "user", "summarize INC0001001")
    focus = await store.get_focus(sid)
    await store.update_focus(sid, {"active_subject": {...}})

No process-wide singleton needed because SessionStore is stateless — it holds a
reference to the shared Redis client.
"""
from __future__ import annotations

from typing import Any

import orjson
import redis.asyncio as redis

from oneops.adapters.dragonfly import get_redis_client
from oneops.config import get_settings
from oneops.observability import get_logger, span

_log = get_logger("oneops.session_store")

# Telemetry span names + attribute keys (single source — sonar S1192).
_SPAN_LOAD = "state.load"
_SPAN_UPDATE = "state.update"
_ATTR_KIND = "state.kind"
_ATTR_FOUND = "state.found"
_ATTR_ERROR = "state.error"

# Lua: atomic "deep-merge" of a JSON map.
# Reads the existing JSON, parses it, top-level merges the new entries,
# writes it back, refreshes TTL. All under the key's single-shard lock.
_MERGE_JSON_LUA = """
local key = KEYS[1]
local updates = cjson.decode(ARGV[1])
local ttl = tonumber(ARGV[2])

local current_raw = redis.call('GET', key)
local current
if current_raw then
    current = cjson.decode(current_raw)
else
    current = {}
end
for k, v in pairs(updates) do
    current[k] = v
end
redis.call('SET', key, cjson.encode(current))
if ttl > 0 then
    redis.call('EXPIRE', key, ttl)
end
return 1
"""

# Lua: atomic append to a JSON-encoded list with optional trim and TTL refresh.
_APPEND_LIST_LUA = """
local key = KEYS[1]
local item = ARGV[1]
local max_len = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local current_raw = redis.call('GET', key)
local current
if current_raw then
    current = cjson.decode(current_raw)
else
    current = {}
end
table.insert(current, cjson.decode(item))
if max_len > 0 and #current > max_len then
    local excess = #current - max_len
    for i = 1, excess do table.remove(current, 1) end
end
redis.call('SET', key, cjson.encode(current))
if ttl > 0 then
    redis.call('EXPIRE', key, ttl)
end
return #current
"""


class SessionStore:
    """Thin facade over Dragonfly. Stateless across calls."""

    def __init__(self, client: redis.Redis, *, ttl_seconds: int, max_history: int = 50) -> None:
        self._r = client
        self._ttl = ttl_seconds
        self._max_history = max_history
        # Pre-register Lua scripts for atomic ops; first call sends the script,
        # subsequent calls send only the SHA1. Concurrency-safe (redis-py caches).
        self._merge_json = self._r.register_script(_MERGE_JSON_LUA)
        self._append_list = self._r.register_script(_APPEND_LIST_LUA)

    # ── Key helpers ──────────────────────────────────────────────
    @staticmethod
    def _history_key(session_id: str) -> str:
        return f"session:{session_id}"

    @staticmethod
    def _focus_key(session_id: str) -> str:
        return f"focus:{session_id}"

    @staticmethod
    def _canonical_key(session_id: str) -> str:
        return f"canonical:{session_id}"

    # ── Conversation history ─────────────────────────────────────
    async def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return list of {role, content, timestamp?}. Empty list if absent."""
        with span(_SPAN_LOAD, **{_ATTR_KIND: "history"}) as sp:
            raw = await self._r.get(self._history_key(session_id))
            if raw is None:
                sp.set_attribute(_ATTR_FOUND, False)
                return []
            await self._r.expire(self._history_key(session_id), self._ttl)
            try:
                items = list(orjson.loads(raw))
                sp.set_attribute(_ATTR_FOUND, True)
                sp.set_attribute("state.history_len", len(items))
                return items
            except orjson.JSONDecodeError as e:
                _log.warning("history.json.decode_failed", session_id=session_id, error=str(e))
                sp.set_attribute(_ATTR_ERROR, "json_decode")
                return []

    async def append_history(self, session_id: str, role: str, content: str) -> int:
        """Append a turn. Returns new list length. Trimmed to max_history."""
        with span(_SPAN_UPDATE, **{_ATTR_KIND: "history", "state.role": role, "state.content_len": len(content)}):
            item = orjson.dumps({"role": role, "content": content})
            new_len = await self._append_list(
                keys=[self._history_key(session_id)],
                args=[item, self._max_history, self._ttl],
            )
            return int(new_len)

    # ── Focus ────────────────────────────────────────────────────
    async def get_focus(self, session_id: str) -> dict[str, Any]:
        """Return focus dict (active_subject / mentioned_subject / anchor_subject /
        pending_clarification). Empty dict if absent."""
        with span(_SPAN_LOAD, **{_ATTR_KIND: "focus"}) as sp:
            raw = await self._r.get(self._focus_key(session_id))
            if raw is None:
                sp.set_attribute(_ATTR_FOUND, False)
                return {}
            await self._r.expire(self._focus_key(session_id), self._ttl)
            try:
                d = dict(orjson.loads(raw))
                sp.set_attribute(_ATTR_FOUND, True)
                sp.set_attribute("state.focus_keys", len(d))
                if "active_subject" in d and isinstance(d["active_subject"], dict):
                    eid = d["active_subject"].get("entity_id")
                    if eid:
                        sp.set_attribute("state.focus_entity_id", str(eid))
                return d
            except orjson.JSONDecodeError as e:
                _log.warning("focus.json.decode_failed", session_id=session_id, error=str(e))
                sp.set_attribute(_ATTR_ERROR, "json_decode")
                return {}

    async def update_focus(self, session_id: str, updates: dict[str, Any]) -> None:
        """Atomically top-level merge `updates` into focus state.

        Per CONVERSATION_STATE_POLICY:
          - Only call on a successful grounded response (caller's responsibility)
          - active_subject + mentioned_subject can be replaced
          - anchor_subject set once, never overwritten — caller enforces
        """
        if not updates:
            return
        with span(
            _SPAN_UPDATE,
            **{
                _ATTR_KIND: "focus",
                "state.update_keys": ",".join(sorted(updates.keys())),
            },
        ):
            await self._merge_json(
                keys=[self._focus_key(session_id)],
                args=[orjson.dumps(updates), self._ttl],
            )

    async def set_focus(self, session_id: str, focus: dict[str, Any]) -> None:
        """Replace focus state outright. Use sparingly; prefer update_focus."""
        with span(
            _SPAN_UPDATE,
            **{_ATTR_KIND: "focus", "state.operation": "set", "state.focus_keys": len(focus)},
        ):
            await self._r.set(
                self._focus_key(session_id),
                orjson.dumps(focus),
                ex=self._ttl,
            )

    async def clear_pending_clarification(self, session_id: str) -> None:
        """Clear only pending_clarification, preserving subjects."""
        await self._merge_json(
            keys=[self._focus_key(session_id)],
            args=[orjson.dumps({"pending_clarification": None}), self._ttl],
        )

    # ── Canonical state ──────────────────────────────────────────
    async def get_canonical_state(self, session_id: str) -> dict[str, Any]:
        """Return canonical_state dict. Empty dict if absent."""
        with span(_SPAN_LOAD, **{_ATTR_KIND: "canonical"}) as sp:
            raw = await self._r.get(self._canonical_key(session_id))
            if raw is None:
                sp.set_attribute(_ATTR_FOUND, False)
                return {}
            await self._r.expire(self._canonical_key(session_id), self._ttl)
            try:
                d = dict(orjson.loads(raw))
                sp.set_attribute(_ATTR_FOUND, True)
                sp.set_attribute("state.key_count", len(d))
                last_uc = d.get("last_successful_use_case")
                if last_uc:
                    sp.set_attribute("state.last_successful_use_case", str(last_uc))
                active_kb = d.get("active_kb_id")
                if active_kb:
                    sp.set_attribute("state.active_kb_id", str(active_kb))
                return d
            except orjson.JSONDecodeError as e:
                _log.warning("canonical.json.decode_failed", session_id=session_id, error=str(e))
                sp.set_attribute(_ATTR_ERROR, "json_decode")
                return {}

    async def update_canonical_state(self, session_id: str, updates: dict[str, Any]) -> None:
        """Atomically top-level merge `updates` into canonical_state."""
        if not updates:
            return
        with span(
            _SPAN_UPDATE,
            **{
                _ATTR_KIND: "canonical",
                "state.update_keys": ",".join(sorted(updates.keys())),
            },
        ):
            await self._merge_json(
                keys=[self._canonical_key(session_id)],
                args=[orjson.dumps(updates), self._ttl],
            )

    async def increment_turn(self, session_id: str) -> int:
        """Atomically increment turn_index. Returns the new value.

        Used at request entry to assign a stable per-turn index across all writes.
        """
        # We use HINCRBY-style semantics with a Lua script for portability with the
        # JSON-blob storage. Reads current turn_index, increments, writes back.
        lua = """
        local key = KEYS[1]
        local ttl = tonumber(ARGV[1])
        local raw = redis.call('GET', key)
        local cur
        if raw then cur = cjson.decode(raw) else cur = {} end
        cur.turn_index = (tonumber(cur.turn_index) or 0) + 1
        redis.call('SET', key, cjson.encode(cur))
        if ttl > 0 then redis.call('EXPIRE', key, ttl) end
        return cur.turn_index
        """
        script = self._r.register_script(lua)
        result = await script(keys=[self._canonical_key(session_id)], args=[self._ttl])
        return int(result)

    # ── Bulk delete (logout / TTL eviction) ──────────────────────
    async def purge(self, session_id: str) -> int:
        """Delete all keys for this session. Returns count deleted."""
        keys = [
            self._history_key(session_id),
            self._focus_key(session_id),
            self._canonical_key(session_id),
        ]
        return int(await self._r.delete(*keys))


# ── Module-level factory (per event loop) ──────────────────────
# SessionStore holds Lua script registrations against a Redis client whose
# socket is bound to one event loop. In production a single persistent loop
# hosts the whole service, so this behaves as a process-wide singleton. In
# tests, pytest-asyncio creates a fresh loop per test; each loop gets its
# own SessionStore. WeakKeyDictionary lets loop GC drop dead entries.
import asyncio as _asyncio
import weakref as _weakref

_stores: _weakref.WeakKeyDictionary[_asyncio.AbstractEventLoop, SessionStore] = (
    _weakref.WeakKeyDictionary()
)


async def get_session_store() -> SessionStore:
    """Get-or-create the SessionStore for the current event loop.

    SessionStore registers Lua scripts at construction and holds a Redis
    client reference whose socket is loop-bound. We key by event loop so
    different loops (e.g. per-test in pytest-asyncio) get isolated stores.
    """
    loop = _asyncio.get_running_loop()
    store = _stores.get(loop)
    if store is not None:
        return store
    settings = get_settings()
    client = await get_redis_client()
    store = SessionStore(client, ttl_seconds=settings.session_ttl_seconds)
    _stores[loop] = store
    return store


def reset_session_store_for_test() -> None:
    """Test helper: clear every cached SessionStore.

    Safe to call at any time — running coroutines holding their own
    reference to a store will continue to work until they finish; new
    `get_session_store()` calls will rebuild fresh instances.
    """
    _stores.clear()


__all__ = ["SessionStore", "get_session_store", "reset_session_store_for_test"]
