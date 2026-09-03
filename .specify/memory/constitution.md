# Constitution

**This file is an index, not a restatement.** Every rule below already lives somewhere in
this repository, usually next to the defect that produced it. Copying the text here would
create a second copy that goes stale silently, and this repository's own standard is that
stale documentation is a bug rather than a TODO. So each article names the rule and points
at the authority.

That is a deliberate departure from the fuller constitutions in sibling repositories. They
were written where no equivalent record existed. Here there are 28 ADRs, a system design
document, three agent role definitions and a CLAUDE.md — the gap was never the rules, only
a single place to find them from.

## The articles

**I. The control plane is domain-agnostic.** No tenant's implementation details enter
`agentic_control_plane/` or `tests/`. Tenant vocabulary is data the package reads, in
`config/`, never something the package contains.
→ [ADR-0001](../../docs/adr/0001-keep-the-control-plane-domain-agnostic.md),
[ADR-0016](../../docs/adr/0016-specialist-and-model-routing-lives-in-configuration.md); enforced by the
tenant-vocabulary step in `.github/workflows/ci.yml`.

**II. A failed run is reported, never lost.** Every terminal state produces an outcome
event, including the failures.
→ [ADR-0003](../../docs/adr/0003-a-failed-run-must-be-reported-never-lost.md),
[ADR-0007](../../docs/adr/0007-a-full-work-queue-is-backpressure-not-loss.md).

**III. The audit trail is checkable, and belongs to the run.** Appending is not enough; a
chain proves the records present and nothing about the ones that should be.
→ [ADR-0008](../../docs/adr/0008-the-audit-trail-must-be-checkable-not-merely-appended.md),
[ADR-0011](../../docs/adr/0011-the-audit-cursor-belongs-to-the-run-not-the-process.md),
[ADR-0009](../../docs/adr/0009-the-audit-trail-does-not-live-in-the-workspaces-root.md).

**IV. Parked state outlives the process that parked it.** A human gate can wait 24 hours;
crossing a restart is the ordinary case, not an edge one.
→ [ADR-0010](../../docs/adr/0010-durable-work-hand-off.md),
[ADR-0026](../../docs/adr/0026-a-runs-delivery-target-outlives-its-process.md); proved by
`tests/evaluation/`.

**V. Nothing is published before a human approves it.** `PUBLISH_MODE` defaults to `none`,
and anything else is a deliberate widening of what a compromised control plane can reach.
→ [ADR-0021](../../docs/adr/0021-nothing-commits-before-the-release-gate.md),
[ADR-0012](../../docs/adr/0012-an-approved-change-must-outlive-the-run-that-made-it.md); see
`SECURITY.md`.

**VI. A green suite is never proof.** Unit tests measure isolation; functional verification
measures the deployed thing. Neither substitutes for the other.
→ `.claude/agents/qa.md`, `tests/evaluation/REPORT.md`.

**VII. A claim needs the command that produced it, and exit codes are read from `$?`.**
A summary line is not a result — `pytest` can print "377 passed" and exit 1.
→ `CLAUDE.md`, `CONTRIBUTING.md`.

**VIII. The layout is enforced by a test, or it is a suggestion.**
→ [ADR-0028](../../docs/adr/0028-the-repository-layout-is-enforced-by-a-test.md),
`tests/contract/test_repository_structure.py`.

**IX. A decision that costs something gets an ADR, and a merged ADR is never edited.**
A reversal supersedes; it does not overwrite.
→ `CLAUDE.md`, `.claude/agents/design.md`.

**X. Documentation moves in the same change as the thing it describes.**
→ `CLAUDE.md`.
