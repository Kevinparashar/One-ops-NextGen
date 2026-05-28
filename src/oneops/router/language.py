"""Language detection — substrate gap G4.

Detects the locale of a user message so the rest of the pipeline can respond
in the user's language (BEHAVIOR_CORPUS §C13: "same-language reply").

Design constraint — **no phrase catalog, no stop-word list** ([[feedback_poc5mw_thumb_rules]]):
language hints come from **Unicode script analysis**, a structural property
of the text, not a per-language word list that requires maintenance.

A script (Latin, Cyrillic, CJK, Devanagari, Arabic, Hebrew, Greek, Thai)
narrows the locale to a class. The detector returns the most-common ISO
locale for the dominant script, OR `None` when the script alone is
insufficient (e.g. Latin → cannot distinguish en / fr / es without a
phrase-aware library). The router falls back to the tenant's configured
locale on `None`.

For production fan-out across many languages, plug a real library
(`lingua-py`, `langdetect`, `fastText`) by implementing `LanguageDetector`
and registering it via `set_language_detector` — same shape as every other
substrate component.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from oneops.observability import get_logger

_log = get_logger("oneops.router.language")


@dataclass(frozen=True)
class DetectionResult:
    """What the detector observed.

    `locale` is a BCP-47-ish tag or `None` when undetermined. `script` is the
    dominant Unicode script the heuristic saw; useful for observability and
    for the router to decide whether to skip glossary normalization that
    only makes sense for one script."""

    locale: str | None
    script: str
    confidence: float                       # 0.0..1.0; > 0 only if any data


@runtime_checkable
class LanguageDetector(Protocol):
    """Detect the locale of a user message."""

    def detect(self, text: str) -> DetectionResult: ...


# ── Script-based default detector ────────────────────────────────────────


# Script-name (as returned by `unicodedata`) → canonical locale tag. The
# table is data, not a catalogue of phrases — it maps Unicode scripts to
# the most-common BCP-47 default for that script.
# Latin is intentionally absent: it covers ~60 languages, so the detector
# returns None for pure-Latin text and the router falls back to tenant locale.
_SCRIPT_TO_LOCALE: dict[str, str] = {
    "Cyrillic": "ru",
    "Hiragana": "ja",
    "Katakana": "ja",
    "Han": "zh",                            # CJK ideographs — could be zh / ja / ko
    "Cjk": "zh",                            # `unicodedata` reports CJK ideographs as "CJK UNIFIED IDEOGRAPH-…"
    "Hangul": "ko",
    "Devanagari": "hi",
    "Arabic": "ar",
    "Hebrew": "he",
    "Greek": "el",
    "Thai": "th",
    "Bengali": "bn",
    "Tamil": "ta",
    "Telugu": "te",
    "Gujarati": "gu",
    "Kannada": "kn",
    "Malayalam": "ml",
    "Gurmukhi": "pa",
    "Oriya": "or",
    "Sinhala": "si",
    "Myanmar": "my",
    "Khmer": "km",
    "Lao": "lo",
    "Georgian": "ka",
    "Armenian": "hy",
    "Ethiopic": "am",
}


def _script_name(ch: str) -> str:
    """Unicode script name for one character — uses the closest information
    `unicodedata` exposes (the character's name prefix). Returns an empty
    string for unclassified characters."""
    if not ch:
        return ""
    try:
        # The Unicode name starts with the script, e.g. "HIRAGANA LETTER A".
        # This is the public-stdlib alternative to the `unicodedataplus`
        # library and is sufficient for our coarse-grained detection.
        name = unicodedata.name(ch, "")
    except (TypeError, ValueError):
        return ""
    if not name:
        return ""
    # Take the first word and normalize capitalization. Filter out class
    # prefixes that aren't scripts (DIGIT, MATHEMATICAL, etc.).
    first = name.split(" ", 1)[0].capitalize()
    if first in {"Digit", "Mathematical", "Combining", "Modifier", "Spacing",
                 "Variation", "Zero", "Tag"}:
        return ""
    return first


class UnicodeScriptDetector:
    """The deterministic in-process default — no I/O, no model, no phrase
    list. Suitable for a cold-start FaaS environment.

    Scoring: among the alphabetic characters in `text`, the script with the
    most characters wins; confidence is its share of total alphabetic
    characters. Empty input / no alphabetic characters returns
    `(locale=None, script="", confidence=0.0)`."""

    def detect(self, text: str) -> DetectionResult:
        if not text:
            return DetectionResult(locale=None, script="", confidence=0.0)

        counts: dict[str, int] = {}
        total = 0
        for ch in text:
            if not ch.isalpha():
                continue
            script = _script_name(ch)
            if not script:
                continue
            counts[script] = counts.get(script, 0) + 1
            total += 1

        if total == 0 or not counts:
            return DetectionResult(locale=None, script="", confidence=0.0)

        dominant = max(counts, key=lambda k: counts[k])
        confidence = counts[dominant] / total
        # Latin → None (locale ambiguous without phrase analysis).
        if dominant == "Latin":
            return DetectionResult(
                locale=None, script="Latin", confidence=confidence)
        locale = _SCRIPT_TO_LOCALE.get(dominant)
        return DetectionResult(
            locale=locale, script=dominant, confidence=confidence)


class NullLanguageDetector:
    """Disabled detector — always returns `None`. Selected by
    `ONEOPS_LANG_DETECT=off`. The router falls back to `tenant.locale`."""

    def detect(self, text: str) -> DetectionResult:
        return DetectionResult(locale=None, script="", confidence=0.0)


_detector: LanguageDetector | None = None


def _build_default() -> LanguageDetector:
    backend = os.getenv("ONEOPS_LANG_DETECT", "script").strip().lower()
    if backend == "off":
        _log.info("language.detector_selected", backend="off")
        return NullLanguageDetector()
    _log.info("language.detector_selected", backend="script")
    return UnicodeScriptDetector()


def get_language_detector() -> LanguageDetector:
    """The process-wide detector. Lazy; cold-start-safe."""
    global _detector
    if _detector is None:
        _detector = _build_default()
    return _detector


def set_language_detector(detector: LanguageDetector) -> None:
    """Replace the process-wide detector — for tests and FaaS wiring."""
    global _detector
    _detector = detector


def resolve_locale(
    text: str, *, fallback_locale: str,
) -> tuple[str, DetectionResult]:
    """Detect → fall back to `fallback_locale` when undetermined.

    Returns `(effective_locale, detection)`. The detection record is
    threaded into observability so an operator can see what the detector
    saw, even when the effective locale ended up being the fallback."""
    detection = get_language_detector().detect(text)
    if detection.locale:
        return detection.locale, detection
    return fallback_locale, detection


__all__ = [
    "DetectionResult",
    "LanguageDetector",
    "UnicodeScriptDetector",
    "NullLanguageDetector",
    "get_language_detector",
    "set_language_detector",
    "resolve_locale",
]
