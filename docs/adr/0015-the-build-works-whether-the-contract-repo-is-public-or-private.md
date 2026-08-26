# ADR-0015: The build works whether the contract repo is public or private

## Status

**Accepted** (2026-08-26). Reverses the mechanism removed in commit `080ffae` — *"Drop the
build-time PAT and correct the claims that assumed private repos"* — while **preserving that
commit's actual security concern**, which was about the image rather than about credentials in
general.

## Context

`requirements.txt` pins `agentic-events` to a git URL on `agentic-sdlc-eventbus`. On 2026-07-31,
`080ffae` removed the build-time PAT with this reasoning:

> `agentic-sdlc-eventbus` is public, so `agentic-events` resolves over anonymous HTTPS and no CI job
> needs a secret.

**That premise does not hold.** Repository visibility on this platform gets toggled — for demos, for
sharing, for reasons unrelated to CI. On 2026-08-26 `eventbus` was private and every build in this
repo failed at dependency install with:

```
fatal: could not read Username for 'https://github.com': No such device or address
```

CI had been red since **2026-08-11**, the last successful `ci` run. Nothing exercised it in between,
so the breakage sat unnoticed until unrelated work needed a green build.

**Two things made the diagnosis worse than the outage.** The message names neither the repository nor
the setting. And the pinned URL still carries the pre-rename owner `jayakumard10`, which looks
exactly like the cause — it is not; GitHub's rename redirect resolves it correctly, and only the
*auth* fails. The first diagnosis attempted here was the wrong one for that reason.

## Decision

### 1. Both paths work, and neither requires editing a file

| `agentic-sdlc-eventbus` | how `agentic-events` resolves |
|---|---|
| public | anonymous HTTPS, no credential anywhere |
| private | `EVENTBUS_READ_PAT` — fine-grained, **read-only**, scoped to that one repository |

**The secret is optional at every layer.** The CI step configures git only when the value is
non-empty; the Dockerfile tests `-s /run/secrets/github_pat` and skips the git config on an empty
file. A fork with no secret still gets a green build while eventbus is public, which is the property
`080ffae` valued and this record keeps.

### 2. The empty-secret trap is handled explicitly, because it is silent

Interpolating an unset GitHub secret yields the **empty string**, and
`git config url."https://@github.com/".insteadOf` is a *syntactically valid* rewrite carrying an
empty credential. The clone then falls through to an interactive prompt and dies with
`could not read Password` on a runner with no TTY — a message pointing at nothing.

So the empty case must **skip the configuration entirely** rather than write it. Same reason the CI
job writes an *empty* `secrets/github_pat.txt` rather than a placeholder string when no secret is
set: a placeholder is non-empty, so the Dockerfile would configure git with a credential that cannot
authenticate, converting a working anonymous build into a failing authenticated one.

### 3. `080ffae`'s security concern is preserved, not overruled

That commit's real argument was never "credentials are bad". It was:

> this is the image that handles a real PAT at runtime, so a build step that reintroduced one would
> ship it

Still true, and still respected. The build credential is a **BuildKit secret**: it exists only under
`/run/secrets` for the life of one `RUN`, never becomes a layer, and the git config it creates is
removed inside that same layer. `080ffae` deliberately **kept** the `Assert the built image carries
no credential` step after removing the mechanism, saying it was "a regression guard" for exactly
this. That guard now has something to guard again, which is the outcome it was left for.

### 4. CI names the cause before pip can obscure it

A pre-flight `git ls-remote` runs after credentials are configured and before install. It
distinguishes the two failures that matter — *the repository is private and no secret is set* versus
*the secret is set but wrong or expired* — because those have different fixes and pip's single error
message covers both.

## Consequences

**A repository setting can no longer silently break the build**, and when something related does
break, the failure names the repository, the secret and both remedies in about a second rather than
in nine minutes of pip output.

**This adds an optional repository secret**, which `080ffae` counted as a cost worth removing. The
answer to that is the outage it was traded for: a build that only works while a setting nobody is
tracking stays in one position is not a working build. The secret being *optional* keeps the fork
story `080ffae` wanted.

**What is not verified here.** The authenticated path has **not** been exercised — no
`EVENTBUS_READ_PAT` exists yet, and creating one is not something this change can do. What is
verified is the anonymous path and the plumbing around it: the workflow and compose files parse, the
pre-flight correctly reports the current private repository as unreachable, and no credential value
appears anywhere in the repository. **Until the secret is set, or eventbus is made public again,
CI stays red — correctly, because something true is wrong.**

**PyPI remains the better end state and is deliberately not taken here.** Publishing `agentic-events`
as a public package would make visibility irrelevant in every consumer, forever, with no secret at
all. It is a release-process change of roughly half a day and it would block Track P1, which is what
is actually waiting on a green build.
