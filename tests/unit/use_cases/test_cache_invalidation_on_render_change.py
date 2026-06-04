"""Cache invalidation when the render schema changes.

The UC-1 summary fingerprint includes `HUMANISE_RECORD_VERSION` from
`field_labels.py`. Bumping the version invalidates ALL cached entries
automatically — no manual flush required. This test locks that contract.

Why this matters: on 2026-05-30 we discovered `search_tsv` and
`content_hash_*` were leaking into the summary card because they weren't in
`_HIDDEN`. The fix removed them — but cached entries built before the fix
would still return the leak. Including the version in the fingerprint makes
the bump itself the cache flush.
"""
from __future__ import annotations

from oneops.use_cases._shared import field_labels as fl
from oneops.use_cases.uc01_summarization.llm_summarizer import _fingerprint

_REC = {
    "tenant_id": "T001",
    "incident_id": "INC0001234",
    "title": "x",
    "status": "open",
}


def test_fingerprint_changes_when_render_version_changes(monkeypatch):
    """A version bump must produce a DIFFERENT key for the SAME record —
    that's the only way an in-flight cache entry from the old code path can
    be invalidated without a manual flush.
    """
    monkeypatch.setattr(fl, "HUMANISE_RECORD_VERSION", "v1")
    fp_v1 = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    monkeypatch.setattr(fl, "HUMANISE_RECORD_VERSION", "v2")
    fp_v2 = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    monkeypatch.setattr(fl, "HUMANISE_RECORD_VERSION", "v3")
    fp_v3 = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    assert fp_v1 != fp_v2 != fp_v3, (
        "Bumping HUMANISE_RECORD_VERSION must produce a different cache "
        "key — otherwise stale leaked entries would still be served."
    )


def test_fingerprint_stable_when_version_unchanged():
    """Same version + same record → same key. Caching depends on this."""
    a = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    b = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    assert a == b


def test_fingerprint_changes_when_tenant_changes():
    """Independent invariant — protect against the version-axis change
    accidentally breaking the tenant-axis isolation."""
    a = _fingerprint(
        tenant_id="T001", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    b = _fingerprint(
        tenant_id="T002", service_id="incident",
        entity_id="INC0001234", record=_REC,
    )
    assert a != b


def test_current_version_is_at_least_v2():
    """The leak fix was v2. If someone reverts it to v1, this test fails
    and the leak comes back. Treat this as a do-not-regress guard."""
    v = fl.HUMANISE_RECORD_VERSION
    assert v >= "v2", (
        f"HUMANISE_RECORD_VERSION is {v!r}; v2 is the minimum that includes "
        "the search_tsv + content_hash_* leak fix. Bumping is fine; "
        "reverting brings back the production leak."
    )


def test_render_filter_blocks_search_tsv_today():
    """The actual leak. The test above guards the *cache key*; this guards
    the *filter*. Both must hold or the fix isn't real."""
    rec = {
        "tenant_id": "T001",
        "incident_id": "INC0001234",
        "status": "open",
        "search_tsv": "'foo':1 'bar':2",
        "content_hash": b"\x01\x02",
        "content_hash_symptom": b"\xff\xee",
        "content_hash_diagnosis": b"\xdd\xcc",
    }
    rendered = fl.humanise_record(rec)
    for k in rendered:
        kl = k.lower()
        assert "tsv" not in kl, f"search_tsv leaked as {k!r}"
        assert "hash" not in kl, f"content_hash leaked as {k!r}"
