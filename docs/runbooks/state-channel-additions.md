# Runbook — Adding a new state channel to `OneOpsState`

**Last verified:** 2026-05-17 (two passes):
- G-Deploy Path B (Phase 4 batch 1): `Annotated[list[dict], list-concat-reducer]` verified
- Step 5.1 dict-merge probe (Phase 5): `Annotated[dict[str, T], merge_dict]` verified, including concurrent-write via parallel Sends

---

## Verified scope — read this first

Two patterns verified safe for v2 → v2.1 channel addition:

**Pattern 1 — List-concat** (verified Phase 4 batch 1, 2026-05-17):
- **Declaration:** `Annotated[list[dict], list-concat-reducer]`
- **Reducer:** `lambda l, r: (l if l is not None else []) + (r if r is not None else [])`
- **Identity:** `[]`

**Pattern 2 — Dict-merge** (verified Phase 5 step 5.1, 2026-05-17):
- **Declaration:** `Annotated[dict[str, T], merge_dict]`
- **Reducer:** `lambda l, r: {**(l or {}), **(r or {})}`
- **Identity:** `{}`
- **Bonus verification:** concurrent writes from parallel `Send` payloads merge correctly without lost-update (the central Phase 5 production pattern)

For both verified patterns:
- `state.get("new_channel")` returns reducer identity (NOT `None`, NOT KeyError)
- `state["new_channel"]` direct access returns identity (NO KeyError)
- Reducer **never called with `None` as left** — LangGraph supplies identity
- WRITE through reducer persists correctly via merge

---

## PR-review rules for adding a state channel

### R1 — Use `Annotated[T, reducer]` declaration

```python
class OneOpsState(TypedDict, total=False):
    # GOOD — Annotated + reducer
    audit_log: Annotated[list[dict], list_concat_reducer]
```

Bare `field: SomeType` declarations are NOT verified safe on checkpoint restore — see Caveat C below.

### R2 — Reducer identity must be trivially constructible

```python
# GOOD — identity is [] which constructs trivially
def list_concat_reducer(l, r):
    return (l if l is not None else []) + (r if r is not None else [])

# UNVERIFIED — identity is {} (likely safe but re-test)
def dict_merge_reducer(l, r):
    return {**(l or {}), **(r or {})}

# UNSAFE — identity construction may raise
class NonEmptyList:
    def __init__(self): raise ValueError("must contain at least one element")
```

### R3 — Defensive code in nodes (good / ok / bad)

```python
# good — belt + suspenders
audit = state.get("audit_log") or []

# ok — verified safe for list-concat pattern
audit = state["audit_log"]

# bad — assumes non-empty without checking
audit = state["audit_log"]
last_entry = audit[-1]  # IndexError on identity
```

### R4 — Reducer must handle identity-element input

The reducer's first invocation receives `left = identity_element`. Verify with a unit test:

```python
def test_reducer_handles_identity():
    assert your_reducer(your_identity, [sample_item]) == [sample_item]
    assert your_reducer(your_identity, []) == []  # both empty must not crash
```

### R5 — Channel REMOVAL is NOT covered by this verification

If v2.1 drops a channel that v2 wrote:
- The persisted v2 checkpoint still contains the old channel's data
- v2.1 code that doesn't declare the channel may still see it via `state.get()`
- Behavior is UNVERIFIED — re-test before removing.

### R6 — Run the 3-minute Path-B-shape smoke after adding a channel

Copy `tests/stress/g_deploy_path_b.py`, swap the new channel + reducer + identity, run:

1. Build v2 graph (without new channel), run a turn, persist checkpoint
2. Build v2.1 graph (with new channel + reader node + writer node)
3. Run a follow-up turn on the same `thread_id`
4. Verify: no crash, reader gets identity, writer persists through reducer

Cost: ~3 minutes per channel. Mandatory.

---

## Caveats — patterns NOT verified by the current test

### Caveat A — Non-trivial-identity reducers
- **Dict-merge (`Annotated[dict[str, T], merge_dict]`) — VERIFIED SAFE (2026-05-17, Phase 5 step 5.1 smoke).** Same restore behavior as list-concat. State.get and direct state["ch"] both return `{}` (identity). Concurrent writes from parallel Sends merge correctly without lost-update. Safe to use without separate Path-B re-test for future channel additions of this pattern.
- Set-union (`Annotated[set, ...]`) — re-test
- Counter/accumulator (`Annotated[int, ...]`) — re-test
- Custom-type reducer where identity is a dataclass with required fields — risk of identity-construction crash; AVOID this pattern, OR add an explicit identity factory

### Caveat B — Plain TypedDict fields with NO Annotated reducer
- Existing `OneOpsState` fields without reducers (e.g. `canonical_state_loaded`, `plan`, `final_status`) — restore behavior NOT verified
- Most likely behavior (UNTESTED): `state.get("plain_field")` returns whatever the channel-init logic supplies — may be `None`
- `state["plain_field"]` direct access may raise KeyError on missing
- **Before adding a new plain TypedDict field that ANY node will read, run a Path-B-shape smoke specifically for the plain-field case.**

### Caveat C — Reducers whose identity element raises during construction
- A reducer requiring a non-default constructor or whose identity has invariants beyond the type system may crash channel initialization
- Likely failure mode: graph compilation succeeds but `g.ainvoke()` raises on restore
- **Action: avoid this pattern.** If invariants are needed, enforce them at write time inside the reducer, not at the type level.

---

## Deploy posture for channels added in Phase 4 batch 1

Channels added: `subqueries`, `rewritten_message`, `routing_shortlist`, `routing_reranked`, `decomposer_*` observability fields, `rewriter_*` observability fields, `routing_gate_verdict`.

All use Annotated reducers with trivial identities OR simple `Optional[str|bool]` fields populated fresh each turn from `initial_state_from_envelope`. **Safe to deploy without explicit migration step.** Verified by G-Deploy Path A (v1 legacy → v2 three_stage) on same `thread_id` — Phase 4 batch 1 final smoke.
