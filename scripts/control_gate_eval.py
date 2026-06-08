#!/usr/bin/env python3
"""Control-gate eval — 100 UNSEEN queries, all aspects, gpt-4o-mini vs gpt-4o.

Tests ONLY the Stage-1 control gate (conversation/control_gate.py) — its job is
to classify each message as social/meta (canned reply), off-domain (refuse), or
in-domain task (`none` → fall through to the router). The live bug: gpt-4o-mini
over-refuses in-domain IT how-to as out_of_scope. This scores three buckets:

  social : greeting/thanks/identity/help/chitchat → any social/meta label
  off    : genuinely non-IT  → out_of_scope or chitchat (refuse)
  in     : IT/ticket task    → MUST be `none` (the over-refusal metric)

Each query is classified with a UNIQUE tenant → guaranteed cache MISS → fresh
model verdict. Parallel (bounded). Run:
  .venv/bin/python scripts/control_gate_eval.py
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except Exception:                                              # noqa: BLE001
    pass

import os  # noqa: E402

from oneops.conversation.control_gate import LlmControlClassifier  # noqa: E402
from oneops.llm import LlmGateway  # noqa: E402
from oneops.llm.transport import LiteLLMTransport  # noqa: E402

_SOCIAL = {"greeting", "wellbeing_check", "farewell", "thanks", "acknowledgement",
           "compliment", "apology", "frustration", "identity", "help_inquiry",
           "capabilities_inquiry", "chitchat"}

# (query, bucket) — bucket ∈ {social, off, in}. All unseen phrasings.
DATA: list[tuple[str, str]] = [
    # ── social: greetings ──
    ("hey there", "social"), ("good evening", "social"), ("morning!", "social"),
    ("yo", "social"), ("hello again", "social"), ("hiya", "social"),
    ("good day to you", "social"), ("namaste", "social"),
    # ── social: gratitude / ack / compliment / apology / farewell ──
    ("thanks a ton", "social"), ("much appreciated", "social"),
    ("cheers for that", "social"), ("okay sure", "social"),
    ("got it thanks", "social"), ("understood", "social"),
    ("you're really helpful", "social"), ("great job", "social"),
    ("sorry my mistake", "social"), ("oops my bad", "social"),
    ("catch you later", "social"), ("that's all, bye", "social"),
    # ── social: identity / help / capabilities ──
    ("are you a real person", "social"), ("what are you exactly", "social"),
    ("what can you help me with", "social"), ("how does this work", "social"),
    ("what record types do you support", "social"), ("list your features", "social"),
    ("can you do incident stuff", "social"), ("what are you good at", "social"),
    # ── social: chitchat ──
    ("how was your day", "social"), ("tell me something interesting", "social"),
    ("are you bored", "social"), ("what's up", "social"),
    ("do you like your job", "social"),
    # ── off-domain (genuinely non-IT) → refuse ──
    ("will it rain tomorrow", "off"), ("temperature today", "off"),
    ("score of the game last night", "off"), ("who's winning the series", "off"),
    ("how do i bake bread", "off"), ("best pizza recipe", "off"),
    ("good lunch spot nearby", "off"), ("flights to paris", "off"),
    ("hotels in goa", "off"), ("suggest a netflix show", "off"),
    ("play a song for me", "off"), ("capital of brazil", "off"),
    ("how many continents are there", "off"), ("who wrote hamlet", "off"),
    ("how do i lose weight", "off"), ("give me relationship advice", "off"),
    ("what should i wear today", "off"), ("write me a haiku", "off"),
    ("tell me a joke", "off"), ("what's 47 times 3", "off"),
    # ── in-domain IT how-to → MUST be none (the over-refusal test) ──
    ("how do i connect to the guest wifi", "in"),
    ("vpn won't connect from home", "in"),
    ("how to set up a static IP", "in"),
    ("wifi keeps dropping on my laptop", "in"),
    ("how do i wipe a company macbook", "in"),
    ("screen flickering on my monitor", "in"),
    ("how to pair a bluetooth headset to my work laptop", "in"),
    ("factory reset my work phone", "in"),
    ("how do i update windows", "in"),
    ("excel won't open", "in"),
    ("teams audio not working", "in"),
    ("how to install slack", "in"),
    ("outlook calendar not syncing", "in"),
    ("how do i change my AD password", "in"),
    ("mfa not sending codes", "in"),
    ("locked out of my account", "in"),
    ("how to set up SSO", "in"),
    ("reset my okta access", "in"),
    ("how do i restart a kubernetes pod", "in"),
    ("aws s3 access denied", "in"),
    ("docker container won't start", "in"),
    ("how to increase disk on the server", "in"),
    ("database connection timeout", "in"),
    ("how do i create a distribution list", "in"),
    ("shared mailbox not showing up", "in"),
    ("how to recover a deleted teams channel", "in"),
    ("status of my open tickets", "in"),
    ("what's the priority", "in"),
    ("who's the assignee", "in"),
    ("any update on my request", "in"),
    ("show my incidents", "in"),
    ("is the sla breached", "in"),
    ("what changes are scheduled this week", "in"),
    ("details on the asset", "in"),
    ("what's the root cause", "in"),
    ("i need help with a printer issue", "in"),
    ("my laptop is broken", "in"),
    ("email is down for the whole team", "in"),
    ("the server is unreachable", "in"),
    ("my vpn keeps dropping", "in"),
    ("laptop won't wake from sleep", "in"),
    ("is the network down", "in"),
    ("printer offline again", "in"),
    ("can someone fix my email", "in"),
    ("hi, my outlook is broken", "in"),
    ("thanks, now show my open tickets", "in"),
    ("priority", "in"),
]


def _gateway() -> LlmGateway:
    return LlmGateway(transport=LiteLLMTransport(
        base_url=os.environ.get("LLM_GATEWAY_URL", "http://localhost:4311"),
        api_key=(os.environ.get("LLM_GATEWAY_API_KEY")
                 or os.environ.get("LITELLM_MASTER_KEY") or "sk-1234")), redact=False)


def _ok(bucket: str, label: str | None) -> bool:
    lbl = label or "none"
    if bucket == "in":
        return lbl == "none"                       # must fall through
    if bucket == "off":
        return lbl in ("out_of_scope", "chitchat")  # refuse
    return lbl in _SOCIAL                            # social/meta label


async def _score(model: str, gw: LlmGateway) -> tuple[dict, list]:
    clf = LlmControlClassifier(gw, model=model)
    sem = asyncio.Semaphore(6)

    async def one(i: int, q: str, bucket: str):
        async with sem:
            # unique tenant per (model,query) → guaranteed cache miss.
            # NOTE: full model name (not model[:4] — both models share "gpt-").
            tag = model.replace("-", "_").replace(".", "_")
            lbl = await clf.classify(message=q, tenant_id=f"CG_{tag}_{i}")
            return (q, bucket, lbl, _ok(bucket, lbl))

    res = await asyncio.gather(*[one(i, q, b) for i, (q, b) in enumerate(DATA)])
    by = defaultdict(lambda: [0, 0])
    fails = []
    for q, b, lbl, ok in res:
        by[b][1] += 1
        by[b][0] += int(ok)
        if not ok:
            fails.append((b, q, lbl or "none"))
    return by, fails


async def main() -> int:
    gw = _gateway()
    n = len(DATA)
    for model in ("gpt-4o-mini", "gpt-4o"):
        by, fails = await _score(model, gw)
        total = sum(v[0] for v in by.values())
        print(f"\n========== control gate: {model} — {total}/{n} = {total/n*100:.1f}% ==========")
        for b in ("social", "off", "in"):
            ok, tot = by[b]
            print(f"  {b:<7} {ok}/{tot} = {ok/tot*100:3.0f}%")
        if fails:
            print(f"  fails ({len(fails)}):")
            for b, q, lbl in fails:
                print(f"    [{b}] {q[:48]!r} → {lbl}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
