"""Entity-ID normalizer — exhaustive edge-case coverage.

Every way a user can mangle an entity reference, plus every failure mode —
because the normalizer is what stands between messy human input and a clean
DB lookup, and it must never silently guess or drop (thumb rule #11).
"""
from __future__ import annotations

import pytest

from oneops.errors import ConfigError
from oneops.router.entity_id import EntityIdNormalizer


@pytest.fixture(scope="module")
def norm() -> EntityIdNormalizer:
    return EntityIdNormalizer.from_registry_file()


# ── clean canonical IDs, every service ───────────────────────────────────


@pytest.mark.parametrize("token,entity_id,service", [
    ("INC0048213", "INC0048213", "incident"),
    ("REQ0002001", "REQ0002001", "request"),
    ("PBM0003003", "PBM0003003", "problem"),
    ("CHG0004007", "CHG0004007", "change"),
    ("AST0001005", "AST0001005", "asset"),
    ("CI0000001", "CI0000001", "cmdb_ci"),
    ("KB0012345", "KB0012345", "knowledge"),
    ("CAT0000010", "CAT0000010", "catalog"),
    ("ONB0000002", "ONB0000002", "onboarding"),
])
def test_clean_ids_normalize_for_every_service(norm, token, entity_id, service):
    r = norm.normalize(token)
    assert r.ok
    assert r.entity.entity_id == entity_id
    assert r.entity.service_id == service


# ── alias prefixes resolve to the right service ──────────────────────────


@pytest.mark.parametrize("token,service", [
    ("SR0002001", "request"),       # SR → request
    ("PRB0003003", "problem"),      # PRB → problem
    ("RFC0004007", "change"),       # RFC → change
    ("CMDB0000001", "cmdb_ci"),     # CMDB → cmdb_ci (the canonical service id)
])
def test_alias_prefixes_resolve(norm, token, service):
    r = norm.normalize(token)
    assert r.ok and r.entity.service_id == service


# ── case insensitivity ───────────────────────────────────────────────────


@pytest.mark.parametrize("token", ["inc0048213", "Inc0048213", "iNc0048213"])
def test_case_is_normalized(norm, token):
    r = norm.normalize(token)
    assert r.ok and r.entity.entity_id == "INC0048213"


# ── internal separators — spaces, hyphens, underscores, mixed ────────────


@pytest.mark.parametrize("token", [
    "INC 0048 213", "INC-0048-213", "INC_0048_213", "INC-0048 213",
    "INC  0048   213", "inc - 0048 _ 213", "INC\t0048\t213",
])
def test_internal_separators_are_stripped(norm, token):
    r = norm.normalize(token)
    assert r.ok, f"{token!r} should normalize"
    assert r.entity.entity_id == "INC0048213"


# ── leading / trailing punctuation ───────────────────────────────────────


@pytest.mark.parametrize("token", [
    "INC0048213.", "INC0048213?", "INC0048213!", "INC0048213,",
    "#INC0048213", "(INC0048213)", "'INC0048213'", "\"INC0048213\"",
    "  INC0048213  ", "[INC0048213]", "INC0048213;",
])
def test_surrounding_punctuation_is_stripped(norm, token):
    r = norm.normalize(token)
    assert r.ok and r.entity.entity_id == "INC0048213"


# ── longest-prefix wins (CMDB vs CI) ─────────────────────────────────────


def test_longest_prefix_wins(norm):
    # CMDB (4 chars) and CI (2 chars) both map to cmdb — CMDB0001 must not be
    # mis-read as CI + "MDB0001".
    assert norm.normalize("CMDB0000001").entity.entity_id == "CMDB0000001"
    assert norm.normalize("CI0000001").entity.entity_id == "CI0000001"


# ── digits preserved exactly — no invented zeros ─────────────────────────


def test_digits_are_preserved_exactly(norm):
    # "INC 48 213" is a DIFFERENT id from "INC0048213" — the normalizer must
    # not pad or invent leading zeros.
    assert norm.normalize("INC 48 213").entity.entity_id == "INC48213"
    assert norm.normalize("INC0048213").entity.entity_id == "INC0048213"


# ── failures — explicit reason, never a silent drop (rule #11) ───────────


def test_empty_input_fails_with_a_reason(norm):
    for token in ["", "   ", "\t", "()", "#", "---"]:
        r = norm.normalize(token)
        assert not r.ok
        assert r.reason                          # explicit, human-readable
        assert r.entity is None


def test_bare_digits_have_no_prefix(norm):
    # "12345" — no prefix → cannot guess which service. Must fail, not guess.
    r = norm.normalize("12345")
    assert not r.ok
    assert "recognised ID prefix" in r.reason
    assert r.matched_prefix == ""                # not a near-miss — plain noise


def test_unknown_prefix_fails(norm):
    r = norm.normalize("XYZ0001")
    assert not r.ok and r.matched_prefix == ""


def test_prefix_with_no_number_is_a_near_miss(norm):
    # "INC" alone — a real prefix, no number. A near-miss the caller surfaces.
    r = norm.normalize("INC")
    assert not r.ok
    assert r.matched_prefix == "INC"
    assert "no number" in r.reason


def test_prefix_with_non_numeric_body_is_a_near_miss(norm):
    r = norm.normalize("INCABC")
    assert not r.ok
    assert r.matched_prefix == "INC"
    assert "not a number" in r.reason


def test_id_glued_to_text_fails_cleanly(norm):
    # "INC0048foo" — prefix INC, body "0048FOO" is not all digits → near-miss.
    r = norm.normalize("INC0048foo")
    assert not r.ok and r.matched_prefix == "INC"


# ── extract — scan a free-text message ───────────────────────────────────


def test_extract_finds_a_single_id(norm):
    out = norm.extract("can you summarize INC0048213 for me")
    assert [e.entity_id for e in out.entities] == ["INC0048213"]


def test_extract_finds_multiple_ids(norm):
    out = norm.extract("summarize INC0048213 and CHG0004007 please")
    assert {e.entity_id for e in out.entities} == {"INC0048213", "CHG0004007"}


def test_extract_handles_messy_ids_in_a_sentence(norm):
    out = norm.extract("what's the status of inc-0048-213 and req 0002 001?")
    assert {e.entity_id for e in out.entities} == {"INC0048213", "REQ0002001"}


def test_extract_dedups_repeated_ids(norm):
    out = norm.extract("INC0048213 ... again INC0048213")
    assert [e.entity_id for e in out.entities] == ["INC0048213"]


def test_extract_surfaces_a_malformed_near_miss(norm):
    # A real prefix with a stray letter in the body ("INC" + "X0048") — a
    # genuine attempt at an ID, surfaced as a near-miss, not silently dropped.
    out = norm.extract("summarize INCX0048 for me")
    assert not out.entities
    assert len(out.malformed) == 1
    assert out.malformed[0].matched_prefix == "INC"


def test_extract_ignores_plain_noise(norm):
    # Words/numbers that match no prefix are not surfaced as malformed.
    out = norm.extract("section 3 has about 200 items, see page 12")
    assert not out.entities
    assert not out.malformed


def test_extract_does_not_flag_a_plain_word(norm):
    # "catalog" starts with the CAT prefix but has no digits — it is a word,
    # not an ID attempt, and must not be flagged.
    out = norm.extract("open the catalog and the knowledge page")
    assert not out.entities and not out.malformed


def test_extract_on_an_empty_message(norm):
    out = norm.extract("")
    assert not out.entities and not out.malformed
    assert out.has_entities is False


# ── extract pass 2 — digit-less prefix garbles ───────────────────────────


@pytest.mark.parametrize("message", [
    "can you summarize INC for me",      # bare prefix, number forgotten
    "summarize inc please",              # lower-case bare prefix
    "what about REQ then",               # a different bare prefix
])
def test_extract_catches_a_bare_prefix_with_no_number(norm, message):
    out = norm.extract(message)
    assert not out.entities
    assert len(out.malformed) == 1
    assert out.malformed[0].matched_prefix in ("INC", "REQ")


def test_extract_catches_an_all_caps_digitless_garble(norm):
    # "INCABC" — upper-cased, prefix-led, no digit: a genuine ID attempt.
    out = norm.extract("please summarize INCABC")
    assert not out.entities
    assert len(out.malformed) == 1
    assert out.malformed[0].matched_prefix == "INC"


def test_extract_does_not_flag_ordinary_words_that_start_with_a_prefix(norm):
    # "incident" / "request" start with INC / REQ but are plain lower-case
    # words — they must never be mistaken for an ID (the whole reason pass 2
    # is gated on exact-prefix or all-caps, not a relaxed regex).
    out = norm.extract("please summarize this incident and the open request")
    assert not out.entities and not out.malformed


def test_extract_bare_prefix_rides_alongside_a_valid_id(norm):
    out = norm.extract("summarize INC0048213 and also CHG")
    assert [e.entity_id for e in out.entities] == ["INC0048213"]
    assert len(out.malformed) == 1
    assert out.malformed[0].matched_prefix == "CHG"


def test_extract_does_not_double_flag_a_separated_id_prefix(norm):
    # "inc" in "inc-0048-213" is the head of a valid ID — pass 1 owns it,
    # pass 2 must not also surface it as a bare-prefix near-miss.
    out = norm.extract("status of inc-0048-213 please")
    assert [e.entity_id for e in out.entities] == ["INC0048213"]
    assert not out.malformed


def test_extract_dedups_a_repeated_bare_prefix(norm):
    out = norm.extract("INC ... and INC again")
    assert len(out.malformed) == 1


# ── registry-driven construction ─────────────────────────────────────────


def test_from_registry_file_loads_all_service_prefixes(norm):
    # 9 services; request/problem/change/cmdb each add one alias → 13 prefixes.
    assert norm.normalize("INC0001").ok
    assert norm.normalize("ONB0001").ok          # last service in the schema


def test_missing_schema_file_raises(norm):
    with pytest.raises(ConfigError, match="not found"):
        EntityIdNormalizer.from_registry_file("/nonexistent/service-schema.json")


def test_normalizer_with_no_prefixes_is_rejected():
    with pytest.raises(ConfigError, match="no prefixes"):
        EntityIdNormalizer({})


# ── clarification_message — the on-screen text for a bad ID (rule #11) ───


def test_clarification_message_names_service_and_gives_an_example(norm):
    msg = norm.clarification_message([norm.normalize("INCX0048")])
    assert "INCX0048" in msg          # the user's text, so they recognise it
    assert "incident" in msg          # the service it looked like
    assert "INC0001234" in msg        # a valid example to copy


def test_clarification_message_combines_multiple_bad_ids(norm):
    msg = norm.clarification_message(
        [norm.normalize("INCX0048"), norm.normalize("REQQ1")])
    assert msg.startswith("I spotted")
    assert "INCX0048" in msg and "REQQ1" in msg


def test_clarification_message_for_an_unrecognised_id(norm):
    msg = norm.clarification_message([norm.normalize("12345")])
    assert "12345" in msg and "INC0001234" in msg


def test_clarification_message_dedups_a_repeated_bad_id(norm):
    r = norm.normalize("INCX0048")
    assert norm.clarification_message([r, r]).count("INCX0048") == 1


def test_clarification_message_is_empty_when_nothing_is_malformed(norm):
    assert norm.clarification_message([]) == ""
    assert norm.clarification_message([norm.normalize("INC0048213")]) == ""
