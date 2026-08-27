# ADR-0018: What control-plane requires of any specialist

## Status

**Accepted** (2026-08-27). Makes concrete the invocation
[ADR-0016](0016-specialist-and-model-routing-lives-in-configuration.md) § 5 deferred, on the image
[ADR-0017](0017-a-specialist-capable-image-is-a-second-image.md) provisions. Paired with
[ADR-0019](0019-a-specialist-run-is-its-own-graph.md), which decides where the invocation runs.

## Context

ADR-0016 resolved which specialist handles a run and refused to invoke it, so that the routing
decision could be reviewed on its own. Invoking one raises a question that routing did not: **what
does this package get to assume about a program it did not write?**

Two answers are wrong in opposite directions. Assume nothing, and control-plane cannot tell a
specialist what to read, where to write, or which run it belongs to. Assume the specialist's whole
schema, and control-plane's package acquires that specialist's vocabulary — which ADR-0001 forbids,
and which would make a second specialist a second code path rather than a second config entry.

## Decision

### 1. Four flags, because they are the whole of what an orchestrator has to say

Control-plane appends `--tenant-repo`, `--output`, `--run-id` and `--json`. Everything else — the
subcommand, and any flag that names something tenant-specific — comes from
`config/scenario_specialists.yaml` and is passed through untouched.

The split is not arbitrary. Those four are exactly the things **only the caller knows**: which
clone this run is working on, where its output belongs, which run it is part of, and that a machine
rather than a person is reading. A specialist that will not accept them cannot be orchestrated at
all; one that needs anything else can be given it as configuration without a code change here.

`--run-id` is the one worth naming separately. Without it the specialist's logs and control-plane's
audit chain share no identifier, and the two halves of a run cannot be reconciled after the fact.

### 2. One JSON object on stdout, with a four-field envelope

`status`, `phase`, `run_id`, `detail`. Everything past those four is carried through as an opaque
`dict` and never inspected. PR #9 established that the durable checkpointer round-trips an
arbitrary dict unchanged across two independent saver contexts, which is what lets a specialist's
artifact cross a human gate without this package modelling it.

So a specialist reporting five fields and one reporting twelve both work, and neither needs a
change in `agentic_control_plane/`. That is the property that keeps ADR-0001 true while real
specialists exist.

### 3. Parsing is strict, unlike the built-in generator's

`nodes/coder.py` parses the `claude` CLI leniently — strict JSON first, then the largest
brace-delimited substring — because that CLI genuinely narrates around its answer and cannot be
told not to.

A specialist is different: it is held to this contract. Unparseable stdout means a broken
specialist, and reporting that is more useful than salvaging a substring and continuing on a guess.
The failure names the command, the exit code and what stderr said.

**A `status: "ok"` payload with a non-zero exit code is reported rather than reconciled.** One of
the two is wrong, guessing which would hide a real defect in whichever specialist does it, and the
place to fix it is that specialist.

### 4. Every failure raises, so one safe-stop covers all of them

Not installed, hung, unintelligible, or honestly reporting its own failure — all four raise, and
the caller's existing safe-stop ends the run in a defined terminal state with a stated reason. A
result object carrying a failure would invite a caller to forget to check it.

Timeouts are **per phase** and configured, because phases are not comparable: one that asks a model
for a document and one that drives a compiler through a repair loop differ by an order of
magnitude, and a single number is either too tight for the second or useless for the first.

## Consequences

**A second specialist is a config entry.** The contract is four flags and four fields; nothing in
this package names a tenant, and the CI vocabulary guard still passes with two tenants routed.

**The contract is a requirement on specialists, and one already meets it.** That is worth stating
as a limit rather than a triumph: it was checked against exactly one implementation, so "any
specialist" means "any specialist that agrees to this", and the second one is where the assumption
gets tested. `--run-id` is the precedent — it was accepted by one subcommand and not the other for
a whole milestone, on the noisier half, and nothing noticed until someone tried to correlate a run.

**What is not verified here.** No real specialist has been invoked through this path. Every test
runs a Python script written into `tmp_path` — a real subprocess, real argv, real stdout, real exit
codes, real timeouts, but not the real specialist, whose generating phase needs a JDK, Maven and a
Docker daemon. That is ADR-0017's image, and exercising it end to end is the step after this one.
