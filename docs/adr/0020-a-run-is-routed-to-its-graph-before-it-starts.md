# ADR-0020: A run is routed to its graph before it starts

## Status

**Accepted** (2026-08-27). Completes what
[ADR-0019](0019-a-specialist-run-is-its-own-graph.md) left unconnected: the specialist graph
existed and nothing reached it. **Corrects the plan recorded in ADR-0016 § 5**, which named
`nodes/coder.py`'s `SpecialistNotWiredError` as the seam where invocation would land. That is the
wrong seam, and the correction is the substance of this record.

## Context

ADR-0016 resolved routing inside the Coder node, because that is where a generator is chosen. It
followed that invoking a specialist would replace the safe-stop there.

**Reading the graph before writing that code showed it is the wrong place**, for the same class of
reason ADR-0019 found on the other side of the same node. ADR-0019 established that the four nodes
*after* `coder` assume the built-in Python generator. This is its mirror image: **the three nodes
before it assume the same thing.**

```
codebase_reasoner.py
  if state.scenario_type == "brownfield":
      decision = interrupt(codebase_impact_review)
```

A drift-triggered modernization run is brownfield. Branching at `coder` therefore means such a run:

1. executes `codebase_reasoner`, `architecture_design` and `decomposer_planner` — three
   SDLC-shaped nodes — against a repository none of them was written for;
2. **parks at a human gate about a Python codebase-impact analysis**;
3. reaches `coder` only after someone approves that gate, and discovers there what it always was.

Step 2 is the expensive part, and not in machine time. It puts a reviewer in front of a decision
that means nothing for this run, and the platform's whole governance claim rests on gates being
decisions a human can actually make.

## Decision

### 1. The decision is made at the trigger, where the evidence already is

`handle_trigger` clones the target before starting anything, so the workspace's `origin` — the
identity routing matches on — exists from that line onward. `run_routing.plan_for_trigger` resolves
the route there and returns the initial state and the graph to run it on.

An unrouted target returns **`None`** for the factory rather than the SDLC factory by name, so a
run that is not routed anywhere reaches `runner` on exactly the path it always took.

### 2. Resuming reads the checkpoint, not the clone and not memory

A resume is routinely a different process from the one that started the run — that is what a
durable checkpointer is for — so nothing in memory can be consulted, and the workspace may be gone.

The graph is recovered from the checkpoint's own channel values: `specialist` is a field on
`SpecialistRunState` and absent from `GraphState`, so its presence *is* the discriminator. **Read
straight from the saver via `get_tuple`, not through `runner.snapshot_for`**, because that needs a
compiled graph and which graph to compile is the question being asked.

Storing the graph kind alongside the checkpoint was the alternative. It is refused: a second record
of the same fact is a second thing that can disagree with the first, and the state model already
answers it.

An unreadable or absent checkpoint routes to the SDLC graph. That is also the correct answer for
any checkpoint written before specialist runs existed.

### 3. A routing table that changed under a parked run refuses to resume

If a run parked as a specialist run and its target no longer routes to a specialist, resuming
raises rather than falling back. The run holds a design produced by one specialist; generating from
it with a different generator would substitute one for the other silently, which is the same
failure ADR-0016 refused when it forbade falling back to the built-in generator.

### 4. Specialist output does not live under `WORKSPACES_ROOT`

Reconciliation sweeps that root and deletes any directory whose run has reached a terminal state;
an output directory sitting beside the clones looks exactly like an unrecognised run to it.
`ADR-0009` records this happening once already, to the audit trail. `SPECIALIST_OUTPUT_ROOT` is a
separate mount for the same reason.

### 5. `coder.py`'s external branch stays

It should now be unreachable — routing happens before the SDLC graph starts, so an external route
cannot reach that node. It is kept as defence in depth: a guard that is unreachable *and* correct
costs nothing, and deleting it would mean a future path into that node fails by generating Python
for a target routed away from it.

## Consequences

**A specialist run now reaches the specialist.** End to end in one test: trigger, clone, route,
specialist graph, parked on the specialist's own design gate — and asserting that
`codebase_impact_review` is *not* among its gates, which is the whole of what this record fixes.

**Two outcomes are new on the run-outcome topic**: `routing_failed` (a config file that will not
load — nothing executed, and the fix is not about the target) and `routing_changed` (§ 3). They are
deliberately distinct from `run_failed`, which sends someone to look at the target.

**ADR-0016 § 5 is now partly historical.** Its reasoning about *not falling back* stands and is
strengthened; its statement about where invocation would be wired does not. Recorded here rather
than edited there, because a superseded plan is evidence about how the design moved.

**What is not verified here.** No real specialist has run through this path — the end-to-end test
drives a Python script standing in for one. Nothing is published: `generate` writes into
`SPECIALIST_OUTPUT_ROOT`, which is a local directory rather than a clone of the target project, so
**G7's open half is still open** and `card-service` is still empty. Adding `output_repository` to
the route, cloning it, and publishing through `merge_release_approval` is the next change.
