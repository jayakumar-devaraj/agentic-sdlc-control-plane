# Tasks: the repository layout is deliberate and enforced

**Spec:** [spec.md](spec.md) · **Plan:** [plan.md](plan.md)

One row per commit, each pushed as it landed. **The evidence column is a log: each row records what was true at that commit**, so the collection counts in rows 1-5 are the pre-structure-test 399 rather than the final 443. That is the point of a log. Evidence is the command's **exit code read
from `$?`**, never a summary line — `pytest` can print "377 passed" and exit 1, and
`cmd | tail -1` returns `tail`'s status rather than `cmd`'s.

| # | Task | Commit | Evidence | Done |
|---|---|---|---|---|
| 0 | Ignore `.env` and `docker-compose.override.yml`; add `.env.example` | `fa107e3` | `git check-ignore -v` resolves both to the new rules; `.env.example` still tracked; its three variable names match what the two compose files read, exactly | ☑ |
| 1 | Fold `pytest.ini` + `.coveragerc` into `pyproject.toml` | `0a01f05` | pytest reports `configfile: pyproject.toml`, `testpaths: tests`; a bare `pytest --cov` with no argument still measures the package, which is what proves the coverage source transferred; 399 / 377 passed / 91%, exit 0 | ☑ |
| 2 | `git mv` 30 test files into four tiers | `c4e3183` | 399 collected (unchanged), 377 passed / 22 skipped, 91%, exit 0. `test_specialist_artifact_gate.py`'s `parents[1]` → `parents[2]`, verified to resolve back to the real `secrets/` | ☑ |
| 3 | Extract the checkpointer durability test to `evaluation/` | `379ceff` | the two files together report 7 passed / 1 skipped — exactly the 8 the single file had. Three orphaned imports removed | ☑ |
| 4 | `conftest.py` derives tier markers; `--strict-markers` | `b34ceec` | tiers partition exactly: 262 + 62 + 15 + 60 = 399, so no test is in zero tiers or two. Planted `tests/test_a_deliberate_stray.py` → `pytest -m unit` exit **4** naming the file, where before it would have been the 400th test silently deselected; removed → exit 0 | ☑ |
| 5 | CI gates the tier partition; one full run, not a job per tier | `b944a25` | step script extracted from the workflow YAML and run verbatim, all three branches: clean 399/399 exit 0; a tier dropped from the marker expression exit 1 naming 262 orphans; a planted stray exit 1 on the collection-error branch rather than a vacuous 0 | ☑ |
| 6 | `dependabot.yml`, `security.yml`, `CODEOWNERS` | `661b2c2` | both YAML files parse; `security.yml` has no `pull_request` trigger, so its check can never be made required; the `agentic-events` strip leaves exactly the eight remaining pins | ☑ |
| 7 | `requirements.lock` + `scripts/check_lock_is_current.py` | `1fea584` | clean → 8 pins agree, exit 0; a pin edited to a version the lock lacks → exit 1 naming it; a pin added without regenerating → exit 1. The lock resolved the mutable `v0.1.0` tag to commit `6989319` | ☑ |
| 8a | Three stale doc claims; verification report → `tests/evaluation/REPORT.md` | `0dd870f` | README section order held (Tech stack → Architecture → Quickstart → Local development → Testing → Deployment/CI); the diff touches no mermaid block, so the diagram check cannot regress from it; ADR index regenerated from the files, 27 rows | ☑ |
| 8b | `CONTRIBUTING.md`, `SECURITY.md` | `61417e9` | every control credited was checked against the code: the publish token test exists at `tests/contract/test_publish.py:268`, and the release gate does call `evaluate_guardrails` | ☑ |
| 8c | `.specify/`, `specs/001`, ADR-0028 | `7290e36`, `8f5ecf9` | constitution is an index of ten articles, each pointing at an existing authority rather than restating it | ☑ |
| 9 | `tests/contract/test_repository_structure.py` | `64ff341` | 44 assertions pass on the real tree, and five deliberate violations each fail the assertion that owns them: a `src/` directory, an unindexed ADR, a sixth Postgres-gated unit test, a reordered README, a `[project]` table. Two bugs in the test were caught by running it — a line-by-line Postgres counter that missed a multi-line decorator, and a secrets check reading the filesystem instead of git. Final suite 443 collected, 421 passed / 22 skipped, 91%, exit 0 | ☑ |

## Left undone

**Five of the six Postgres-gated tests stayed in `tests/unit/`** — four in `test_consumer.py`,
one in `test_runner.py`. Extracting them needs the files split rather than moved, which
changes what tests do, not where they live. The exception is stated in `README.md` and the
structure test pins the count at five so it cannot quietly grow.

**ADR-0024's filename does not match its own title.** Found, reported, left: this repository
never edits a merged ADR, and renaming would break inbound links.

**No branch protection was configured**, because none can be: rulesets are unavailable on a
private repository on this plan. Nothing prevents a red merge today. Recorded in
`SECURITY.md` as a known gap rather than left to be discovered.

**Declarative extraction of the state machines, routing, prompts and tool schemas** was
noted in the audit and deliberately not started. It genuinely applies to this repository and
is its own engagement.
