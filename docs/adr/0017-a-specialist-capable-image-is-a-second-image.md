# ADR-0017: A specialist-capable deployment is a second image, not a bigger default one

## Status

**Accepted** (2026-08-27). Builds the customised image
[ADR-0016](0016-specialist-and-model-routing-lives-in-configuration.md) § 4 named without building.
Follows the posture `nodes/coder.py` set for the `claude` CLI. Supersedes nothing.

## Context

ADR-0016 made routing configurable and then refused to invoke anything, on the grounds that the
shipped image cannot run a specialist:

> The shipped image is `python:3.12-slim` and carries no specialist runtime; a specialist-capable
> deployment is a customised image, not a config toggle.

That is a true statement about a thing that did not exist. The routing table declares, per phase,
what an external specialist needs — the `claude` CLI for either subcommand, plus a JDK, Maven and a
Docker daemon for the generating one — and `specialists.require_runtime` reports what is absent by
name. On every image this repository has published, the answer is *all of it*.

## Decision

### 1. A second image, in its own file, based on the first

`Dockerfile.specialist` starts `FROM ${BASE_IMAGE}` and adds the runtime. The default image is
untouched.

**A trailing stage in the existing `Dockerfile` was the obvious alternative and is refused.** The
last stage of a Dockerfile is the default target, so adding one silently redirects every bare
`docker build .` — compose, CI, every developer — to the much larger image. Making that safe means
`--target` appearing at every call site, and the first one anybody forgets is a several-hundred-
megabyte surprise. A separate file cannot do that by accident.

**Why not put the runtime in the default image and be done.** Because most runs do not need it. The
routing table's whole point is that a target resolves to the built-in generator unless something
routes it elsewhere, and paying for a JDK, Maven, Node and the `claude` CLI on every deployment to
serve the exception is the wrong default. It also keeps the security surface of the ordinary
deployment where it is.

### 2. Versions are pinned to what the specialist's baseline pins

Temurin **25.0.4+7** and Maven **3.9.16**, taken from the generated project's own `pom.xml`
(`<java.version>25</java.version>`, `maven.compiler.release`) and from its Maven wrapper's
`distributionUrl`. Not "latest", and not Debian's `default-jdk` — which is 17 here, and which
Debian's `maven` package drags in, putting an older JDK on `PATH` beside the pinned one.

The generated project asserts its JDK's feature version in a test. An image that disagrees with the
baseline produces a failure that the heal loop will attribute to generated code, which is the
expensive kind of wrong answer.

### 3. The image provides the `claude` binary and cannot provide the session

`claude -p` needs an authenticated login. That is per-operator and must never be baked into an
image, so it is mounted at runtime. The consequence is worth stating plainly because it is easy to
misread the preflight: **`require_runtime` passing on this image means the CLI is on `PATH`, not
that a call will succeed.** The existing `_require_claude_cli` has always had this property; this
ADR does not change it, and does not pretend the image closes it.

The CLI is installed from the npm registry rather than by piping a shell script from the network
into `bash`.

### 4. The specialist is a build argument, not a hardcoded pin

`SPECIALIST_REQUIREMENT` takes the whole `pip` requirement ADR-0055 specified — the package name and
the git ref. Moving to a newer tag is then a build flag rather than an edit, and an image built
without it carries the runtime but no specialist, which is a legitimate thing to want when testing
the runtime itself.

The build-time credential follows the existing `Dockerfile` exactly (ADR-0015): a BuildKit secret
that never becomes a layer, with the git config removed inside the same `RUN`.

### 5. The Docker daemon is mounted, not nested

The generating phase needs a daemon because the baseline's own test uses Testcontainers. This image
does not run one. Docker-in-Docker needs `--privileged`, which is a large grant for a process that
clones arbitrary repositories and runs generated code; mounting the host's socket reuses a daemon
that already exists.

**Both options are worse than they look and this one is chosen with its cost stated.** A mounted
socket is effectively root on the host, so this belongs to a deployment that already trusts the
control plane with a write-scoped PAT, not to a developer laptop running an untrusted tenant. It is
recorded here rather than settled: the first deployment that actually runs the generating phase
should revisit it, and a rootless or remote daemon is the direction.

## Consequences

**ADR-0016's preflight now has an environment where it passes.** That is the whole delivery: the
routing decision and the runtime it demands finally exist in the same place.

**Nothing in this change invokes a specialist.** The image can run one; the node still stops at
`SpecialistNotWiredError`. Wiring is the next change, and keeping it separate means the image can be
built, inspected and rejected on its own terms.

**The image is much larger than the default** - **1.55 GB against 452 MB** - and that cost buys one
capability. CI reports both on every run rather than gating on a threshold nobody can justify; a
jump being visible is what makes it noticeable.

**Half a gigabyte of that was avoidable and `docker history` is how it was found, not intuition.**
The first build came out at 2.05 GB with a single **837 MB** layer, which was Debian's `npm`
package pulling in `node-gyp` and a C/Python build toolchain to install one JavaScript CLI. Node
from nodejs.org's own tarball ships npm and needs none of it. The remaining bulk is the JDK
(317 MB), which is not avoidable - the baseline compiles Java.

**What is not verified here.** No specialist has been invoked, so the daemon-mount decision in § 5
is reasoned rather than exercised — in particular, Testcontainers inside a container talking to the
host daemon needs host-path agreement for bind mounts, and nothing here proves that agreement holds.
The `claude` session is unverified for the reason in § 3: there is no session to bake in. What *is*
verified is that the image builds, that `java -version`, `mvn -v` and `claude --version` all report
the pinned versions inside it, and that the default image still reports none of them — the second
half being the assertion that keeps this from quietly becoming a bigger default.
