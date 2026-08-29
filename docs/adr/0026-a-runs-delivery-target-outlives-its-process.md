# 0026 - A run's delivery target outlives its process

## Context

Publishing an approved change needs four facts captured at clone time: the repository, the
branch, the scenario, and the commit the clone started from. They are not in the graph state -
a run that fails to clone never has a graph - and the workspace that held them is deleted
immediately after the terminal state, by design, so the outcome event has to carry them.

They lived in a `dict` on `Worker`. That object's own docstring said why:

> `commit_sha_before` is captured once, at clone time, and a run reaches its terminal state on
> a later slice - after a gate, in a different call, **possibly in a different process**.
> Holding it here is what stops it being lost in between.

The first two clauses are right and the conclusion does not follow. A process-local dict
survives a later slice and a different call. A different process is precisely what it does not
survive. The comment describes the requirement accurately and then names a mechanism that
meets two thirds of it, which is why it read as settled.

**Observed end to end on 2026-08-29**, driving a real drift event through the deployed stack.
Run `drift-eventbus-p95_latency_ms-task1b` parked at `codebase_impact_review`, the consumer was
restarted, and the run was then approved through both gates:

```
Committed 24564efd in /workspaces/drift-eventbus-p95_latency_ms-task1b
Run ... reached terminal state completed
WARNING Run ... completed with no target on record; not publishing
Removed workspace for run ...
```

The outcome event that reached the topic:

```json
"git_target": {"repo_url": "unknown", "branch": "unknown", "commit_sha": null},
"payload": {"terminal_state": "completed", "commit_sha_after": "24564efd..."}
```

A run in the same process reported its repository and starting commit correctly. Three things
went wrong at once, and only the first is loud:

1. The approved, tested, committed change was **not published** - defeating the guarantee
   [ADR 0012](0012-an-approved-change-must-outlive-the-run-that-made-it.md) is named after,
   by a route that ADR did not consider: it protected the change from `workspace.cleanup`
   within a process, not from the process ending.
2. The outcome event carried `repo_url: "unknown"` and a hardcoded `scenario_type` fallback, on
   a run reporting `completed` with a real `commit_sha_after`. A consumer of that topic sees a
   successful run against a repository that does not exist.
3. `workspace.cleanup` then deleted the only copy of commit `24564efd`. The work is gone.

A quieter fourth: `_publish_audit` needs the same git context for its envelope, so every run
resumed in a fresh process stopped publishing audit records to the topic and wrote only to the
local file - losing the independent copy [ADR 0008](0008-the-audit-trail-must-be-checkable-not-merely-appended.md) relies on
as its anchor, exactly when a restart made the local file least trustworthy.

This is not an edge case. `PARKED_RUN_TTL_HOURS` defaults to 24, so a run waiting on a human
overnight crosses any restart, deploy or crash in that window. Deploying anything to the
consumer - including the observability work this platform has queued - is itself the trigger.

No test caught it because the only two tests touching the dict populated it by hand
(`worker._targets["run-spec"] = ...`) and then drove the same `Worker` object. Both halves
looked complete from inside themselves, which is the failure
[ADR 0023](0023-which-programs-a-run-migrates-is-deployment-data.md) already named.

## Decision

A run's delivery target is written to a `run_targets` table in this service's own Postgres when
the run starts, read back by whichever process reaches the terminal state, and deleted when the
outcome is published.

Deliberately the same mechanism as [ADR 0010](0010-durable-work-hand-off.md), one level up: a
small table in the database this service already owns, a connection per operation, and a row
whose lifetime is the run's. The problem is the same shape - state accepted by one process and
needed by another - so it gets the same answer rather than a second one.

Three details are load-bearing:

- **The target is recorded before the clone and again after it.** Before, so the clone-failure
  path still has a repository to name in its outcome; after, because `commit_sha_before` is only
  knowable once the clone succeeds. The write is an upsert, which also makes a redelivered
  trigger for a known run a no-op rather than a primary-key collision.
- **The two writes have opposite failure policies.** The first raises, and `handle_trigger`
  turns that into a refusal: nothing has started, no workspace, no checkpoint, and a run whose
  target was not written down is a run that cannot be delivered. The second only logs, because
  it is adding a commit to a row that already names the repository - its failure costs a null
  field on one event, not a delivery.
- **Reading and forgetting never raise.** Both are called while an outcome is being published.
  Turning a bookkeeping failure into a failed run there would be a worse trade than the degraded
  report they fall back to, which is the same reasoning `Inbox.discard` uses.

`Worker` still accepts a process-local store, and still defaults to one so it stays constructible
without Postgres, as `inbox` does. What changed is that `main` wires the durable store
explicitly: a deployment that runs without it is choosing the old behaviour rather than
inheriting it.

## Consequences

Delivery, the outcome event's git context, and the audit topic's independent copy all survive a
restart. The run above would have published its branch.

The regression tests build a **second** `Worker` over a run the first one started, because that
is the only position from which this is visible. That is the real calling position, and the
existing tests' single-object setup is what hid the defect - a point worth keeping in mind for
anything else on `Worker` that looks per-run.

A second record of a fact the run already implies is a cost, and this repository's usual
instinct - visible in `run_routing` - is to derive rather than duplicate. It does not apply
here: the outcome must be publishable for `clone_failed` and `routing_failed`, where no graph
and no checkpoint ever existed, so there is nothing to derive from. The row is deleted with the
run, so the duplication is bounded to the run's own lifetime.

Rows can leak if a process dies between the terminal state and the delete. The cost is a row,
and the TTL sweep publishes an outcome for anything parked past its deadline, which deletes it.
A periodic reaper is the bounded upgrade if that stops being true.
