# Contributing

`CLAUDE.md` and `AGENTS.md` are addressed to AI agents. This file is the human door to the
same rules. It **points** at where each rule lives rather than restating it — this
repository already keeps its non-negotiables in `CLAUDE.md`, its decisions in `docs/adr/`
and its design in `docs/system-design.md`, and a fourth copy would be a fourth thing to
keep in sync.

## Getting a suite running

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest
```

399 tests, 22 of which skip without a reachable Postgres. `README.md` → *Testing* has the
environment for running those, and the tier table for everything else.

## The four rules that will fail your pull request

**1. A test goes in a tier directory.** `tests/{unit,contract,integration,evaluation}/`.
Its marker comes from the directory — never write one by hand. A test file anywhere else in
`tests/` is a collection error, not a warning, and CI separately asserts that the tiers
partition the suite. Which tier: does it need something running (`integration`), does it
drive the real seam end to end (`evaluation`), would breaking it break a consumer
(`contract`), or none of those (`unit`)?

**2. A decision that costs something gets an ADR.** `docs/adr/NNNN-title.md`, Context /
Decision / Consequences, numbered without gaps. The test for "does this need one" is in
`.claude/agents/design.md`: an ADR is warranted when someone could reasonably have chosen
otherwise. An ADR nobody would argue with is noise. Merged ADRs are never edited — a
reversal gets a new ADR that supersedes the old one, so the reasoning at the time stays
readable.

**3. Documentation moves in the same change as the thing it describes.** Stale docs are
bugs here, not follow-ups. `README.md` says how to run this; `docs/system-design.md` says
what the system is; ADRs say why. Keep "why" out of the README — the section order is fixed
and is asserted by `tests/contract/test_repository_structure.py`.

**4. A claim needs the command that produced it.** Not "the tiers partition the suite" —
the two collection counts. Not "it passes" — the exit code, read from `$?` rather than from
a summary line, because `pytest` can print "377 passed" and exit 1, and `cmd | tail -1`
returns `tail`'s status rather than `cmd`'s.

## Commits

Small, in order, each one honestly describable in its own message — if the message needs
"and" three times it is more than one commit. Build it, test it against something real,
commit it, **push it**. The full discipline is in `CLAUDE.md`; the reason for the push rule
is that these repositories are reviewed through GitHub, so an unpushed commit is invisible
work.

Do not add `Co-Authored-By` or "Generated with" trailers to commits or pull request bodies.

## Changing a dependency

Pins live in `requirements.txt`. After editing one, regenerate the lock:

```bash
uv pip compile requirements.txt --generate-hashes -o requirements.lock
python scripts/check_lock_is_current.py
```

CI runs that check before it installs anything. Note that `pydantic` is constrained by
`agentic-events` and cannot move on its own — see the comment in `requirements.txt`.

## What not to do

- **Do not move the package under `src/`.** It would break both container builds and
  `specialists.py`'s resolution of the routing table at `parents[1] / "config"`. The
  reasoning is in `pyproject.toml`'s header and the structure test asserts the outcome.
- **Do not touch anything under `secrets/`.** Only `.example` templates are ever tracked.
- **Do not add tenant-specific vocabulary** to `agentic_control_plane/` or `tests/`. This
  repository is domain-agnostic by requirement; CI greps for it. Tenant names belong in
  `config/`, which is data the package reads (ADR-0016).
