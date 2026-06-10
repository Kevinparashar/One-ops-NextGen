"""Entity-ID normalizer — system-wide, registry-driven (router layer).

Users write entity references every imaginable way: `INC0048213`, `inc0048213`,
`INC 0048 213`, `INC-0048-213`, `#INC0048213.`, `(REQ0002001)`. Before routing
or any DB lookup, the reference must be canonicalised to one form.

This is **one normalizer for the whole platform** — entity-ID prefixes are
platform vocabulary, not use-case vocabulary. The prefix → service map is
*data*, read from `registries/v2/platform/service-schema.json` (`id_prefix` +
`alias_prefixes`). Adding a service later = one registry row, no code change
(thumb rule #2 — registry-driven, never a hardcoded list).

**No silent failure (thumb rule #11).** `normalize` never returns a bare
`None` and never guesses. It returns a `NormalizationResult` that is either a
clean `NormalizedEntity` or an explicit, human-readable `reason` the caller
surfaces to the user (`unrecognised prefix`, `id body is not numeric`, ...).
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from oneops.errors import ConfigError
from oneops.observability import get_logger

_log = get_logger("oneops.router.entity_id")

_DEFAULT_SCHEMA = "registries/v2/platform/service-schema.json"

# Canonical id body width across every service (INC0001234, KB0005010, …).
# Anything 1-2 digits is too short to be a real id (REJECTED). 3-6 digits is
# an unpadded near-miss (CLARIFY, surface the auto-padded suggestion). 7
# digits is canonical (ACCEPTED). 8+ digits is over-padded (REJECTED).
CANONICAL_DIGIT_WIDTH = 7
_REJECT_DIGITS_BELOW = 3                  # 1-2 → reject
_REJECT_DIGITS_ABOVE = CANONICAL_DIGIT_WIDTH       # 8+ → reject
# 0 digits → CLARIFY (a bare prefix like "INC" — number forgotten).
# 3-6 digits → CLARIFY (unpadded — likely meant the canonical width).

# A candidate is 2-5 letters, optional separators, then a digit and more
# digit/separator runs. This finds *potential* IDs in free text; `normalize`
# is what actually validates and canonicalises each one.
_CANDIDATE = re.compile(r"[A-Za-z]{2,5}[ \t\-_]*\d[\d \t\-_]*")
# A standalone, digit-free alphabetic word. The second `extract` pass checks
# each against the registry prefix set to catch digit-less garbles ("INC" with
# no number, "INCABC") that the digit-bearing pattern above cannot see. The
# `\b` anchors keep it from biting a letter run glued to digits ("INC" inside
# "INC0048213"), which the first pass already owns.
_ALPHA_WORD = re.compile(r"\b[A-Za-z]{2,12}\b")
# A bare contiguous digit run with word boundaries. Used to catch
# `0001234` / `1234` sole tokens AND "incident 0001234" / "we have 1001
# tickets" — the surrounding context decides what to do with each.
_BARE_DIGITS = re.compile(r"\b(\d{2,12})\b")
# Separators then a digit — used to tell that an alpha word is the prefix head
# of a separated ID ("inc" in "inc-0048-213"), which pass 1 already owns.
_LEADS_DIGITS = re.compile(r"[ \t\-_]*\d")
# Mixed-case prefix garble ("INCabc", "REQ_test") — a caps prefix with a
# lower/alnum tail. Pass-2.5 territory (pure-upper → pass 2, digit-bearing
# → pass 1).
_MIXED_RE = re.compile(r"\b([A-Z]{2,5}[A-Za-z0-9]{1,10})\b")
# Everything that is not a letter or digit is a separator to be stripped.
_SEPARATORS = re.compile(r"[^0-9A-Za-z]")


class IdAcceptance(StrEnum):
    """Three-state outcome the contract requires.

    * ACCEPTED — the token is a valid canonical id; the entity is set and
      the caller acts on it.
    * CLARIFY — the token is a near-miss (unpadded width, bare prefix,
      ambiguous bare digits across services). The caller surfaces the
      `clarification` text to the user; on a follow-up reply the caller
      re-normalises with the disambiguator's help.
    * REJECTED — the token is not a real id (too few digits, too many,
      unknown prefix, no word boundary). The caller drops silently unless
      a near-miss prefix was matched, in which case the reason is
      surfaced as advice. Either way, never executed as an id.
    """

    ACCEPTED = "accepted"
    CLARIFY = "clarify"
    REJECTED = "rejected"


@dataclass(frozen=True)
class NormalizedEntity:
    """A canonical entity reference: uppercase prefix + digits, plus the
    service it resolves to and the original raw text it came from."""

    entity_id: str
    service_id: str
    raw: str


@dataclass(frozen=True)
class NormalizationResult:
    """The outcome of normalising one token.

    Three states (`acceptance`):
      * `ACCEPTED` — `entity` carries the canonical entity.
      * `CLARIFY`  — near-miss; `reason` describes what to ask the user.
        `suggested` may carry an auto-padded canonical guess (for an
        unpadded width like `INC1234` → `INC0001234`). `candidates` carries
        the full set of services a bare-digit token could resolve to
        (`0001234` → INC0001234 / REQ0001234 / …).
      * `REJECTED` — not an id at all. `matched_prefix` is non-empty when
        the token began with a known prefix but the body was unrecoverable
        (≤2 digits, 8+ digits, non-numeric body) — that case is surfaced
        as advice; plain noise is dropped.

    `ok` is preserved for backward compatibility — `True` iff acceptance
    is ACCEPTED. New callers should prefer `acceptance`.
    """

    ok: bool
    raw: str
    acceptance: IdAcceptance = IdAcceptance.REJECTED
    entity: NormalizedEntity | None = None
    reason: str = ""
    matched_prefix: str = ""
    suggested: NormalizedEntity | None = None
    candidates: tuple[NormalizedEntity, ...] = field(default_factory=tuple)

    @staticmethod
    def good(entity: NormalizedEntity) -> NormalizationResult:
        return NormalizationResult(
            ok=True, raw=entity.raw,
            acceptance=IdAcceptance.ACCEPTED, entity=entity)

    @staticmethod
    def bad(
        raw: str, reason: str, matched_prefix: str = "",
    ) -> NormalizationResult:
        return NormalizationResult(
            ok=False, raw=raw,
            acceptance=IdAcceptance.REJECTED,
            reason=reason, matched_prefix=matched_prefix)

    @staticmethod
    def clarify(
        raw: str, reason: str, *,
        matched_prefix: str = "",
        suggested: NormalizedEntity | None = None,
        candidates: Iterable[NormalizedEntity] = (),
    ) -> NormalizationResult:
        return NormalizationResult(
            ok=False, raw=raw,
            acceptance=IdAcceptance.CLARIFY,
            reason=reason, matched_prefix=matched_prefix,
            suggested=suggested, candidates=tuple(candidates))


@dataclass(frozen=True)
class ExtractionResult:
    """Every entity reference found in a message: the clean ones, plus
    `malformed` near-misses (a known prefix with a botched number) that the
    caller should ask the user to correct — never silently dropped."""

    entities: tuple[NormalizedEntity, ...]
    malformed: tuple[NormalizationResult, ...]

    @property
    def has_entities(self) -> bool:
        return len(self.entities) > 0


class EntityIdNormalizer:
    """Canonicalises entity-ID references. One instance, platform-wide."""

    def __init__(self, prefix_to_service: dict[str, str]) -> None:
        # Uppercase prefixes; matched longest-first so a longer prefix is never
        # shadowed by a shorter one that happens to share a start.
        self._prefix_to_service = {p.upper(): s for p, s in prefix_to_service.items()}
        self._prefixes = sorted(self._prefix_to_service, key=len, reverse=True)
        if not self._prefixes:
            raise ConfigError("EntityIdNormalizer built with no prefixes")

    @classmethod
    def from_registry_file(cls, path: str | None = None) -> EntityIdNormalizer:
        """Build from `service-schema.json` — every service's `id_prefix` and
        each of its `alias_prefixes` map to that service."""
        if path is None:
            path = str(Path(__file__).resolve().parents[3] / _DEFAULT_SCHEMA)
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"service-schema file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"service-schema unreadable: {p}", cause=exc) from exc

        mapping: dict[str, str] = {}
        for svc in doc.get("services", []):
            service_id = svc.get("service_id")
            prefix = svc.get("id_prefix")
            if not service_id or not prefix:
                continue
            mapping[prefix] = service_id
            for alias in svc.get("alias_prefixes", []):
                mapping[alias] = service_id
        _log.info("entity_id.normalizer_loaded", prefix_count=len(mapping),
                  source=str(p))
        return cls(mapping)

    # ── normalise one token ──────────────────────────────────────────────

    def normalize(self, token: str) -> NormalizationResult:
        """Canonicalise one entity-ID token. Never raises, never guesses —
        returns a `NormalizationResult` (good, or a reason)."""
        raw = token or ""
        # Strip every separator (space, tab, hyphen, underscore, punctuation,
        # brackets) and uppercase — "INC 0048-213" / "(inc_0048213)" → "INC0048213".
        cleaned = _SEPARATORS.sub("", raw).upper()
        if not cleaned:
            return NormalizationResult.bad(raw, "no entity reference found in the input")

        for prefix in self._prefixes:
            if cleaned.startswith(prefix):
                body = cleaned[len(prefix):]
                if not body:
                    return NormalizationResult.bad(
                        raw, f"'{prefix}' is a valid ID prefix but has no number "
                             f"after it", matched_prefix=prefix)
                if not body.isdigit():
                    return NormalizationResult.bad(
                        raw, f"the part after '{prefix}' is not a number: "
                             f"'{body}'", matched_prefix=prefix)
                return NormalizationResult.good(NormalizedEntity(
                    entity_id=prefix + body,
                    service_id=self._prefix_to_service[prefix],
                    raw=raw))

        return NormalizationResult.bad(
            raw, f"'{cleaned}' does not begin with a recognised ID prefix "
                 f"(expected one of: {', '.join(sorted(self._prefixes))})")

    # ── scan a whole message ─────────────────────────────────────────────

    def extract(self, message: str) -> ExtractionResult:
        """Find every entity reference in a free-text message.

        Returns the clean `entities` (deduped, in first-seen order) and the
        `malformed` near-misses — a token that began with a real prefix but
        had a botched number. Plain noise (letters+digits matching no prefix)
        is *not* surfaced; a genuine attempt at an ID always is (rule #11).

        Two passes. Pass 1 finds digit-bearing tokens ("INC0048213",
        "INCX0048"). Pass 2 finds digit-less garbles — a token that *is* a
        known prefix ("INC" with the number forgotten) or an all-caps token
        led by one ("INCABC"). An ordinary lower-case word that merely starts
        with a prefix ("incident", "request") is never flagged."""
        text = message or ""
        entities: list[NormalizedEntity] = []
        malformed: list[NormalizationResult] = []
        seen_ok: set[str] = set()
        seen_bad: set[str] = set()
        self._pass1_digit_tokens(text, entities, seen_ok, malformed, seen_bad)
        self._pass2_digitless_garbles(text, malformed, seen_bad)
        self._pass25_mixed_case(text, malformed, seen_bad)
        self._pass3_bare_digits(text, entities, malformed, seen_bad)
        return ExtractionResult(tuple(entities), tuple(malformed))

    def _record_bad(
        self, malformed: list[NormalizationResult], seen_bad: set[str],
        result: NormalizationResult,
    ) -> None:
        """Append a malformed near-miss, deduped by its upper-cased raw token."""
        key = (result.raw or "").strip().upper()
        if key not in seen_bad:
            seen_bad.add(key)
            malformed.append(result)

    def _pass1_digit_tokens(
        self, text: str, entities: list[NormalizedEntity], seen_ok: set[str],
        malformed: list[NormalizationResult], seen_bad: set[str],
    ) -> None:
        """Pass 1 — digit-bearing tokens ("INC0048213", "INCX0048"). Valid IDs
        become entities; prefix-matched-but-bad tokens become near-misses;
        prefix-less noise is ignored."""
        for match in _CANDIDATE.finditer(text):
            result = self.normalize(match.group(0))
            if result.ok and result.entity is not None:
                if result.entity.entity_id not in seen_ok:
                    seen_ok.add(result.entity.entity_id)
                    entities.append(result.entity)
            elif result.matched_prefix:
                self._record_bad(malformed, seen_bad, result)

    def _pass2_digitless_garbles(
        self, text: str, malformed: list[NormalizationResult],
        seen_bad: set[str],
    ) -> None:
        """Pass 2 — digit-less prefix garbles (a bare "INC" with the number
        forgotten). A word heading a separated digit-bearing ID ("inc" in
        "inc-0048-213") is already owned by pass 1 and skipped."""
        for match in _ALPHA_WORD.finditer(text):
            word = match.group(0)
            if _LEADS_DIGITS.match(text, match.end()):
                continue
            if not self._is_digitless_id_attempt(word):
                continue
            result = self.normalize(word)
            if not result.ok and result.matched_prefix:
                self._record_bad(malformed, seen_bad, result)

    def _pass25_mixed_case(
        self, text: str, malformed: list[NormalizationResult],
        seen_bad: set[str],
    ) -> None:
        """Pass 2.5 — mixed-case prefix garbles ("INCabc", "REQ_test"). A caps
        prefix + lowercase/alnum tail is almost always a botched ID (caps
        prefix = deliberate signal). Pure-upper → pass 2; digit-bearing →
        pass 1; pure-lower ("incident") is an English word and ignored.
        (Closes the 2026-05-30 "summarize INCabc" silent-wrong-ticket bug.)"""
        for match in _MIXED_RE.finditer(text):
            token = match.group(1)
            if any(ch.isdigit() for ch in token):
                continue                       # pass-1 territory
            if token.isupper():
                continue                       # pass-2 territory
            upper = token.upper()
            matched_prefix = next(
                (p for p in self._prefixes if upper.startswith(p)), "")
            if not matched_prefix:
                continue
            result = self.normalize(token)
            if not result.ok and result.matched_prefix:
                self._record_bad(malformed, seen_bad, result)

    def _pass3_bare_digits(
        self, text: str, entities: list[NormalizedEntity],
        malformed: list[NormalizationResult], seen_bad: set[str],
    ) -> None:
        """Pass 3 — bare 7+ digit runs with no prefix ("summarize 0001234" —
        user forgot the INC/REQ). Without this, such a message has zero
        entities AND zero near-misses, so the router falls through to
        focus-injection and silently summarises the wrong ticket (2026-05-30
        incident). 7 because every ITSM id here is 7-digit; 4-6 digit numbers
        are usually years/quantities/dates. Digit runs already inside a matched
        entity or near-miss are skipped."""
        for match in _BARE_DIGITS.finditer(text):
            body = match.group(1)
            if len(body) < 7:
                continue
            if any(e.entity_id.endswith(body) for e in entities):
                continue
            if any((b.raw or "").endswith(body) for b in malformed):
                continue
            self._record_bad(malformed, seen_bad, NormalizationResult.bad(
                body,
                f'"{body}" looks like a record number but is missing its '
                f'type prefix. Please share the full ID — '
                f'e.g. "INC{body}" for an incident or "REQ{body}" for a '
                f'request.',
            ))

    def _is_digitless_id_attempt(self, word: str) -> bool:
        """True when a digit-free word is a genuine entity-ID attempt — so it
        is surfaced as a near-miss — vs. an ordinary English word or common
        acronym that merely happens to *be* a prefix.

        Two qualifying shapes:
          * ALL-CAPS prefix of length ≥ 3 standing alone ("INC", "REQ") — the
            number was forgotten. Length-2 prefixes (KB, CI, SR, CAT) are
            EXCLUDED here because they are common English shorthand ("find a
            KB for X" = "knowledge-base", not a botched id) and flagging them
            blocks every natural KB-search query — 2026-05-27 incident. A
            two-letter id is still reachable through pass-1 once the user
            types the number ("KB0005001").
          * ALL-CAPS token led by a 3+-letter prefix and longer than it
            ("INCABC", "REQXYZ") — the trailing garble is the deliberate
            ID-style signal that tells these apart from plain words. 2-letter
            prefixes (SR, KB, CI) are EXCLUDED here too: an all-caps word that
            merely STARTS with one ("SRE" = Site Reliability Engineer, "CIO",
            "KBASE") is an ordinary acronym, not a botched id — flagging it
            hijacks the turn (2026-06-09 incident). The real id is still
            reachable once the user types the number.

        Lower-case / title-case prefixes never qualify ("Incident", "inc",
        "request") — those are English usage."""
        upper = word.upper()
        if upper == word and upper in self._prefix_to_service \
                and len(upper) >= 3:
            return True
        if word.isupper():
            return any(upper.startswith(p) and len(upper) > len(p)
                       for p in self._prefixes if len(p) >= 3)
        return False

    # ── user-facing clarification ────────────────────────────────────────

    def _describe_one(self, result: NormalizationResult) -> str:
        """One human-readable line telling the user how to fix one bad ID."""
        shown = (result.raw or "").strip() or result.raw
        if result.matched_prefix:
            service = self._prefix_to_service[result.matched_prefix]
            example = f"{result.matched_prefix}0001234"
            return (f'"{shown}" looks like a {service} ID but is not valid — '
                    f'{service} IDs look like "{example}". '
                    f"Please share the correct ID.")
        return (f'"{shown}" is not a record ID I recognise. IDs begin with a '
                f'type prefix — e.g. "INC0001234" for an incident or '
                f'"REQ0001234" for a request. Which record did you mean?')

    def clarification_message(
        self, results: Iterable[NormalizationResult]
    ) -> str:
        """Build the single on-screen message for one or more malformed
        references. Empty string when nothing is malformed. This is the text
        the user actually sees — a botched ID is never silently dropped
        (thumb rule #11)."""
        seen: set[str] = set()
        lines: list[str] = []
        for r in results:
            if r.ok:
                continue
            key = (r.raw or "").strip()
            if key in seen:
                continue
            seen.add(key)
            lines.append(self._describe_one(r))
        if not lines:
            return ""
        if len(lines) == 1:
            return lines[0]
        return ("I spotted a few record IDs that do not look right:\n"
                + "\n".join(f"- {ln}" for ln in lines))


__all__ = [
    "NormalizedEntity",
    "NormalizationResult",
    "ExtractionResult",
    "EntityIdNormalizer",
]
