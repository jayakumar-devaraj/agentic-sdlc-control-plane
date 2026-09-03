# 0028 - The repository layout is enforced by a test

## Context

This repository was in good order by every measure that had a check behind it: 27 ADRs, 91%
coverage, a functional verification report covering things no unit test can reach, a CI job
asserting that no tenant vocabulary has leaked into a domain-agnostic package.

The layout had no check behind it at all.

Thirty test files sat flat in `tests/`. Two of them — `test_inbox.py` and
`test_run_targets.py` — were gated at module level on a reachable Postgres, which makes them
integration tests wearing a unit test's filename. `test_graph_integration.py` drives the
compiled graph from the real calling position. Nothing in the tree said so, and nothing
could have said so: a directory named `tests/` carries no claim that anything can contradict.

That is survivable while a repository has one contributor holding the whole tree in their
head. It stops being survivable at the first change made by someone who was not there, because
a tree that is *mostly* the documented one gives no way to tell which parts were deliberate.

Two specific failures make the general argument concrete.

**A missing tier marker is invisible.** CI selects tests by marker. A test carrying no marker
is collected, counted in the "passed" total, and never executed by the marker-filtered
command that was supposed to run it. `--strict-markers` does not catch this — it catches a
*misspelled* marker, not a missing one. In `agentic-sdlc-eventbus` this silently deselected 18
tests and left a module at 69% coverage while the run printed "99 passed". This repository ran
a bare `pytest` with no `-m` filter, so it had not been bitten yet; it was one flag away.

**A README is not a check.** This repository's own README asserted "257 tests" in two places
while the suite collected 399, and asserted that CI "runs on every push and pull request to
`main`" while the workflow has no push trigger and carries a twenty-line comment arguing
against one. Both claims had been false for some time and neither cost anything, because
prose cannot fail.

## Decision

**The layout is asserted by `tests/contract/test_repository_structure.py`, and it lives in the
contract tier because breaking the layout breaks a consumer — the next contributor.**

The test asserts **shape, never content**. It does not care what an ADR argues, only that
decisions get recorded as numbered ADRs without gaps. It does not read a spec, only that a
spec directory carries the sections the workflow depends on.

Three assertions are specific to this repository rather than inherited:

1. **The package is at the root, and is not also under `src/`.** The reference layout this
   work came from puts it under `src/`. That is right where something installs the package and
   wrong here: nothing installs this one, both Dockerfiles rely on `WORKDIR /app` with
   `COPY . .`, and `specialists.py` resolves the routing table at
   `Path(__file__).resolve().parents[1] / "config"`. A `src/` move breaks all three to protect
   against a wheel that does not exist.

2. **`config/` is a sibling of the package, not inside it.** The inverse of the eventbus rule,
   for the inverse reason. There, contract artifacts must be *inside* the package or they do
   not ship in the wheel. Here, tenant vocabulary must be *outside* it, because ADR-0001 and
   ADR-0016 make that vocabulary data the package reads rather than something the package
   contains — and CI's tenant-vocabulary grep scopes itself around exactly that boundary.

3. **Exactly five Postgres-gated tests remain in `tests/unit/`.** A bounded, documented
   exception rather than a silent one: separating them requires splitting modules rather than
   moving files. Pinning the number means the exception cannot quietly grow — a sixth fails
   the build and has to argue for itself.

The test is written **last**, after the layout it describes, so it encodes what was built
rather than what was planned.

## Consequences

**A layout change now either satisfies a stated rule or has to argue with a specific reason.**
That is the whole benefit, and it is the reason to prefer a test over a document: the argument
happens at the point of change instead of six months later in review.

**The test will be wrong eventually, and that is intended.** When this repository grows a
distributable, or a fifth tier, or a second spec, the assertion that contradicts the new
reality is a prompt to make the decision explicitly. A failing structure test is a question,
not a defect.

**It cannot check the two things that matter most.** It asserts that ADRs are numbered without
gaps, not that they are true; that a spec has a `tasks.md`, not that the tasks were done. It
also cannot check itself: ADR-0024's filename slug does not match its own title, and no
shape-based rule would catch that.

**There is no branch protection to enforce any of this.** Rulesets are unavailable on a private
repository on this plan — the API answers 403 — so nothing prevents a red merge. The test
fails the build; a human still has to decline to merge it. Recorded in `SECURITY.md` as a known
gap rather than left to be discovered.
