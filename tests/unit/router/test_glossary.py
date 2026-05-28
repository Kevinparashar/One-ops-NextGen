"""Glossary normalization tests — including the missing-word case."""
from __future__ import annotations

from oneops.router.glossary import Glossary


def _g():
    return Glossary({
        "pwd": "password", "passwd": "password", "pass word": "password",
        "vpn": "virtual private network", "kb": "knowledge base",
    })


def test_known_synonym_is_rewritten():
    assert _g().normalize("reset my pwd") == "reset my password"


def test_multi_word_synonym_is_rewritten():
    assert _g().normalize("forgot my pass word") == "forgot my password"


def test_case_insensitive():
    assert _g().normalize("Reset my PWD") == "Reset my password"


def test_unknown_word_passes_through_unchanged():
    """The missing-from-dictionary case — the glossary is a helper, never a
    gate. A term it does not know is simply left as-is; nothing breaks."""
    out = _g().normalize("my flibbertigibbet is broken")
    assert out == "my flibbertigibbet is broken"


def test_word_boundary_respected():
    # "pwds" must NOT be rewritten — "pwd" is not a standalone word here.
    assert _g().normalize("rotate all pwds") == "rotate all pwds"


def test_empty_text():
    assert _g().normalize("") == ""


def test_multiple_synonyms_in_one_query():
    out = _g().normalize("kb article about vpn")
    assert out == "knowledge base article about virtual private network"


def test_tenant_overlay_wins_on_collision():
    base = _g()
    overlaid = base.overlaid_with({"kb": "tenant knowledge portal"})
    assert overlaid.normalize("open the kb") == "open the tenant knowledge portal"
    # Base entries still apply.
    assert overlaid.normalize("reset pwd") == "reset password"


def test_from_file_loads_the_platform_glossary():
    g = Glossary.from_file()
    assert g.synonym_count > 0
    assert g.normalize("check the vpn") == "check the virtual private network"
