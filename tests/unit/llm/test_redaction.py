"""Prompt-redaction tests — structural PII scrubbed before a prompt leaves."""
from __future__ import annotations

from oneops.llm.models import LlmMessage
from oneops.llm.redaction import redact_messages, redact_text


def test_email_is_redacted():
    out, found = redact_text("contact me at alice@example.com please")
    assert "alice@example.com" not in out
    assert "[REDACTED_EMAIL]" in out
    assert found == {"email"}


def test_ssn_is_redacted():
    out, found = redact_text("my ssn is 123-45-6789")
    assert "123-45-6789" not in out
    assert "ssn" in found


def test_credit_card_is_redacted():
    out, found = redact_text("card 4111 1111 1111 1111 expires soon")
    assert "4111 1111 1111 1111" not in out
    assert "credit_card" in found


def test_ip_address_is_redacted():
    out, found = redact_text("server at 192.168.10.24 is down")
    assert "192.168.10.24" not in out
    assert "ip_address" in found


def test_phone_is_redacted():
    out, found = redact_text("call +1 415 555 0142 today")
    assert "555 0142" not in out
    assert "phone" in found


def test_clean_text_passes_through_unchanged():
    out, found = redact_text("summarize incident INC0048213")
    assert out == "summarize incident INC0048213"
    assert found == set()


def test_multiple_pii_classes_in_one_text():
    out, found = redact_text("alice@x.com / ssn 111-22-3333")
    assert found == {"email", "ssn"}
    assert "alice@x.com" not in out and "111-22-3333" not in out


def test_redact_messages_returns_found_classes():
    messages = (
        LlmMessage("system", "you are an assistant"),
        LlmMessage("user", "email bob@example.com about it"),
    )
    scrubbed, found = redact_messages(messages)
    assert found == {"email"}
    assert scrubbed[0] == messages[0]                 # clean message untouched
    assert "bob@example.com" not in scrubbed[1].content
