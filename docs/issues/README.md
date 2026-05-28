# Issue ledger

Structured record of every non-trivial bug, failure mode, and design defect
the OneOps router encounters as it ships. Each issue is one file:
`ISS-NNN-short-slug.md`.

## Fixed structure per issue

```
# ISS-NNN: title

**Trigger:** the input or pattern that revealed it.

**Wrong behavior:** what the system did.

**Right behavior:** what it should have done.

**Root cause:** structural / prompt-level / model-level — name the layer.

**Fix:** what changed, where in code, on what date. "In-progress" if not landed yet.

**Test pinning:** which test file + test name captures the regression.

**Status:** active / in-progress / fixed / accepted-as-limitation / deferred.

**Related issues:** ISS-XXX, ISS-YYY.
```

## Why this exists

You can't accumulate engineering knowledge from scattered docs. D-tasks
track planned work. Findings docs track investigations. Audit logs track
production behavior. **This is the canonical place where issues and their
resolutions live**, in a structure that's grep-able, citable, and
reviewable.

When a new bug looks similar to an old one, search here first. When you
write a design doc, cite issues by ID. When you onboard someone, point
them at `docs/issues/` and the high-impact `fixed` issues teach the
system's lessons faster than reading the codebase.

## Status meanings

- **active** — known bug, no fix in progress, repro available.
- **in-progress** — fix being written; update to `fixed` when landed.
- **fixed** — fix landed AND tests pin it. Verify on rerun before closing.
- **accepted-as-limitation** — known constraint, won't be fixed in this version, documented for users / future maintainers.
- **deferred** — would fix but waiting on another component / decision.

## Discipline

Write the issue file BEFORE the fix lands. The act of structuring the
trigger / wrong / right / root-cause forces clearer thinking and surfaces
whether the proposed fix actually addresses the root cause. Writing
retroactively never happens — fix-mode crowds it out.
