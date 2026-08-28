# ADR-0024: Two graphs deliver, and only one of them delivers from the consumer

## Status

**Accepted** (2026-08-28). Fixes a defect the first real specialist run exposed. Makes
[ADR-0009](0009-the-audit-trail-does-not-live-in-the-workspaces-root.md)'s read-only rule true in
code rather than by accident, and completes
[ADR-0021](0021-nothing-commits-before-the-release-gate.md) § 1's delivery story for the specialist
graph. Supersedes nothing.

## Context

Run `step49-cbact04c-20260828-144511` — the first run to drive a real specialist end to end —
finished like this:

```
publish: Run step49-... pushed to agentic-patch/step49-...
runner:  Run step49-... reached terminal state completed
ERROR    publish: Run step49-... could not publish its change: git push failed:
         remote: Permission to jayakumar-devaraj/carddemo-tenant-service.git denied
```

The first line is the specialist graph's own `publish` node, delivering to the **output**
repository the route names. Correct. The third is `Worker._deliver_if_completed` publishing a
**second** time, and aiming at the **tenant** repository — the checkout ADR-0009 keeps read-only
for the whole run.

`_deliver_if_completed` predates the specialist graph. It was written when there was one graph, and
for that graph it is right: the SDLC graph commits into the run's own workspace and the consumer
pushes that workspace to the repository the run was triggered against (ADR-0012). It fires on
`terminal_state == "completed"` and asks nothing about which graph produced it.

**It was refused by GitHub, and that is the part worth recording.** The push failed with a 403
because the PAT this deployment happens to carry has no write grant on the tenant repository. Had
the operator granted one — an entirely reasonable thing to do, since the tenant repository is the
one the platform is nominally *about* — the control plane would have pushed a branch into a
checkout two ADRs describe as read-only, and nothing in this package would have objected.

A guarantee that holds only because of how someone scoped a token is not a guarantee this
repository provides.

## Decision

### 1. `_deliver_if_completed` returns early for a specialist run

The specialist graph delivers in its own `publish` node, after its own gate, to its own output
repository. The consumer's delivery path is therefore **SDLC-only**, and now says so.

Stated as a property rather than as a special case: *each graph delivers once, from the node that
knows where its output belongs.* The specialist graph knows about `output_repository`; the consumer
knows about `target.repo_url`. Neither should deliver on the other's behalf, and the bug was one of
them doing exactly that.

### 2. The discriminator is the one that already exists

`run_routing.is_specialist_run(values)` — a public reading of the `specialist` field that
`graph_for_resume` already keys on, extracted so a second caller cannot invent a second way of
asking. The field's own comment gives the reason it is read off the state instead of stored beside
it: *a second record of the same fact is a second thing that can disagree with the first.* A
`graph_kind` flag on `_RunTarget` would have been that second record.

### 3. The outcome event still reports delivery, from what the graph recorded

Returning early must not make the run-outcome event worse. `publish_node` already writes
`published`, `publish_branch` and `commit_sha_after` into the graph's state, so the outcome is read
from there rather than produced by publishing again. ADR-0012's posture — the outcome records what
happened to the change — is unchanged; only the source of the record moves.

## Consequences

**ADR-0009's read-only rule is now enforced by this package.** It was previously enforced by the
scope of a credential, which is to say it was enforced by whoever last configured a token.

**A test asserts the absence of an action**, which is unusual here and deliberate. The two new tests
are a pair: one asserts a specialist run publishes from this path *not at all*, and the other
asserts an SDLC run still publishes exactly as before. The second exists because a guard that
skipped delivery for everything would satisfy the first while silently removing the path ADR-0012
was written for. Confirmed by removing the guard and watching the first fail with
`assert not ['https://.../tenant-service.git']` and the second pass.

**This was invisible to every test that predates it**, and not because the tests are weak. The
specialist graph's tests drive the graph, which delivers correctly; the consumer's tests drive SDLC
runs, which deliver correctly. Nothing exercised a *specialist run reaching the consumer's terminal
handling*, because until this run nothing had ever got there. That is the same shape as
ADR-0023's missing `--programs`: two correct halves, and a defect living in the join.

**What is still not fixed**, both found on the same run and filed rather than folded in here:
`specialists._docker_daemon_reachable()` returns a false negative on this deployment because it
shells out to a `docker` CLI the image does not install, and the specialist graph never calls
`require_runtime` at all. They interact, so they belong in one change together.
