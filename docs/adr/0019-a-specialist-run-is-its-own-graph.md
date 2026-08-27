# ADR-0019: A specialist run is its own graph

## Status

**Accepted** (2026-08-27). Decides the architecture question the platform audit has carried open
since R2.4 as *"where the governance primitives live"* — the one its § 5 says to settle **before**
a specialist node is written, warning that *"deciding by default means Shape B gets absorbed into a
repo shaped around Shape A."* This is that decision, made deliberately, and it goes the other way
from the sizing's own expectation.

## Context

The audit's *"G20, sized"* block (R2.51) measured two shapes for invoking a specialist:

| | |
|---|---|
| **A** | a node inside the existing graph, inheriting `GraphState` |
| **B** | a separate graph reusing the checkpointer, runner and gate primitives |

It sized A as cheaper — *"a field on `GraphState`, a 1-line 6th `GateType`, and the node itself. No
extraction."* It also said, precisely, what would settle the question: *"The counts are grep-level
and would need a real attempt to confirm that the four `runner.py` lines are the only things that
resist."*

**This was that attempt, and something else resists.** Not the gates, and not the durability
machinery — both of those are as cheap as the sizing said. **It is the four nodes after `coder`.**

The SDLC graph's post-generation half assumes the built-in Python generator:

- `test_executor` runs `python -m pytest` in the run's workspace;
- `evaluate_guardrails` matches Python patterns against generated text;
- `release_gate` commits **that workspace**.

A specialist writes its output to a different repository entirely (ADR-0009) and has already run
its own compile-and-heal loop. Measured against a workspace shaped like a specialist's:

```
pytest failed in ...\javaws: returncode=5
passed: False        ← "no tests ran"
```

`passed=False` sets `route_hint="retry"`, and `_route_after_sync` sends the run back to `coder`.
For a specialist that means **re-invoking it up to four times** — three retries then a fallback,
each one a model pass and a real Maven build — before rolling back a run that had already
succeeded. The release gate would then commit the read-only tenant checkout rather than the
project the code was written into.

Shape A does not avoid this by being small. It relocates it: every one of those nodes grows an
"unless this is a specialist run" branch, and the graph carries two shapes that share a state model
in which roughly sixteen of thirty fields are permanently empty for one of them.

## Decision

### 1. Specialist runs execute a separate graph

```
START → design → (gate: specialist_design_review) → generate → END
                      │
                      └── rejected → END
```

`agentic_control_plane/specialist_graph.py`, over `SpecialistRunState` — the smallest state that
carries a two-phase hand-off, deliberately not `GraphState`.

### 2. What is reused is everything that made the first graph durable

The same `interrupt()`, the same `PostgresSaver`, the same `runner`, the same `AuditEvent` /
`GateRecord` / `RunStatus` vocabulary. This is not a second orchestrator; it is a second topology in
the same host.

The sizing's two coupling counts were checked against the real code and **both held**:

| | Sized | Found |
|---|---|---|
| `runner.py` | 4 lines: 2 imports, one `build_graph(...)`, one `initial_state: GraphState` | exactly those |
| `checkpointer.py` | 1 function introspecting `state.py` | exactly that |

`runner` gains an optional graph factory defaulting to the SDLC graph, so every existing caller is
unchanged. Its terminal-state reading needed **nothing**: it classifies on `safe_stop` and
`run_status`, which are lifecycle rather than workflow, and the new state carries both under those
names on purpose. The serde now discovers models from both state modules, because one saver serves
both graphs and a run parked by either must be readable by either.

### 3. The gate is a sixth `GateType`, in the shared vocabulary

`specialist_design_review`, one line, breaking no test — as the sizing predicted. It lives in
`state.py` beside the other five rather than in the new module, because a decision published on the
gate-decision topic must name its gate the same way whichever graph raised it. One consumer, one
vocabulary.

### 4. The gate sits before generation, not after

A design is what a reviewer can actually read, and generation is the expensive half. The specialist
deliberately does not make this call — its own ADR-0008 reports what it found and leaves *whether
that warrants a pause* to control-plane. This is control-plane exercising that.

A rejected design ends the run **completed**, not failed: the governance step did exactly its job.

### 5. `GraphState` is not split here

The audit's item 2 — a generic run/gate/audit base with the SDLC fields as a subclass, bounded at
one 189-line file and seventeen test files — remains the tidier end state and remains unattempted.
Nothing in this change needs it, and putting a thirty-field refactor in the same diff as a new
execution path would make both harder to review.

## Consequences

**The audit's open architecture question is closed, against its own sizing's expectation**, and the
reason is evidence that did not exist when the sizing was written. Item 1 of that sizing — the
gate/runner/checkpointer primitives — was as near-free as it said. Item 2 stays open. Item 3, a
non-drift ingress, is untouched.

**The SDLC graph is unchanged.** No node gained a branch, no test was modified, and the existing
suite passes as it stood: **344 passed, 93.74%**, up from 310 at 93.13%.

**Two graphs now have to be kept honest about one thing**: the gate vocabulary. A seventh gate type
added for one graph is visible to the other's `GateRecord`, which is the cost of the single
vocabulary in § 3 and is preferred to two.

**What is not verified here.** No real specialist has run through this graph — every test uses a
Python script in `tmp_path`, which is a real subprocess but not a real JDK, Maven or Docker daemon.
Nor does anything yet *route* a run to this graph: the consumer still builds only the SDLC one, and
`nodes/coder.py` still safe-stops a routed target rather than handing it over. That hand-off is the
next change, and keeping it out of this one means the topology can be rejected on its own terms.
