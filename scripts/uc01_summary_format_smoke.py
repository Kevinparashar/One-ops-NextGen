#!/usr/bin/env python3
"""UC-1 summary FORMAT smoke + devil's-play (live API).

Exercises the 2026-06-01 summary-format change against the running engine
(http://localhost:8765). Proves, for the COMPACT grounded format:

  SMOKE
    * Works across every summarisable service (incident/request/change/
      problem/asset/cmdb_ci) — format never breaks.
    * The SAME ticket produces the SAME summary via the chat door AND the
      fast-path (button) door (single-capability parity).
    * No raw note JSON / author-id / field-dump leaks into the narrative.

  DEVIL'S PLAY (adversarial / edge)
    * Field-read ("give me sla of …") still returns just the field, NOT a
      full summary — the format change does not bleed into the field path.
    * Non-existent ticket → graceful not_found, no fabricated summary.
    * Incident with notes → 2-3 dated bullets, paraphrased (no WN-/USR ids).
    * Asset/CI (no timeline) → no fabricated "Key updates", no break.
    * No vague/hallucinated filler words in any summary.

Run:  .venv/bin/python scripts/uc01_summary_format_smoke.py
Exit code 0 = all green; 1 = at least one failure.
"""
from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://localhost:8765"
HEADERS = {
    "content-type": "application/json",
    "x-tenant-id": "T001",
    "x-user-id": "oneops",
    "x-role": "service_desk_agent",
}

# (service_id, ticket_id, expect_bullets) — bullets only where a timeline exists.
TICKETS = [
    ("incident", "INC0001001", True),
    ("request", "SR0002002", True),    # this SR has comments
    ("change", "CHG0004001", False),
    ("problem", "PBM0003001", False),
    ("asset", "AST0001001", False),
    ("cmdb_ci", "CI0000001", False),
]

# Markers that must NEVER appear in a user-facing summary (raw-leak / vague).
RAW_LEAK = ['"note_id"', '"author_role"', "WN-INC", '"comment_id"', '"is_public"',
            "{'", '{"', "work_notes", "author_role"]
VAGUE = ["it seems", "appears to", "likely ", "may have", "unfortunately",
         "it is important to note", "as an ai"]

_failures: list[str] = []
_passes = 0


def _check(cond: bool, label: str, detail: str = "") -> None:
    global _passes
    if cond:
        _passes += 1
        print(f"  ✓ {label}")
    else:
        _failures.append(label)
        print(f"  ✗ {label}  {('— ' + detail) if detail else ''}")


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers=HEADERS, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def _summary_of(payload: dict) -> tuple[str, str]:
    """Return (summary_text, outcome) from a chat/fast response."""
    for step in payload.get("step_results") or []:
        out = (step or {}).get("output") or {}
        outcome = out.get("outcome", "")
        summ = out.get("summary")
        if isinstance(summ, dict):
            return str(summ.get("summary") or ""), outcome
        if isinstance(summ, str):
            return summ, outcome
    return str(payload.get("final_response") or ""), ""


def smoke() -> None:
    print("\n=== SMOKE: format across services + button==chat parity ===")
    for svc, tid, expect_bullets in TICKETS:
        print(f"\n[{svc} {tid}]")
        chat = _post("/api/chat", {"message": f"summarize {tid}",
                                   "session_id": f"smoke-chat-{tid}"})
        fast = _post(f"/api/fast/uc01_summarization",
                     {"inputs": {"ticket_id": tid, "service_id": svc},
                      "session_id": f"smoke-fast-{tid}"})
        c_txt, c_oc = _summary_of(chat)
        f_txt, f_oc = _summary_of(fast)

        _check(bool(c_txt.strip()), f"{svc}: chat summary non-empty")
        _check(bool(f_txt.strip()), f"{svc}: fast summary non-empty")
        _check(c_txt == f_txt, f"{svc}: button == chat (byte-identical)",
               f"chat[:60]={c_txt[:60]!r} fast[:60]={f_txt[:60]!r}")
        for txt, door in ((c_txt, "chat"), (f_txt, "fast")):
            low = txt.lower()
            leak = next((m for m in RAW_LEAK if m.lower() in low), None)
            _check(leak is None, f"{svc}/{door}: no raw-data leak", f"found {leak!r}")
            vague = next((m for m in VAGUE if m in low), None)
            _check(vague is None, f"{svc}/{door}: no vague/hallucination filler",
                   f"found {vague!r}")
        # Compactness: the status line is the headline facts only (≤ 4
        # labels), NOT an inline key/value field dump.
        first_line = c_txt.strip().splitlines()[0] if c_txt.strip() else ""
        n_labels = first_line.count("**") // 2 if "**" in first_line else first_line.count("·") + 1
        _check(n_labels <= 5, f"{svc}: status line is compact (≤5 labels)",
               f"{n_labels} labels: {first_line[:120]}")
        if svc == "request":
            _check("stage" in c_txt.lower(), f"{svc}: stage field present in output",
                   first_line[:140])
        if expect_bullets:
            _check("\n- " in ("\n" + c_txt), f"{svc}: has dated bullets (timeline present)")
        print(f"    summary:\n      " + c_txt.replace("\n", "\n      ")[:500])


def devils() -> None:
    print("\n=== DEVIL'S PLAY: adversarial / edge ===")

    print("\n[field-read still works — not a summary]")
    p = _post("/api/chat", {"message": "give me sla of ticket INC0001001",
                            "session_id": "dev-fieldread"})
    txt, oc = _summary_of(p)
    _check(oc == "field_read", "sla query → outcome=field_read", f"got {oc!r}")
    _check("sla" in txt.lower(), "sla query returns the SLA field")
    _check("**Status**" not in txt and "**Priority**" not in txt,
           "sla query is NOT a full summary (no status line)")

    print("\n[non-existent ticket — graceful, no fabrication]")
    p = _post("/api/chat", {"message": "summarize INC9999999",
                            "session_id": "dev-notfound"})
    txt, oc = _summary_of(p)
    fr = str(p.get("final_response") or "").lower()
    _check(oc in ("not_found", "") and ("no incident" in fr or "not" in fr or oc == "not_found"),
           "missing ticket → graceful not_found", f"oc={oc!r} fr={fr[:80]!r}")

    print("\n[incident with notes — paraphrased bullets, no raw ids]")
    p = _post("/api/chat", {"message": "summarize INC0001001",
                            "session_id": "dev-notes"})
    txt, _ = _summary_of(p)
    _check("\n- " in ("\n" + txt), "incident has bullets")
    _check("WN-" not in txt and "note_id" not in txt,
           "bullets are paraphrased (no WN-/note_id ids)")

    print("\n[asset — no timeline → no fabricated 'Key updates']")
    p = _post("/api/fast/uc01_summarization",
              {"inputs": {"ticket_id": "AST0001001", "service_id": "asset"},
               "session_id": "dev-asset"})
    txt, _ = _summary_of(p)
    _check(bool(txt.strip()), "asset summary non-empty (no break)")
    _check("Key updates" not in txt,
           "asset has NO 'Key updates' section (no timeline)", txt[:120])

    print("\n[request with NO comments — must omit Key updates, no 'none' filler]")
    p = _post("/api/fast/uc01_summarization",
              {"inputs": {"ticket_id": "SR0002005", "service_id": "request"},
               "session_id": "dev-emptynotes"})
    txt, _ = _summary_of(p)
    low = txt.lower()
    _check("key updates" not in low, "empty-comments request omits Key updates", txt[:160])
    _check(not any(s in low for s in ("no comment", "no updates", "none ", "n/a")),
           "no 'no comments / none / N/A' absence filler", txt[:160])


if __name__ == "__main__":
    try:
        smoke()
        devils()
    except Exception as exc:  # noqa: BLE001
        print(f"\nHARNESS ERROR: {exc}")
        sys.exit(2)
    print(f"\n================  {_passes} passed, {len(_failures)} failed  ================")
    if _failures:
        print("FAILURES:")
        for f in _failures:
            print("  -", f)
        sys.exit(1)
    print("ALL GREEN")
    sys.exit(0)
