# Spec: the repository layout is deliberate and enforced

**Status:** Built
**Number:** 001

## Problem

Observed by audit on 2026-09-03, against a repository that was otherwise in good order — 118
files, 126 commits, 27 ADRs, 91% coverage, a functional verification report.

1. **Thirty test files sat flat in `tests/`.** Nothing in the layout distinguished a test
   that mocks everything from one needing a real Postgres, or from one driving the compiled
   graph end to end. The only markers in the suite were four `parametrize` and five
   `skipif` — no tier markers at all, and no `conftest.py`. CI ran a bare `pytest`, so the
   silent-deselection failure could not fire *yet*; it was one `-m` flag away.
2. **`.env` was not gitignored** while `docker-compose.specialist.yml` reads
   `OTEL_EXPORTER_OTLP_HEADERS` — a credential — from it, and Compose merges `.env`
   automatically.
3. **No dependency-update or CVE signal**, on the service that mounts a write-scoped PAT and
   the host Docker socket and executes code it did not write.
4. **Nothing enforced the layout**, so no future contributor could tell which parts of the
   tree were deliberate.
5. **Three documentation claims were false**: "257 tests" (399), "runs on every push and
   pull request" (there is no push trigger, and the workflow argues against one at length),
   and an ADR index listing 10 of 27.

## Scope

Layout, tooling and gates, documents, and the test that holds them.

**Not in scope.** Features, application logic, performance, and the declarative extraction
of the state machines, routing, prompts and tool schemas in `graph.py`,
`specialist_graph.py` and the nine nodes — that genuinely applies here and is a separate
engagement. Bugs found were written down and left.

## Success criteria

| # | Criterion | Verified by |
|---|---|---|
| 1 | Suite unchanged after every move | 399 collected, 377 passed / 22 skipped, 91% — identical before and after; `echo $?` = 0 |
| 2 | The four tiers partition the suite exactly | 262 + 62 + 15 + 60 = 399 |
| 3 | A test outside a tier is a hard error, not a silent skip | planted stray file → `pytest -m unit` exit 4 naming it; removed → exit 0 |
| 4 | `.env` cannot be committed | `git check-ignore -v .env` resolves to the new rule |
| 5 | Lock drift fails the build | pin edited → exit 1; pin added → exit 1; clean → exit 0 |
| 6 | The layout is asserted, not described | `tests/contract/test_repository_structure.py` |

## Constraints

- **The package stays at the repository root.** A `src/` move breaks both Dockerfiles
  (`WORKDIR /app`, `COPY . .`, `python -m agentic_control_plane.main`) and
  `specialists.py:179`, which resolves the routing table at `parents[1] / "config"`.
- **No Dockerfile or compose file is edited.**
- **Nothing under `secrets/` is moved, renamed or committed.**
- `pythonpath = .` must survive the `pytest.ini` → `pyproject.toml` fold.

## Open questions

**Answered during the build:** whether the six Postgres-gated tests inside otherwise-unit
modules could be extracted. One could, cleanly. Five could not without splitting files,
which changes what tests do rather than where they live — so they stayed, the exception is
documented in `README.md`, and the structure test pins the count so it cannot grow.

**Still open:** ADR-0024's filename slug does not match its own title. Renaming breaks
inbound links and this repository never edits a merged ADR. Recorded, not fixed.
