# Functional verification report

**This is the evaluation tier's primary artefact, and for this repository it outranks the
coverage number.** Most of what can go wrong in a control plane cannot be reached by a unit
test: a gate that survives a container restart, a consumer that rebalances, a clone that
authenticates. Every row below was produced by running against real infrastructure, and the
command or run that produced it is named rather than summarised.

Moved here from README.md, where it sat between "Testing" and "Deployment" and read as an
appendix to the suite. It is not an appendix - **five defects appear below that the unit
suite passed straight through**, and they are the reason ADRs 0002 to 0006 exist.

Run end-to-end against a real broker and a real Postgres, from inside the container:

| Check | Result |
|---|---|
| Pattern subscription discovers a topic created after startup | PASS — run began ~31s after publish |
| Container consumes a drift event through the event bus's cross-container listener | PASS |
| Container publishes its outcome event through the same listener | PASS — consumed back off the topic, `producer.instance_id` matching the container hostname |
| Private-repo HTTPS clone using the mounted PAT | PASS — cloned at commit `335472aa`, against a repository that was private when this was run |
| A PAT lacking access fails cleanly | PASS — 403 became a `clone_failed` outcome, no crash |
| Run parks at a real `interrupt()` without blocking the poll loop | PASS |
| Kafka gate decision resumes the parked run | PASS |
| `test_executor` runs a real pytest subprocess against the clone | PASS — 1075ms |
| Full completion in-container | PASS — commit `9224abcd`, `completed`, workspace removed |
| Unmounted fixtures safe-stop with a stated reason | PASS |
| Workspace deleted on terminal state | PASS |
| Parked run resumed by a *different* process after the original was killed | PASS |
| Startup reconciliation removes orphaned workspaces | PASS |
| The documented script-free sequence above, run in replay mode | PASS — 2026-08-01, cloned at `820750d2`, both gates answered, real pytest passed in 1336 ms, 0 guardrail findings, commit `9637e763`, `completed` |
| The same sequence a second time, in the same process | PASS — `completed`, `run-outcome` published with the clone-time `commit_sha` |
| **`ORCHESTRATOR_MODE=live`: real generation via the `claude` CLI** | PASS — 2026-08-01, coder generated 2 file(s) in 111 875 ms, `test_executor` passed a real pytest against them in 1 609 ms, 0 guardrail findings, commit `9251028d`, `completed` |
| The same sequence run from a **Linux** shell | PASS — driven from a Linux container against the same daemon, twice, both to `completed` (commits `26c3575a`, `65d809f5`) |
| **Every run is audited, not only the first** | PASS after ADR 0011 — two consecutive runs on a fresh audit volume recorded **17 records each** on `control-plane.audit.v1`, one continuous 34-record chain. Before the fix, the second run recorded **zero** despite completing |
| **`PUBLISH_MODE=branch`: an approved change reaches the tenant repository** | PASS — 2026-08-01, in-container run pushed `agentic-patch/clean-1785628701` at `b0d47320`; the outcome event carried `commit_sha_after`, `published: true` and the branch. `main` untouched |
| The delivered branch carries the change and nothing else | PASS — after excluding build artefacts: 3 files (module, its test, the generated doc). The first delivery before that fix carried 4 `.pyc` files |
| Hash-chained audit trail | PASS — `verify_chain` clean over all 34 records, and continuous across a container restart |
| An empty `event_id` is rejected rather than acted on | PASS — envelope validation routed it to the DLQ; the parked run was untouched and resumed normally once a valid decision arrived |

The two rows about running the sequence twice are the ones worth understanding together, because
the second run is what exposed [ADR 0011](../../docs/adr/0011-the-audit-cursor-belongs-to-the-run-not-the-process.md).
Both runs completed and published outcomes, and `verify_chain` reported the trail intact — while
the second run was missing from it entirely. A chain proves record N follows N−1; it is evidence
about the records that are present and none at all about the ones that should be. The check that
actually catches it is counting records per `correlation_id` on the audit topic against runs
served, which is what the "every run is audited" row reports.

Memory, sampled across a full run including the pytest subprocess: **69.6 MiB against the 1 GiB
limit**, flat throughout; the Postgres container sits at 36 MiB against 512 MiB.

Five defects were found by these runs and by nothing else, with a full unit suite passing
throughout all of them. They are written up in `../../docs/adr/0002` through `0006`.

## Standing to this report

Rows are dated where the run that produced them was dated. A row is evidence about the day
it was run and the tree it was run against, not a standing guarantee - re-run rather than
re-read when it matters.
