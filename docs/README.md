# Documentation

| Document | Purpose | Audience |
|---|---|---|
| [executive-summary.md](executive-summary.md) | Two-page orientation: what the platform does, what was delivered, what is proven, what it does not do. **Start here.** | Anyone, technical or not |
| [system-design.md](system-design.md) | Authoritative design for the whole platform: context, components, event contract, control-plane internals, reliability, security, operations, verification. | Anyone needing to understand how the system works |
| [adr/](adr/) | One record per significant decision, in Context / Decision / Consequences form. | Anyone asking why something is the way it is |
| [ai-engineering-practice.md](ai-engineering-practice.md) | How this platform was built with AI assistance, and the evidence for what caught the mistakes. | Anyone assessing the engineering process rather than the system |
| [../README.md](../README.md) | How to run and use this service. | Operators and contributors |

## The split, and why it holds

The README says **how to run this**. The design document says **what the system is**. The ADRs say
**why it is that way**.

Keeping "why" out of the README is deliberate. Rationale written inline tends to accumulate as a
narrative of bugs found, which makes the operational instructions harder to use and buries the
reasoning where nobody looks for it. An ADR is a stable address for a decision.

## Architecture decision records

| ADR | Subject |
|---|---|
| [0001](adr/0001-keep-the-control-plane-domain-agnostic.md) | Keep the control plane domain-agnostic |
| [0002](adr/0002-map-decision-identity-to-state-provenance.md) | Map a decision's identity to state provenance at the boundary |
| [0003](adr/0003-a-failed-run-must-be-reported-never-lost.md) | A failed run must be reported, never lost |
| [0004](adr/0004-the-poll-loop-tolerates-transient-client-errors.md) | The poll loop tolerates transient client errors |
| [0005](adr/0005-single-threaded-worker-and-when-offsets-commit.md) | A single-threaded worker, and when offsets commit |
| [0006](adr/0006-configure-the-committer-identity-on-a-cloned-workspace.md) | Configure the committer identity on a cloned workspace |
| [0007](adr/0007-a-full-work-queue-is-backpressure-not-loss.md) | A full work queue is backpressure, not loss |
| [0008](adr/0008-the-audit-trail-must-be-checkable-not-merely-appended.md) | The audit trail must be checkable, not merely appended |
| [0009](adr/0009-the-audit-trail-does-not-live-in-the-workspaces-root.md) | The audit trail does not live in the workspaces root |
| [0010](adr/0010-durable-work-hand-off.md) | Durable work hand-off |
| [0011](adr/0011-the-audit-cursor-belongs-to-the-run-not-the-process.md) | The audit cursor belongs to the run, not the process |
| [0012](adr/0012-an-approved-change-must-outlive-the-run-that-made-it.md) | An approved change must outlive the run that made it |
| [0013](adr/0013-liveness-is-the-workers-signal-not-the-process.md) | 0013 — Liveness is the worker's signal, not the process's |
| [0014](adr/0014-stage-then-unstage-rather-than-exclude.md) | 0014 — Stage then unstage, rather than exclude in one `git add` |
| [0015](adr/0015-the-build-works-whether-the-contract-repo-is-public-or-private.md) | The build works whether the contract repo is public or private |
| [0016](adr/0016-specialist-and-model-routing-lives-in-configuration.md) | Specialist and model routing lives in configuration, not in the package |
| [0017](adr/0017-a-specialist-capable-image-is-a-second-image.md) | A specialist-capable deployment is a second image, not a bigger default one |
| [0018](adr/0018-what-control-plane-requires-of-any-specialist.md) | What control-plane requires of any specialist |
| [0019](adr/0019-a-specialist-run-is-its-own-graph.md) | A specialist run is its own graph |
| [0020](adr/0020-a-run-is-routed-to-its-graph-before-it-starts.md) | A run is routed to its graph before it starts |
| [0021](adr/0021-nothing-commits-before-the-release-gate.md) | Nothing commits before the release gate, so nothing needs rolling back |
| [0022](adr/0022-a-specialist-capable-deployment-is-an-override-and-three-grants.md) | A specialist-capable deployment is an override file and three grants an image cannot hold |
| [0023](adr/0023-which-programs-a-run-migrates-is-deployment-data.md) | Which programs a run migrates is deployment data, not run data |
| [0024](adr/0024-a-read-only-guarantee-a-credential-enforces-is-not-enforced.md) | Two graphs deliver, and only one of them delivers from the consumer |
| [0025](adr/0025-the-probe-asks-the-daemon-and-the-graph-asks-the-probe.md) | The probe asks the daemon, and the specialist graph finally asks the probe |
| [0026](adr/0026-a-runs-delivery-target-outlives-its-process.md) | A run's delivery target outlives its process |
| [0027](adr/0027-the-specialist-subprocess-is-traced-before-this-service-is.md) | The specialist subprocess is traced before this service is |
| [0028](adr/0028-the-repository-layout-is-enforced-by-a-test.md) | The repository layout is enforced by a test |

New ADRs are numbered sequentially and never edited once merged. A decision that is later reversed
gets a new ADR that supersedes the old one, so the reasoning at the time stays legible.
