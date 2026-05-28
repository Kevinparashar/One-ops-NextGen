"""Policy composer — assembles per-profile system prompts at runtime.

Design:

1. The authoritative source of policy text is `docs/policies/updated_policy_v2.md`.
   Each named block in that file is declared as `BLOCK_NAME = \"\"\"...\"\"\"`.
2. `blocks.py` parses the markdown ONCE at import and exposes the blocks as a
   read-only mapping. No copy-paste, no drift between docs and code.
3. `profiles.py` defines the canonical profile recipes (PLANNER_POLICY_PROFILE,
   FEATURE_AGENT_WITH_TOOLS_POLICY_PROFILE, etc.) as ordered lists of block names.
4. `composer.compose(profile, context)` assembles the final system prompt:
     - concatenates the named blocks in profile order
     - substitutes runtime context ($message, $request_id, $tenant_id, $user_id,
       $role, $session_id, $locale, $ticket_id) into USER_CONTEXT_TEMPLATE slots

Concurrency:
- Block + profile registries are immutable after import (Python's GIL serializes
  module import). All reads after that are safe across threads / asyncio tasks.
- compose() is pure — no shared mutable state.

NO STATIC KEYWORDS / UC NAMES:
- Block names are loaded structurally from the markdown.
- Profile recipes reference block names only.
- Runtime context is per-call, passed in by the caller.
"""
from __future__ import annotations

from oneops.policy.blocks import POLICY_BLOCKS, get_block, list_block_names
from oneops.policy.composer import (
    POLICY_PROFILES,
    Profile,
    compose,
    list_profile_names,
)

__all__ = [
    "POLICY_BLOCKS",
    "POLICY_PROFILES",
    "Profile",
    "compose",
    "get_block",
    "list_block_names",
    "list_profile_names",
]
