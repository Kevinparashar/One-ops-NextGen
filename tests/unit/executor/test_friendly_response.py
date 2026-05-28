"""friendly_step_response — UC-1 contract A+B.

Verifies each branch of the response renderer:

  * success + LLM summary block → paragraph surfaces verbatim
  * success + handler structured outcome (not_found / invalid_request /
    llm_unavailable) → handler's already-friendly `message`
  * authz-recheck deny → "Your role doesn't allow you to read {entity_id}."
  * LLM gateway exhausted → friendly retry message
  * timeout → friendly retry message
  * unknown failure → short generic line (no internals leaked)
"""
from __future__ import annotations

import pytest

from oneops.executor.nodes import friendly_step_response


def _step(parameters=None, agent_id="uc01_summarization"):
    return {"step_id": "step_1", "agent_id": agent_id,
            "parameters": parameters or {"ticket_id": "INC0001001"}}


def _result(*, status="success", output=None, error=None):
    return {"step_id": "step_1", "agent_id": "uc01_summarization",
            "status": status, "output": output, "error": error}


# ── success: LLM-generated summary paragraph is what the user sees ──────


def test_success_with_summary_paragraph_is_surfaced_verbatim():
    out = friendly_step_response(
        _step(),
        _result(status="success", output={
            "outcome": "summarized",
            "summary": {
                "summary": "VPN tunnel resets repeatedly on Wi-Fi handoff.",
                "key_details": {"Status": "in_progress"},
                "model": "gpt-4o-mini",
                "usage": {"prompt_tokens": 120, "completion_tokens": 80,
                          "cost_usd": 0.0002},
            },
        }))
    assert out == "VPN tunnel resets repeatedly on Wi-Fi handoff."


def test_success_with_summary_as_plain_string_is_surfaced():
    out = friendly_step_response(
        _step(),
        _result(status="success", output={
            "outcome": "summarized",
            "summary": "Already a plain string.",
        }))
    assert out == "Already a plain string."


# ── success: handler structured outcomes use their friendly `message` ───


def test_not_found_outcome_uses_handlers_message_verbatim():
    out = friendly_step_response(
        _step(parameters={"ticket_id": "INC9999999"}),
        _result(status="success", output={
            "outcome": "not_found",
            "ticket_id": "INC9999999",
            "service_id": "incident",
            "message": "No incident with id INC9999999 was found for this tenant.",
            "summary": None,
        }))
    # The handler's message is already user-friendly; surface it as-is.
    assert "INC9999999" in out
    assert "found" in out.lower()


def test_invalid_request_outcome_uses_handlers_message():
    out = friendly_step_response(
        _step(parameters={}),
        _result(status="success", output={
            "outcome": "invalid_request",
            "message": "A ticket id is required to summarise a record.",
            "summary": None,
        }))
    assert out == "A ticket id is required to summarise a record."


def test_llm_unavailable_outcome_uses_handlers_message():
    out = friendly_step_response(
        _step(),
        _result(status="success", output={
            "outcome": "llm_unavailable",
            "message": "The summariser is not wired to an LLM in this process.",
            "summary": None,
        }))
    assert "summariser" in out.lower() or "summarisation" in out.lower()


# ── failure: authz_recheck deny → role-doesnt-allow phrasing ────────────


def test_authz_deny_yields_role_phrasing_with_entity_id():
    out = friendly_step_response(
        _step(parameters={"ticket_id": "INC0001001"}),
        _result(status="failed",
                error="before-hook aborted: HookError(authz_recheck: "
                      "role_not_in_audience)"))
    assert out == "Your role doesn't allow you to read INC0001001."


def test_authz_deny_without_entity_id_still_clear():
    out = friendly_step_response(
        _step(parameters={}),
        _result(status="failed",
                error="before-hook aborted: HookError(authz_recheck: …)"))
    assert "role doesn't allow" in out.lower()


# ── failure: LLM gateway exhaustion → friendly retry ────────────────────


def test_llm_gateway_failure_yields_retry_message():
    out = friendly_step_response(
        _step(),
        _result(status="failed",
                error="handler raised LLMGatewayError: LLM call failed after 3 attempt(s)"))
    assert "temporarily unavailable" in out.lower()
    assert "try again" in out.lower()


# ── failure: timeout → friendly retry ──────────────────────────────────


def test_timeout_yields_retry_message():
    out = friendly_step_response(
        _step(),
        _result(status="failed",
                error="handler timed out after 30.0s (tool=summarize_entity)"))
    assert "too long" in out.lower()
    assert "try again" in out.lower()


# ── failure: unknown error → short generic line, no internals leaked ────


def test_unknown_failure_yields_neutral_generic_line():
    out = friendly_step_response(
        _step(),
        _result(status="failed", error="some obscure internal error"))
    # The error string is NOT exposed to the user.
    assert "obscure internal" not in out.lower()
    # The line is short and neutral.
    assert "complete that request" in out.lower()


# ── entity-id extraction handles every UC's primary-key field ───────────


@pytest.mark.parametrize("param_key,value", [
    ("ticket_id",   "INC0001001"),
    ("article_id",  "KB0005010"),
    ("entity_id",   "INC0001001"),
    ("incident_id", "INC0001001"),
    ("problem_id",  "PBM0003003"),
    ("change_id",   "CHG0004007"),
    ("asset_id",    "AST0001006"),
    ("ci_id",       "CI0000003"),
])
def test_entity_id_extraction_finds_id_from_any_supported_field(param_key, value):
    out = friendly_step_response(
        _step(parameters={param_key: value}),
        _result(status="failed",
                error="before-hook aborted: HookError(authz_recheck …)"))
    assert value in out


# ── empty output / no signal ───────────────────────────────────────────


def test_success_with_no_surfaceable_text_falls_back_to_short_ack():
    out = friendly_step_response(
        _step(), _result(status="success", output={"some_field": 42}))
    assert out == "Done."
