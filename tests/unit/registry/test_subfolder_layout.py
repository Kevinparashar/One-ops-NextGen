"""Per-UC subfolder layout — production-grade FileBackend tests (2026-05-31).

Asserts the recursive-discovery + duplicate-id-guard behaviour:

  • Tools placed under `tools/uc01_summarization/get_ticket.json` are discovered
  • Tools placed under `tools/shared/notify.json` are also discovered
  • Mixed flat + subfolder layout still works (backward-compatibility)
  • Same `record_id.json` in two different subfolders raises
    `RegistryDuplicateIdError` at discovery time
  • `read()` finds files at any depth
  • `write()` of an existing record updates its current location
    (doesn't create a duplicate at the top level)
  • `write()` of a brand-new record lands at the top of the kind dir
"""
from __future__ import annotations

import pytest

from oneops.errors import RegistryDuplicateIdError
from oneops.registry.store import FileBackend


def _env(rid: str, version: int = 1) -> dict:
    """Minimal valid envelope shape — backend doesn't parse contents, so we
    can store anything that round-trips as JSON."""
    return {
        "id": rid,
        "active_version": version,
        "versions": {str(version): {"status": "active", "version": version}},
    }


# ── recursive discovery ──────────────────────────────────────────────────────


def test_subfolder_record_is_discovered(tmp_path):
    """A file under `tools/uc01_summarization/get_ticket.json` is found by
    list_ids() and readable via read()."""
    backend = FileBackend(str(tmp_path))
    uc_dir = tmp_path / "tools" / "uc01_summarization"
    uc_dir.mkdir(parents=True)
    backend.write("tools", "get_ticket", _env("get_ticket"))
    # The write went to the top level (new record); move it into the UC dir
    src = tmp_path / "tools" / "get_ticket.json"
    dst = uc_dir / "get_ticket.json"
    src.rename(dst)
    ids = backend.list_ids("tools")
    assert ids == ["get_ticket"], ids
    rec = backend.read("tools", "get_ticket")
    assert rec is not None
    assert rec["id"] == "get_ticket"


def test_mixed_flat_and_subfolder_layout_works(tmp_path):
    """Backward compatibility — existing flat layout still works alongside
    per-UC subfolders during gradual migration."""
    backend = FileBackend(str(tmp_path))
    # Flat tool (legacy)
    backend.write("tools", "flat_tool", _env("flat_tool"))
    # Subfolder tool (modern)
    (tmp_path / "tools" / "uc01_summarization").mkdir(parents=True, exist_ok=True)
    backend.write("tools", "sub_tool", _env("sub_tool"))
    src = tmp_path / "tools" / "sub_tool.json"
    src.rename(tmp_path / "tools" / "uc01_summarization" / "sub_tool.json")
    ids = backend.list_ids("tools")
    assert ids == ["flat_tool", "sub_tool"], ids


def test_shared_folder_discovered(tmp_path):
    """A `tools/shared/` folder for cross-UC reusable tools is discovered."""
    backend = FileBackend(str(tmp_path))
    (tmp_path / "tools" / "shared").mkdir(parents=True, exist_ok=True)
    backend.write("tools", "notify_milestone", _env("notify_milestone"))
    src = tmp_path / "tools" / "notify_milestone.json"
    src.rename(tmp_path / "tools" / "shared" / "notify_milestone.json")
    assert backend.list_ids("tools") == ["notify_milestone"]
    rec = backend.read("tools", "notify_milestone")
    assert rec is not None


def test_deeply_nested_subfolder_is_discovered(tmp_path):
    """Recursive walk goes more than one level deep — e.g. for
    `tools/uc08_fulfillment/identity/create_account.json`."""
    backend = FileBackend(str(tmp_path))
    nested = tmp_path / "tools" / "uc08_fulfillment" / "identity"
    nested.mkdir(parents=True)
    (nested / "create_account.json").write_text(
        '{"id":"create_account","active_version":1,"versions":{"1":{"status":"active","version":1}}}')
    assert backend.list_ids("tools") == ["create_account"]


# ── duplicate-id guard ──────────────────────────────────────────────────────


def test_duplicate_record_id_in_two_subfolders_raises(tmp_path):
    """Same `record_id.json` in two subfolders is a config bug — discovery
    must raise loudly rather than silently picking one (which is the
    failure mode the recursive walker is designed to prevent)."""
    base = tmp_path / "tools"
    (base / "uc01_summarization").mkdir(parents=True)
    (base / "shared").mkdir(parents=True)
    (base / "uc01_summarization" / "notify.json").write_text(
        '{"id":"notify","active_version":1,"versions":{"1":{"status":"active","version":1}}}')
    (base / "shared" / "notify.json").write_text(
        '{"id":"notify","active_version":1,"versions":{"1":{"status":"active","version":1}}}')
    backend = FileBackend(str(tmp_path))
    with pytest.raises(RegistryDuplicateIdError) as ex:
        backend.list_ids("tools")
    msg = str(ex.value)
    assert "notify" in msg
    assert "uc01_summarization" in msg or "shared" in msg


def test_read_of_duplicate_record_raises(tmp_path):
    """Even direct read() must raise on duplicate — defense in depth."""
    base = tmp_path / "tools"
    (base / "uc01_summarization").mkdir(parents=True)
    (base / "shared").mkdir(parents=True)
    (base / "uc01_summarization" / "notify.json").write_text(
        '{"id":"notify","active_version":1,"versions":{"1":{"status":"active","version":1}}}')
    (base / "shared" / "notify.json").write_text(
        '{"id":"notify","active_version":1,"versions":{"1":{"status":"active","version":1}}}')
    backend = FileBackend(str(tmp_path))
    with pytest.raises(RegistryDuplicateIdError):
        backend.read("tools", "notify")


# ── write semantics with subfolders ──────────────────────────────────────────


def test_write_to_existing_record_preserves_subfolder_location(tmp_path):
    """When a record already lives in a subfolder, write() must update IT —
    not create a duplicate at the top level (which would silently break
    list_ids() on the next call due to the duplicate-id guard)."""
    base = tmp_path / "tools" / "uc01_summarization"
    base.mkdir(parents=True)
    initial = (base / "get_ticket.json")
    initial.write_text(
        '{"id":"get_ticket","active_version":1,'
        '"versions":{"1":{"status":"active","version":1,"note":"v1"}}}')
    backend = FileBackend(str(tmp_path))
    # Update with a new envelope
    backend.write("tools", "get_ticket",
                  {"id": "get_ticket", "active_version": 2,
                   "versions": {"1": {"status": "retired", "version": 1},
                                "2": {"status": "active", "version": 2,
                                      "note": "v2"}}})
    # Same location, not duplicated at the top
    assert (base / "get_ticket.json").is_file()
    assert not (tmp_path / "tools" / "get_ticket.json").is_file()
    rec = backend.read("tools", "get_ticket")
    assert rec is not None
    assert rec["active_version"] == 2
