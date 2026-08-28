# ADR-0022: A specialist-capable deployment is an override file and three grants an image cannot hold

## Status

**Accepted** (2026-08-28). Answers the revisit
[ADR-0017](0017-a-specialist-capable-image-is-a-second-image.md) § 5 asked for, and supplies the
deployment that [ADR-0021](0021-nothing-commits-before-the-release-gate.md) said "needs the
credential and the mode above plus the `claude` session and Docker daemon ADR-0017 § 3 and § 5
describe". Supersedes nothing.

## Context

Step 47 finished with the write path implemented end to end and never run against a real target.
The gap was not code. `Dockerfile.specialist` builds an image that can run a specialist; nothing in
this repository says how to *deploy* one. `docker-compose.yml` describes exactly one consumer — the
default image, `PUBLISH_MODE: none`, no session, no daemon — and every one of the four things a
specialist run needs was a sentence in an ADR rather than a line in a file.

Assembling that by hand on each operator's machine is how a deployment decision becomes folklore.

Two things found while assembling it are the reason this ADR is not just a compose file.

**The image had no specialist in it.** `agentic-sdlc-control-plane-specialist:latest` as built
carried `claude`, a JDK and Maven, and `pip list | grep cobol` was empty:
`SPECIALIST_REQUIREMENT` is optional, CI deliberately omits it (ADR-0017 § 4), and a build that
omits it prints one line to stderr and succeeds. The preflight cannot catch this — it checks the
*executables a phase declares*, and the specialist's own entrypoint is not among them, so
`require_runtime` passes and `SpecialistInvocationError` fires later, after the design gate.

**The pinned tag did not contain the fix that made the pin safe.** `v0.1.1` below replaces
`v0.1.0`, and the difference is a whole ADR in the other repository. See § 4.

## Decision

### 1. The deployment is an override of the default one, not a second stack

`docker-compose.specialist.yml`, applied on top:

```bash
docker compose -f docker-compose.yml -f docker-compose.specialist.yml up -d
```

It overrides the `consumer` service and adds nothing else. Compose merges `environment` by key and
`volumes` by container path, so the base file's Postgres wiring, secrets, healthcheck and audit
volume survive untouched, and the diff between the two deployments is exactly the four things that
differ.

**A `profiles:`-gated second service in the base file was the alternative and is refused**, for the
reason ADR-0017 refused a trailing build stage: it puts the specialist deployment's socket mount and
write-scoped credential inside the file every ordinary deployment reads, one flag away from being
active. A separate file cannot be entered by accident, and its absence from a `docker compose up`
is visible in the command rather than in a profile list.

`docker-compose.yml` therefore keeps `PUBLISH_MODE: none` and the default image. That is not
tidiness: it is ADR-0021 § 5's default surviving contact with the first deployment that overrides
it.

### 2. `CLAUDE_SESSION_DIR` is required and has no default

The image provides the `claude` binary and cannot provide the login (ADR-0017 § 3), so the session
is mounted. The interesting part is what it is mounted *from*.

`${CLAUDE_SESSION_DIR:-~/.claude}` would have been friendlier and is refused. The mount is
**read-write** — the CLI writes state as it runs — and the container it is mounted into runs a
specialist that executes generated code. Defaulting the source at the operator's live session
directory hands that directory to whatever the run produces, silently, to save typing a path. The
`:?` form fails the `docker compose` invocation with a sentence naming this ADR instead.

The documented answer is a directory holding a copy of `.credentials.json` and nothing else. `HOME`
is `/root` in this image — neither Dockerfile sets `USER` — which is why the target is
`/root/.claude` rather than a `CLAUDE_CONFIG_DIR` relocation. Fewer assumptions: the target is where
the CLI already looks.

**This grant is still unverified from inside the container**, and honestly so. ADR-0017 § 3's
warning stands unchanged: `require_runtime` passing means the binary is on `PATH`. A green preflight
is not a session.

### 3. The host socket stays mounted, and § 5's revisit is answered rather than deferred again

ADR-0017 § 5 chose a mounted host socket over Docker-in-Docker and recorded the cost — effectively
root on the host — asking that "the first deployment that actually runs the generating phase should
revisit it". This is that deployment, and the answer is **keep it, scoped**.

The reasoning that changed: this deployment already holds a write-scoped PAT for a repository it can
push to. A mounted socket does not widen a trust boundary that the credential has not already
widened, so paying for a rootless or remote daemon *here* buys defence against an attacker who has
already been handed the thing they would use the socket to get. The two grants live and die
together, which is why they live in the same file.

**The direction is unchanged and the scope is the whole of the mitigation.** A rootless or remote
daemon is still right for a deployment that runs *untrusted* tenants; this one runs a repository
its own operator owns. What this ADR adds is that the boundary is now written down at the mount
rather than in prose two documents away.

`TESTCONTAINERS_HOST_OVERRIDE` is set to `host.docker.internal` because a container talking to the
host's daemon starts siblings rather than children. **It is the documented setting and no run has
confirmed it** — ADR-0017 § 5 named precisely this ("host-path agreement for bind mounts") as
unproven, and it remains unproven. It is called out at the line rather than presented as settled.

### 4. The routing table and the image build read the same pin, and that pin is `v0.1.1`

`config/scenario_specialists.yaml`'s `distribution` and the override's `SPECIALIST_REQUIREMENT` are
the same string, in two places because one is read at runtime and one at build time. They must
agree: a routing table naming a tag the image did not install produces a failure **after** a human
has approved a design, which is the most expensive place in this graph to discover a provisioning
mistake.

The pin moved from `v0.1.0` to `v0.1.1` because `v0.1.0` was cut hours before its own repository's
PR #102 merged. That tag therefore does not contain ADR-0059 — the refusal of a `step_name` Java
cannot take, and the `solution_architect` `v1_1_0` prompt that states the rule — so a deployment
pinning it installs a specialist that emits `compute-monthly-interest` and finds out at render
time, past the gate.

**What caught it was installing the wheel, not reading the repository.** From a clean virtualenv,
`node_prompt_version('solution_architect')` reports the version the node actually sends; on
`v0.1.0` that is `v1_0_0`. A `git log` on `main` shows the fix present and says nothing about
whether the released artifact carries it.

## Consequences

**The four things step 47 could not supply now have a place to be supplied.** Two are in the file
(`PUBLISH_MODE: branch`, the socket), and two are operator-held and named at the point of use (the
write-scoped PAT in `secrets/github_pat.txt`, the session directory).

**One PAT now needs three grants, not one.** `contents: read` on the specialist's repository,
because the image `pip install`s the wheel from it at build time; `contents: read and write` on the
routed `output_repository`; and whatever the eventbus contract build already needed. The build-time
grant is easy to miss — it is not a runtime concern and it is not in ADR-0012's framing.

**A build that omits `SPECIALIST_REQUIREMENT` still succeeds, and this ADR does not fix that.** The
override always passes it, so the documented path is safe; a hand-rolled `docker build` is not.
Making the preflight check the specialist's own entrypoint would close it and belongs with the
resolver, not here.

**What is not verified.** No specialist has run. The session mount, the socket mount,
`TESTCONTAINERS_HOST_OVERRIDE`, and whether the image built from this override can reach a private
repository at build time are all reasoned rather than exercised. This ADR is the deployment; the run
that uses it is the evidence, and G7 stays open until a named program has gone through it.
