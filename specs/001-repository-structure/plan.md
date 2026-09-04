# Plan: the repository layout is deliberate and enforced

**Spec:** [spec.md](spec.md)

## Approach

Audit first and completely, with no file touched until the tree was understood; then build
in an order where each step is independently verifiable, and write the enforcing test **last**
so it encodes what was actually built rather than what was planned.

**Considered and rejected: moving the package to `src/`.** It is the reference layout in the
prompt this work came from, and it is wrong here. `src/` exists to stop tests importing the
working tree instead of the installed wheel — there is no wheel, nothing installs this
repository, and `git grep` across all four sibling repositories found one prose mention and
zero imports. It would cost both container builds and the routing-table resolution to protect
against nothing.

**Considered and rejected: a fuller `.specify/memory/constitution.md`.** The prompt's target
includes one. Here the non-negotiables already live in `CLAUDE.md`, `AGENTS.md`, three agent
definitions, 27 ADRs and `docs/system-design.md`. A sixth copy is a sixth thing to keep in
sync, against a standard that calls stale documentation a bug. The constitution is an index
that points at each authority instead.

**Corrected during the build: two premises taken from the prompt.** All five repositories
were said to be public, making Actions minutes free. Four of five are private, so minutes
come out of the account allowance — which is why CI runs one full suite rather than a job per
tier. And branch protection was assumed live; the rules API answers 403 on this plan, so
there is no ruleset and no required check.

## Risks

Each was read rather than reasoned about.

| Risk | Blast radius | Mitigation |
|---|---|---|
| Dockerfile `COPY` paths | both images | Neither has a per-directory `COPY`; both use `COPY . .`. Confirmed by reading them. No edit needed |
| Compose mounts | both stacks | Only `./secrets/*` and `/var/run/docker.sock` are repo-relative, all others named volumes. Untouched |
| `config/` resolution | every routed run | `specialists.py:179` uses `parents[1]`. `config/` stays a root sibling |
| `parents[N]` inside tests | one file | `test_specialist_artifact_gate.py` resolved `parents[1]` to the repo root; one directory deeper it resolved to `tests/`. Changed to `parents[2]` and verified against the real path |
| Mermaid check | CI | `validate-mermaid.mjs` discovers by `git ls-files '*.md'`, so moves are safe. The README diff touches no diagram block |
| Fixture shadowing | whole suite | `env`, `origin` and `workspace` are each defined twice with different bodies — so **no fixture was hoisted into the root conftest**, which would have shadowed one silently |
| CI job renames | branch protection | None renamed. There is no ruleset today, but free now is not free after the repo goes public |

## Sequence

| # | Change | Verified by |
|---|---|---|
| 0 | `.gitignore` + `.env.example` | `git check-ignore -v` |
| 1 | `pyproject.toml`; drop `pytest.ini`, `.coveragerc` | `configfile: pyproject.toml`; bare `pytest --cov` still measures the package |
| 2 | Tier directories, `git mv` 30 files | 399 collected, exit 0 |
| 3 | Extract the durability test that moved cleanly | 7 + 1 skipped = the original 8 |
| 4 | `conftest.py`, markers, `--strict-markers` | tiers partition 399; planted stray → exit 4 |
| 5 | CI tier-partition gate | step script extracted from the YAML and run, all three branches |
| 6 | `dependabot.yml`, `security.yml`, `CODEOWNERS` | YAML parses; no `pull_request` trigger on security |
| 7 | `requirements.lock` + drift gate | three directions, exit codes read from `$?` |
| 8 | Documents | section order held; no diagram touched |
| 9 | **The structure test, last** | it encodes the built tree |

## Decisions needing an ADR

One: that the layout is enforced by a test rather than described in a README —
[ADR-0028](../../docs/adr/0028-the-repository-layout-is-enforced-by-a-test.md).

The package staying at the root is not a new decision; it is the existing one, now asserted.
