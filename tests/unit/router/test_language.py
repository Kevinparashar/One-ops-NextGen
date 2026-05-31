"""Language detection — substrate gap G4.

Verifies the script-based detector returns the expected locales for each
script class, the Latin / undetermined / empty edge cases collapse to the
fallback, and the public Protocol surface is stable.
"""
from __future__ import annotations

import pytest

from oneops.router.language import (
    DetectionResult,
    LanguageDetector,
    NullLanguageDetector,
    UnicodeScriptDetector,
    get_language_detector,
    resolve_locale,
    set_language_detector,
)


@pytest.fixture(autouse=True)
def _reset_detector():
    set_language_detector(UnicodeScriptDetector())
    yield
    set_language_detector(UnicodeScriptDetector())


# ── per-script ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected_locale", "expected_script"), [
    ("Привет, мир!", "ru", "Cyrillic"),
    ("こんにちは世界", "ja", "Hiragana"),                # mixed hiragana+han, hiragana wins
    ("안녕하세요 세계", "ko", "Hangul"),
    ("नमस्ते दुनिया", "hi", "Devanagari"),
    ("مرحبا بالعالم", "ar", "Arabic"),
    ("שלום עולם", "he", "Hebrew"),
    ("γειά σου κόσμε", "el", "Greek"),
    ("สวัสดีชาวโลก", "th", "Thai"),
    ("வணக்கம் உலகம்", "ta", "Tamil"),
])
def test_detector_returns_script_locale(text, expected_locale, expected_script):
    out = UnicodeScriptDetector().detect(text)
    assert out.locale == expected_locale
    assert out.script == expected_script
    assert 0.0 < out.confidence <= 1.0


# ── Latin → None (ambiguous without phrase analysis) ─────────────────────


def test_pure_latin_text_yields_no_locale():
    out = UnicodeScriptDetector().detect("Hello world, how are you?")
    assert out.locale is None
    assert out.script == "Latin"
    # Confidence is still computed (it is real signal — "we saw Latin"), but
    # the locale stays None so the router falls back to tenant.locale.
    assert out.confidence > 0.0


def test_french_latin_text_also_yields_no_locale():
    # Confirms Latin handling is not English-biased — it's a structural rule.
    out = UnicodeScriptDetector().detect("Bonjour le monde, comment ça va ?")
    assert out.locale is None
    assert out.script == "Latin"


# ── empty / no-alpha → None ──────────────────────────────────────────────


def test_empty_text_yields_no_signal():
    out = UnicodeScriptDetector().detect("")
    assert out.locale is None
    assert out.script == ""
    assert out.confidence == 0.0


def test_punctuation_and_digits_only_yields_no_signal():
    out = UnicodeScriptDetector().detect("12345 ?!.,()  ")
    assert out.locale is None
    assert out.script == ""
    assert out.confidence == 0.0


# ── mixed scripts → dominant wins ────────────────────────────────────────


def test_mixed_scripts_dominant_one_wins():
    # Heavily Cyrillic + one Latin word — Cyrillic must dominate.
    text = "Hello Привет всему миру и здравствуйте"
    out = UnicodeScriptDetector().detect(text)
    assert out.script == "Cyrillic"
    assert out.locale == "ru"


def test_mixed_latin_and_cjk_resolves_to_cjk_when_dominant():
    text = "OK 今日は世界の皆さん"
    out = UnicodeScriptDetector().detect(text)
    # CJK ideographs ("Cjk"/"Han") or Hiragana — either way, NOT Latin.
    assert out.script in {"Han", "Cjk", "Hiragana"}
    assert out.locale in {"ja", "zh"}


# ── resolve_locale — detected ⊕ fallback ─────────────────────────────────


def test_resolve_locale_returns_detected_when_available():
    locale, detection = resolve_locale("Привет", fallback_locale="en")
    assert locale == "ru"
    assert detection.locale == "ru"


def test_resolve_locale_falls_back_when_detector_is_undecided():
    locale, detection = resolve_locale(
        "Hello there", fallback_locale="fr")
    assert locale == "fr"
    assert detection.locale is None
    # Detector still records what it saw — the operator can debug.
    assert detection.script == "Latin"


def test_resolve_locale_falls_back_on_empty_text():
    locale, detection = resolve_locale("", fallback_locale="en-GB")
    assert locale == "en-GB"
    assert detection.locale is None


# ── Null detector — disable path is structural, not a special case ───────


def test_null_detector_always_returns_none():
    out = NullLanguageDetector().detect("Привет мир")
    assert out.locale is None
    assert out.script == ""
    assert out.confidence == 0.0


def test_set_language_detector_swaps_the_singleton():
    set_language_detector(NullLanguageDetector())
    locale, _ = resolve_locale("Привет", fallback_locale="en")
    # Detector returned None → fallback wins.
    assert locale == "en"


# ── Protocol conformance ─────────────────────────────────────────────────


def test_unicode_detector_satisfies_protocol():
    assert isinstance(UnicodeScriptDetector(), LanguageDetector)


def test_null_detector_satisfies_protocol():
    assert isinstance(NullLanguageDetector(), LanguageDetector)


def test_detection_result_is_frozen():
    out = DetectionResult(locale="ru", script="Cyrillic", confidence=0.9)
    with pytest.raises(Exception):                  # FrozenInstanceError
        out.locale = "en"                           # type: ignore[misc]


def test_get_language_detector_is_stable_across_calls():
    a = get_language_detector()
    b = get_language_detector()
    assert a is b
