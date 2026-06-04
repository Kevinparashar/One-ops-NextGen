"""Unit tests for the canonical embedding-input builder.

Covers: missing-field defensiveness, cross-table isolation, loud failure on
unknown service_id, empty-text rejection, short-text warning.
"""
from __future__ import annotations

import pytest

from oneops.embeddings.triage_input import (
    build_canonical_anchor,
    build_embedding_input,
    enrich_incident,
    enrich_request,
    validate_embed_text,
)


class TestCanonicalAnchor:
    def test_all_three_fields_present(self) -> None:
        row = {"title": "T", "description": "D", "category": "C"}
        assert build_canonical_anchor(row) == [
            "Title: T",
            "Description: D",
            "Category: C",
        ]

    def test_missing_description_is_omitted(self) -> None:
        row = {"title": "T", "category": "C"}
        assert build_canonical_anchor(row) == ["Title: T", "Category: C"]

    def test_empty_string_is_omitted(self) -> None:
        row = {"title": "T", "description": "", "category": "C"}
        assert build_canonical_anchor(row) == ["Title: T", "Category: C"]

    def test_all_missing_returns_empty(self) -> None:
        assert build_canonical_anchor({}) == []


class TestIncidentEnrichment:
    def test_full_enrichment(self) -> None:
        row = {
            "service_name": "Corporate VPN",
            "subcategory": "vpn",
            "ci_name": "VPN Gateway - APAC",
            "ci_type": "network",
            "ci_location": "Mumbai-DC",
        }
        assert enrich_incident(row) == [
            "Service: Corporate VPN",
            "Subcategory: vpn",
            "Primary CI: VPN Gateway - APAC",
            "CI Type: network",
            "CI Location: Mumbai-DC",
        ]

    def test_no_ci_join_result(self) -> None:
        row = {"service_name": "Email", "subcategory": "outlook"}
        assert enrich_incident(row) == ["Service: Email", "Subcategory: outlook"]

    def test_request_fields_ignored(self) -> None:
        row = {"catalog_name": "Laptop", "catalog_category": "hardware"}
        assert enrich_incident(row) == []


class TestRequestEnrichment:
    def test_full_enrichment(self) -> None:
        row = {
            "catalog_name": "Standard developer laptop",
            "catalog_category": "hardware",
            "ci_name": "Workstation pool",
        }
        assert enrich_request(row) == [
            "Catalog Item: Standard developer laptop",
            "Catalog Category: hardware",
            "Primary CI: Workstation pool",
        ]

    def test_no_catalog_no_ci(self) -> None:
        assert enrich_request({}) == []

    def test_incident_fields_ignored(self) -> None:
        row = {"service_name": "Email", "subcategory": "outlook", "ci_type": "server"}
        assert enrich_request(row) == []


class TestComposedInput:
    def test_incident_e2e(self) -> None:
        row = {
            "title": "VPN disconnects on Wi-Fi handoff",
            "description": "Session drops between SSIDs",
            "category": "network",
            "service_name": "Corporate VPN",
            "subcategory": "vpn",
            "ci_name": "VPN Gateway - APAC",
            "ci_type": "network",
            "ci_location": "Mumbai-DC",
        }
        text = build_embedding_input(row, "incident")
        assert text == (
            "Title: VPN disconnects on Wi-Fi handoff\n"
            "Description: Session drops between SSIDs\n"
            "Category: network\n"
            "Service: Corporate VPN\n"
            "Subcategory: vpn\n"
            "Primary CI: VPN Gateway - APAC\n"
            "CI Type: network\n"
            "CI Location: Mumbai-DC"
        )

    def test_request_e2e(self) -> None:
        row = {
            "title": "New laptop for finance analyst",
            "description": "Standard laptop for new joiner",
            "category": "hardware",
            "catalog_name": "Standard developer laptop",
            "catalog_category": "hardware",
        }
        text = build_embedding_input(row, "request")
        assert text == (
            "Title: New laptop for finance analyst\n"
            "Description: Standard laptop for new joiner\n"
            "Category: hardware\n"
            "Catalog Item: Standard developer laptop\n"
            "Catalog Category: hardware"
        )

    def test_unknown_service_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported service_id"):
            build_embedding_input({"title": "x"}, "problem")

    def test_request_with_incident_only_fields_drops_them(self) -> None:
        row = {
            "title": "T",
            "description": "D",
            "service_name": "VPN",  # incident-only — must NOT appear
            "subcategory": "vpn",  # incident-only — must NOT appear
        }
        text = build_embedding_input(row, "request")
        assert "Service:" not in text
        assert "Subcategory:" not in text
        assert "Title: T" in text


class TestValidate:
    def test_empty_raises(self) -> None:
        with pytest.raises(RuntimeError, match="empty embedding text"):
            validate_embed_text("", "INC0001", "incident")

    def test_whitespace_only_raises(self) -> None:
        with pytest.raises(RuntimeError, match="empty embedding text"):
            validate_embed_text("   \n  ", "INC0001", "incident")

    def test_short_warns(self) -> None:
        warnings = validate_embed_text("Title: x", "INC0001", "incident")
        assert len(warnings) == 1
        assert "short" in warnings[0]

    def test_normal_no_warning(self) -> None:
        text = "Title: VPN disconnects\nDescription: keeps dropping every hour"
        assert validate_embed_text(text, "INC0001", "incident") == []
