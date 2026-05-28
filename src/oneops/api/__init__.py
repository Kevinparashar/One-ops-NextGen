"""HTTP ingress for OneOps — `/chat` (NL door) + `/fast/{uc_id}` (button door).

The ingress is a thin envelope layer: it validates the request, constructs
the per-turn `request_id`, stamps the tenant + role from the auth context
(today: dev-mode header bypass; production: JWT validated by AWS API Gateway
upstream), and hands the envelope to the compiled LangGraph executor.

Production invariants the ingress enforces ([[v4_operating_substrate]]):

  * **Per-turn `thread_id`.** Each request gets its own LangGraph checkpoint
    thread (`thread_id = request_id`), NOT the session_id — so concurrent
    turns from one user run as independent threads and never collide.
    Both turns still read/write the shared session event log under
    `session_id`. ([[feedback_no_turn_cap]] reinforces this: history is
    bounded per-turn, not by collapsing concurrent turns.)
  * **Multi-tenant safe.** `tenant_id` + `user_id` + `role` come from the
    request envelope (auth-validated), never from request body free-text.
  * **Same response contract for both doors.** The handler's output shape
    is identical regardless of which ingress route invoked it; the contract
    test in the integration suite verifies this.
  * **OTel root span per turn.** Already opened by `run_turn` — every node,
    tool, LLM, policy, and DB call nests under it.
"""
from oneops.api.app import build_app, create_app

__all__ = ["build_app", "create_app"]
