# Security

## Reporting a vulnerability

Report privately, not in a public issue. Use **Security → Report a vulnerability** on this
repository to open a private advisory. If that is unavailable to you, contact the maintainer
through their GitHub profile (`@jayakumar-devaraj`) and ask for a private channel before
sending details.

Please include what you were able to do, not only what you think is wrong — a reproduction
against a real deployment is worth more here than a static observation, for the same reason
this repository keeps a functional verification report alongside its coverage number.

## What this service can reach, and why that matters

This is the component of the platform with the widest blast radius, so the threat model is
worth stating plainly rather than leaving to be inferred.

| Capability | Where it comes from | Bounded by |
|---|---|---|
| Clones tenant repositories over HTTPS | a runtime PAT read from `GIT_PAT_FILE` through a credential helper at request time | read-only by default; `PUBLISH_MODE` other than `none` requires a write-scoped token, which ADR-0012 calls a deliberate widening |
| Executes code it did not write | the generating phase runs a real test subprocess against a cloned workspace | the workspace is ephemeral and per-run; guardrail scanning runs at the release gate |
| Mounts the host Docker socket | only in the specialist deployment (`docker-compose.specialist.yml`) | **effectively root on the host.** ADR-0017 recorded the cost rather than settling it, and ADR-0022 revisits it. Do not use that override on a machine you would not hand over |
| Holds an authenticated `claude` session | `CLAUDE_SESSION_DIR`, mounted read-write | must be a *copy* holding `.credentials.json` and nothing else. Pointing it at a live `~/.claude` hands that directory to whatever a run produces |

The default deployment — `docker compose up -d`, no override — has none of the last two and
publishes nothing.

## How credentials are kept out of the repository and the images

These are the controls, each with the thing that enforces it:

- `.gitignore` excludes `secrets/*.txt`, `secrets/claude-session/`, `.env` and
  `docker-compose.override.yml`. Only `.example` templates are tracked.
- Build credentials are BuildKit secrets, mounted for one `RUN` and never written to a
  layer. The `ci` workflow's *"Assert the built image carries no credential"* step reads
  `/root/.gitconfig`'s size in both images and is kept as a regression guard even though it
  passes trivially today.
- The runtime PAT never becomes a layer or an environment variable — it is read from a file
  at request time.
- Error paths do not echo credentials: `publish.py` is tested for the case where a GitHub
  error body could carry the token onto an outbound event.

## Known gaps, stated rather than implied

- **No code scanning and no native secret scanning.** Both require GitHub Advanced Security
  on a private repository, which this account does not have. `.github/workflows/security.yml`
  records this and runs `pip-audit` weekly instead.
- **No branch protection.** Rulesets are unavailable on a private repository on this plan —
  the API answers 403. Nothing currently prevents a red merge; review is the only gate.
- **`TESTCONTAINERS_HOST_OVERRIDE` in the specialist override is unverified.** ADR-0017
  named it as an open question and it is still open. It is set because it is the documented
  answer, not because a run confirmed it.

## Supported versions

This is an application deployed from `main`, not a distributed package. There are no
released versions to support and no backports — fixes land on `main`.
