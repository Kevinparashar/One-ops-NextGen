"""User / tenant long-term profile store — substrate gap G1.

Verifies tenant partitioning, structured I/O, explicit error surface, and
no-silent-failure invariants.
"""
from __future__ import annotations

import pytest

from oneops.session.profile_store import (
    InMemoryUserProfileStore,
    get_user_profile_store,
    set_user_profile_store,
)


@pytest.fixture
def store() -> InMemoryUserProfileStore:
    s = InMemoryUserProfileStore()
    set_user_profile_store(s)
    return s


# ── round-trip ───────────────────────────────────────────────────────────


async def test_get_miss_returns_none_not_an_empty_dict(store):
    # An empty dict and a "no profile" must be distinguishable — UC code
    # often branches on existence ("is this a new user?").
    out = await store.get(tenant_id="t1", user_id="u1")
    assert out is None


async def test_put_then_get_round_trip(store):
    await store.put(
        tenant_id="t1", user_id="u1",
        profile={"preferred_language": "en", "escalations_q1": 3})
    out = await store.get(tenant_id="t1", user_id="u1")
    assert out is not None
    assert out["preferred_language"] == "en"
    assert out["escalations_q1"] == 3
    # `_updated_at` is auto-stamped — operators rely on this for staleness.
    assert "_updated_at" in out


# ── tenant partitioning — structural, not advisory ───────────────────────


async def test_one_tenants_profile_is_invisible_to_another(store):
    await store.put(
        tenant_id="tenant-a", user_id="u1",
        profile={"preferred_language": "en"})
    out = await store.get(tenant_id="tenant-b", user_id="u1")
    assert out is None


async def test_same_user_id_can_have_distinct_profiles_per_tenant(store):
    await store.put(tenant_id="tenant-a", user_id="u1",
                    profile={"role_label": "developer"})
    await store.put(tenant_id="tenant-b", user_id="u1",
                    profile={"role_label": "operator"})
    a = await store.get(tenant_id="tenant-a", user_id="u1")
    b = await store.get(tenant_id="tenant-b", user_id="u1")
    assert a is not None
    assert b is not None
    assert a["role_label"] == "developer"
    assert b["role_label"] == "operator"


# ── merge — shallow, deliberate ──────────────────────────────────────────


async def test_merge_creates_when_missing(store):
    out = await store.merge(
        tenant_id="t1", user_id="u1",
        patch={"escalations_q1": 1})
    assert out["escalations_q1"] == 1


async def test_merge_overlays_top_level_keys(store):
    await store.put(
        tenant_id="t1", user_id="u1",
        profile={"preferred_language": "en", "escalations_q1": 1})
    merged = await store.merge(
        tenant_id="t1", user_id="u1",
        patch={"escalations_q1": 2})
    assert merged["preferred_language"] == "en"     # kept
    assert merged["escalations_q1"] == 2            # overwritten


async def test_merge_is_shallow_not_deep(store):
    # Nested merge would silently drop sub-keys — by design we replace the
    # whole sub-tree under a top-level key.
    await store.put(
        tenant_id="t1", user_id="u1",
        profile={"prefs": {"language": "en", "tz": "UTC"}})
    merged = await store.merge(
        tenant_id="t1", user_id="u1",
        patch={"prefs": {"language": "fr"}})
    assert merged["prefs"] == {"language": "fr"}    # tz dropped — deliberate


# ── tenant_id / user_id mandatory ────────────────────────────────────────


async def test_get_with_missing_tenant_returns_none_not_raise(store):
    # Reads are tolerant of empty envelopes (router pre-flight may call
    # with partial context); writes are strict.
    assert await store.get(tenant_id="", user_id="u1") is None
    assert await store.get(tenant_id="t1", user_id="") is None


async def test_put_refuses_missing_tenant(store):
    with pytest.raises(ValueError, match="tenant_id"):
        await store.put(tenant_id="", user_id="u1", profile={"x": 1})


async def test_put_refuses_missing_user(store):
    with pytest.raises(ValueError, match="user_id"):
        await store.put(tenant_id="t1", user_id="", profile={"x": 1})


async def test_put_refuses_non_dict_profile(store):
    with pytest.raises(ValueError, match="profile must be a dict"):
        await store.put(tenant_id="t1", user_id="u1", profile="oops")  # type: ignore[arg-type]


async def test_merge_refuses_non_dict_patch(store):
    with pytest.raises(ValueError, match="patch must be a dict"):
        await store.merge(tenant_id="t1", user_id="u1", patch=[1, 2])   # type: ignore[arg-type]


# ── get returns a copy — no mutation back-channel ────────────────────────


async def test_get_returns_a_copy_not_the_internal_row(store):
    await store.put(tenant_id="t1", user_id="u1",
                    profile={"preferred_language": "en"})
    out1 = await store.get(tenant_id="t1", user_id="u1")
    out1["preferred_language"] = "tampered"          # type: ignore[index]
    out2 = await store.get(tenant_id="t1", user_id="u1")
    assert out2["preferred_language"] == "en"


# ── singleton accessor is cold-start safe ───────────────────────────────


def test_get_user_profile_store_returns_a_store():
    s = get_user_profile_store()
    assert s is not None
    # And it's stable across calls without doing I/O at import.
    assert get_user_profile_store() is s


# ── set_user_profile_store replaces the singleton ────────────────────────


async def test_set_user_profile_store_overrides_for_tests():
    a = InMemoryUserProfileStore()
    b = InMemoryUserProfileStore()
    set_user_profile_store(a)
    assert get_user_profile_store() is a
    set_user_profile_store(b)
    assert get_user_profile_store() is b
