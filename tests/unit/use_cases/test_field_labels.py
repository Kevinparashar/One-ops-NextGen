"""Field-label humanisation — UC-1 cross-service Key Details contract.

Verifies the operator-facing output format:
  * Operationally-important fields (Status, Priority, Severity, …) come
    FIRST, ahead of service-specific primary-key fields.
  * Values are pre-formatted: dates → "Month Day, Year [HH:MM UTC]",
    booleans → Yes/No, work_notes/comments/attachments → readable strings
    (never raw JSON).
  * Restricted/internal fields stay hidden.
  * Same function works for every ITSM service (incident, request,
    problem, change, asset, cmdb_ci, knowledge).
"""
from __future__ import annotations

from oneops.use_cases._shared.field_labels import humanise_record

# ── operationally-important fields come first ─────────────────────────


def test_incident_state_fields_come_before_primary_key():
    row = {
        "tenant_id": "T001",
        "incident_id": "INC0001001",
        "title": "VPN drops every few minutes",
        "status": "in_progress",
        "priority": "P2",
        "severity": "high",
        "impact": "medium",
        "urgency": "high",
        "category": "Network",
        "subcategory": "VPN",
        "service_name": "Corporate VPN",
        "reported_by": "USR00009",
        "assigned_to": "USR00003",
        "assignment_group": "GRP-NETOPS",
        "ci_id": "CI0000003",
        "linked_ci_ids": ["CI0000003", "CI0000004"],
        "sla_breached": False,
        "helpful_votes": 0,                              # not in known labels — survives
    }
    out = humanise_record(row)
    keys = list(out)
    # Status is the first thing an operator should see, NOT Incident ID.
    assert keys[0] == "Status"
    # State fields cluster at the top.
    state_block = keys[:7]
    assert "Status" in state_block
    assert "Priority" in state_block
    assert "Severity" in state_block
    assert "Impact" in state_block
    assert "Urgency" in state_block
    # Classification follows state.
    assert keys.index("Category") < keys.index("Reported By")
    # People follow classification.
    assert keys.index("Reported By") < keys.index("Configuration Item")
    # Linked records follow people.
    assert keys.index("Configuration Item") < keys.index("SLA Breached")
    # Title (long-form) and Incident ID can land anywhere coherent —
    # they're not in the top state slice.
    assert "Status" in keys[:5]
    # tenant_id hidden.
    assert "Tenant Id" not in out


# ── values are formatted for humans, not raw ──────────────────────────


def test_booleans_render_as_yes_no():
    out = humanise_record({
        "incident_id": "INC0001001", "title": "x",
        "sla_breached": False,
    })
    assert out["SLA Breached"] == "No"


def test_true_booleans_render_as_yes():
    out = humanise_record({
        "problem_id": "PBM0003003", "title": "x",
        "known_error": True,
    })
    assert out["Known Error"] == "Yes"


def test_iso_datetimes_render_as_human_date():
    out = humanise_record({
        "incident_id": "INC0001001",
        "title": "x",
        "sla_due": "2026-04-01T17:10:00+00:00",
        "created_at": "2026-04-01T09:10:00+00:00",
    })
    # "Month Day, Year HH:MM UTC" — the user-facing shape.
    assert "April" in out["SLA Due"]
    assert "2026" in out["SLA Due"]
    assert "17:10" in out["SLA Due"]
    assert "UTC" in out["SLA Due"]


def test_date_only_strings_render_without_time():
    out = humanise_record({
        "asset_id": "AST0001001",
        "asset_name": "Cisco 9800-CL",
        "purchase_date": "2024-08-15",
    })
    assert "August" in out["Purchase Date"]
    assert "2024" in out["Purchase Date"]
    assert ":" not in out["Purchase Date"]                # no time component


# ── work_notes / comments / attachments → readable, NEVER raw JSON ────


def test_work_notes_render_as_readable_strings_not_json():
    out = humanise_record({
        "incident_id": "INC0001001",
        "title": "x",
        "work_notes": [
            {"note_id": "WN1", "is_public": False,
             "author": "USR00003", "author_role": "agent",
             "timestamp": "2026-04-01T09:35:00Z",
             "text": "Reviewed firewall logs."},
            {"note_id": "WN2", "is_public": False,
             "author": "USR00003", "author_role": "agent",
             "timestamp": "2026-04-01T14:50:00Z",
             "text": "Root cause confirmed."},
        ],
    })
    notes = out["Work Notes"]
    assert isinstance(notes, list)
    assert len(notes) == 2
    # Each note is a STRING, not a dict; never raw JSON
    for note in notes:
        assert isinstance(note, str)
        assert "{" not in note                            # no JSON braces
        assert "USR00003" in note                         # author surfaced
    # First note carries the date, author, text.
    assert "April" in notes[0]
    assert "Reviewed firewall logs" in notes[0]
    assert "internal" in notes[0].lower()                  # private notes flagged


def test_comments_render_as_readable_strings_not_json():
    out = humanise_record({
        "incident_id": "INC0001001",
        "title": "x",
        "comments": [
            {"comment_id": "C1", "is_public": True,
             "author": "USR00009", "author_role": "customer",
             "timestamp": "2026-04-01T09:10:00Z",
             "text": "VPN drops every time I walk past the elevators."},
        ],
    })
    comments = out["Comments"]
    assert isinstance(comments, list)
    assert all(isinstance(c, str) for c in comments)
    assert "USR00009" in comments[0]
    assert "elevators" in comments[0]
    assert "{" not in comments[0]


def test_attachments_render_as_readable_strings_not_json():
    out = humanise_record({
        "incident_id": "INC0001001",
        "title": "x",
        "attachments": [
            {"attachment_id": "A1", "name": "vpn_drop.png",
             "size_bytes": 184_320, "mime_type": "image/png",
             "uploaded_at": "2026-04-01T09:14:00Z"},
        ],
    })
    attachments = out["Attachments"]
    assert isinstance(attachments, list)
    assert "vpn_drop.png" in attachments[0]
    assert "image/png" in attachments[0]
    assert "180" in attachments[0]                        # 184320 bytes ≈ 180 KB
    assert "{" not in attachments[0]


# ── per-service: every supported service produces the right shape ────


def test_request_record_uses_request_labels():
    row = {
        "request_id": "REQ0001001", "title": "Onboard new hire",
        "status": "open", "stage": "approval",
        "catalog_item_id": "CAT0000010",
        "requested_for": "USR00007",
    }
    out = humanise_record(row)
    assert list(out)[0] == "Status"                       # state first
    assert out["Status"] == "open"
    assert out["Stage"] == "approval"
    assert out["Catalog Item"] == "CAT0000010"
    assert out["Requested For"] == "USR00007"
    assert out["Request ID"] == "REQ0001001"
    # title / description are NOT in Key Details — they belong to the
    # summary paragraph the LLM produces, and repeating them clutters.
    assert "Title" not in out


def test_problem_record_uses_problem_labels():
    row = {
        "problem_id": "PBM0003003", "title": "Repeated VPN drops",
        "status": "open",
        "root_cause": "RADIUS timeout",
        "workaround": "Restart tunnel",
        "known_error": True,
        "related_incidents": ["INC0001001"],
    }
    out = humanise_record(row)
    assert list(out)[0] == "Status"
    assert out["Root Cause"] == "RADIUS timeout"
    assert out["Workaround"] == "Restart tunnel"
    assert out["Known Error"] == "Yes"


def test_change_record_uses_change_labels():
    row = {
        "change_id": "CHG0004007", "title": "Patch core switch",
        "state": "scheduled", "change_type": "normal",
        "risk_level": "medium",
        "affected_ci": ["CI0000003"],
    }
    out = humanise_record(row)
    # State (or Status) is first.
    assert list(out)[0] in {"State", "Status", "Type", "Risk Level"}
    assert out["Type"] == "normal"
    assert out["Risk Level"] == "medium"
    assert out["Affected CIs"] == ["CI0000003"]


def test_asset_record_uses_asset_labels():
    row = {
        "asset_id": "AST0001006", "asset_name": "Cisco 9800-CL",
        "asset_class": "network", "status": "active",
        "model": "9800-CL", "vendor": "Cisco",
        "serial_number": "FCH2401ABC",
    }
    out = humanise_record(row)
    assert out["Asset Name"] == "Cisco 9800-CL"
    assert out["Class"] == "network"
    assert out["Vendor"] == "Cisco"
    assert out["Serial Number"] == "FCH2401ABC"


def test_cmdb_ci_record_uses_ci_labels():
    row = {
        "ci_id": "CI0000003", "ci_name": "WiFi Controller HQ-01",
        "ci_type": "network", "environment": "production",
        "criticality": "high", "owner": "USR00003",
        "location": "Bangalore-HQ",
        "attributes": {"model": "9800-CL", "ap_count": 47},
        "status": "active",
    }
    out = humanise_record(row)
    assert out["CI Name"] == "WiFi Controller HQ-01"
    assert out["CI Type"] == "network"
    assert out["Environment"] == "production"
    assert out["Criticality"] == "high"
    assert out["Location"] == "Bangalore-HQ"
    assert out["Owner"] == "USR00003"


def test_kb_article_uses_knowledge_labels():
    row = {
        "kb_id": "KB0005010", "title": "VPN troubleshooting",
        "category": "Networking", "audience": "end_user",
        "tags": ["vpn", "network"], "views": 42, "helpful_votes": 7,
        "embedding": [0.0, 0.0],                          # noise — dropped
    }
    out = humanise_record(row)
    assert out["Article ID"] == "KB0005010"
    assert out["Views"] == 42
    assert out["Helpful Votes"] == 7
    assert "Embedding" not in out                         # internal field hidden


# ── unknown fields auto-humanise + survive ────────────────────────────


def test_unknown_field_falls_back_to_autohumanise():
    out = humanise_record({"incident_id": "INC0001001", "title": "x",
                           "custom_metric_value": 42})
    assert out["Custom Metric Value"] == 42


def test_id_token_is_uppercased_in_autohumanise():
    out = humanise_record({"request_id": "REQ0001001", "title": "x",
                           "external_ref_id": "EXT-9"})
    assert out["External Ref ID"] == "EXT-9"


# ── empties / hidden ──────────────────────────────────────────────────


def test_none_values_are_dropped():
    out = humanise_record({"incident_id": "INC0001001", "title": "x",
                           "description": None})
    assert "Description" not in out


def test_empty_list_is_dropped():
    out = humanise_record({"incident_id": "INC0001001", "title": "x",
                           "work_notes": []})
    assert "Work Notes" not in out


# ── Production-grade: internal search/embedding columns NEVER leak ────────

def test_search_tsv_is_hidden():
    """itsm.* tables carry a `search_tsv` tsvector column. The summary card
    UI would render the raw lexeme dump verbatim, which is unreadable and
    leaks substrate detail."""
    out = humanise_record({
        "incident_id": "INC0001001", "title": "x", "status": "open",
        "search_tsv": "'foo':1 'bar':2 'baz':3",
    })
    assert not any("search" in k.lower() and "tsv" in k.lower() for k in out)


def test_content_hashes_are_hidden():
    """The embedding worker stores per-chunk content hashes for cache-gating;
    they're binary blobs and must never reach the response."""
    out = humanise_record({
        "incident_id": "INC0001001", "title": "x", "status": "open",
        "content_hash": b"\x01\x02\x03",
        "content_hash_symptom": b"\xff\xee\xdd",
        "content_hash_diagnosis": b"\x00\x11\x22",
        "content_hash_kb": b"\x33\x44\x55",
    })
    for k in out:
        assert "hash" not in k.lower(), \
            f"binary hash column leaked into key_details as {k!r}"
