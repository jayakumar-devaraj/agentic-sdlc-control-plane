# ADR-0021: Nothing commits before the release gate, so nothing needs rolling back

## Status

**Accepted** (2026-08-27). Completes step 47: the specialist graph
([ADR-0019](0019-a-specialist-run-is-its-own-graph.md)) can now deliver what it generates.
Extends [ADR-0009](0009-the-audit-trail-does-not-live-in-the-workspaces-root.md)'s two-repository
rule and inherits [ADR-0012](0012-an-approved-change-must-outlive-the-run-that-made-it.md)'s
delivery posture rather than inventing a second one.

## Context

Until this change a specialist wrote into a local directory under `SPECIALIST_OUTPUT_ROOT` and
nothing left the run. That is the first half of ADR-0009's contract — read the tenant checkout,
write somewhere else — with the second half missing: *somewhere else* was supposed to be a clone of
the target project.

This is also the first change in the sequence that does something **irreversible**. Everything
before it was undone by deleting a directory. This pushes to a repository.

The SDLC graph solves the equivalent problem with a `rollback` node: it commits as it goes and
reverts to `commit_sha_before` when a run fails or is rejected. That works, and it is the wrong
shape here.

## Decision

### 1. The commit happens after the gate, not before it

```
generate → (gate: merge_release_approval) → publish → END
                 │
                 └── rejected → END
```

`generate` writes files into the checkout and leaves them **uncommitted**. `publish` commits and
delivers, and it runs only on an approved gate.

**This is why there is no rollback node**, and the absence is the point rather than an omission:

- a `generate` that fails half-way leaves an untouched checkout, not a reverted one;
- a run nobody approves leaves the target project exactly as it found it;
- there is no window in which a commit exists that a later step has to undo.

Rollback is a mechanism for cleaning up after a decision made too early. Making the decision later
removes the need for it. The SDLC graph cannot do this — its test executor has to run against
committed state, and its rollback exists to serve that — which is why this is a difference between
two graphs rather than a correction to one.

### 2. The gate reuses `merge_release_approval`

Publishing generated Java and publishing generated Python are the same decision — *may this change
land* — so it carries the same name. The decision consumer already understands it, and ADR-0019
recorded that growing the shared gate vocabulary is the standing cost of having one namespace
across both graphs. A seventh gate type would have paid that cost for no distinction.

The gate payload carries the specialist's own account of what it produced, unmodelled, plus the
list of files that would be published — bounded, because it crosses a checkpoint.

### 3. The output checkout is cloned lazily, in the node that needs it

Not at trigger time. `generate` runs after a human gate, so it routinely runs in a different
process from the one that started the run and after any number of restarts; a clone taken at
trigger time would have to be reconstructed here anyway.

An existing checkout is **reused rather than re-cloned**: a design was approved against a
particular state of the world, and silently replacing it mid-run would discard that.

### 4. `output_repository` is optional, and absent means nothing is published

A route that names no target still runs, writes to a local directory, and ends after `generate` —
no release gate, because a gate over a directory nobody publishes asks a human to approve nothing.

Optional rather than required because **publishing to a repository nobody named is the one mistake
in this graph that cannot be undone by deleting a directory.** Every existing test that predates
this change exercises that path, unmodified.

### 5. Delivery is `PUBLISH_MODE`'s decision, and it still defaults to `none`

Reused wholesale from `publish.py` rather than reimplemented: `none` commits and reports,
`branch` pushes `agentic-patch/{run_id}`, `pull_request` also opens one. Governance without
delivery is the default for a specialist run for the same reason it is for an SDLC run.

**A delivery failure does not fail the run.** The code was generated and approved either way, and
the outcome records what happened to it — ADR-0012's posture, unchanged.

### 6. The second clone shares the first one's credential handling

`workspace.clone_repository` was split out of `clone_for_run` rather than written beside it. The
delicate part is that the token is never in the URL, never in `.git/config`, and never in an error
message; a second clone path that got that subtly wrong would be a credential leak nobody would
notice until it was in a log.

Splitting it surfaced a real defect in the process: the clone failure paths called
`cleanup(run_id)`, which resolves to the run's **workspace**. Correct while every clone lived under
`WORKSPACES_ROOT`; wrong the moment a run has two, where a failed output clone would have deleted
the run's source checkout. They now remove the tree they were cloning into, and a test asserts the
workspace survives.

## Consequences

**Step 47 is complete.** A drift event can now reach a specialist, produce a design, pass a human
review, generate code, pass a second review, and deliver it.

**Two gates now stand between a trigger and a repository**, and they guard different things: the
first protects an expensive phase and asks about an artifact a person can read; the second protects
a repository.

**The shipped routing table now names a real write target.** Reaching it needs two things the file
cannot supply — a write-scoped `GIT_PAT_FILE`, because that repository is private, and
`PUBLISH_MODE` set to something other than `none`. Both are deployment decisions, deliberately.

**What is not verified here.** No real specialist has run through this path: every test drives a
Python script standing in for one. The push *is* exercised against a real git remote rather than
reasoned about — a local origin, `PUBLISH_MODE=branch`, asserting the branch lands and that the
branch the run cloned is untouched — but no run has yet pushed to `card-service` itself, which
needs the credential and the mode above plus the `claude` session and Docker daemon ADR-0017 § 3
and § 5 describe. **`card-service` is therefore still empty**, and G7's contract is now implemented
end to end without having been exercised against the real target.
