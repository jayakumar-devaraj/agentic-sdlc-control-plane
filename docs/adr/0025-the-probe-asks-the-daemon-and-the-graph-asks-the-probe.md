# ADR-0025: The probe asks the daemon, and the specialist graph finally asks the probe

## Status

**Accepted** (2026-08-28). Fixes two defects the first real specialist run exposed
(ADR-0023, ADR-0024 are the other two). Makes
[ADR-0016](0016-specialist-and-model-routing-lives-in-configuration.md)'s preflight real on the
path [ADR-0020](0020-a-run-is-routed-to-its-graph-before-it-starts.md) created, and corrects the
probe [ADR-0017](0017-a-specialist-capable-image-is-a-second-image.md) § 5's deployment made wrong.
Supersedes nothing.

## Context

Two defects, found together on the first specialist deployment, and neither could be fixed alone.

**The probe asked the client.** `_docker_daemon_reachable` opened with:

```python
if shutil.which("docker") is None:
    return False
```

directly beneath a docstring reading *"Probe the daemon, not the client: `docker` on PATH proves
nothing about it."* The docstring is right about the failure it was written for — a client on
`PATH` with no daemon behind it — and the implementation then made the opposite error. ADR-0017
§ 5's deployment mounts the host's socket and installs no client, because nothing in the image
needs one: Testcontainers speaks HTTP over the socket rather than shelling out. Measured from
inside the running container: the daemon answered `200` on `/version` while this function reported
it absent.

**Nothing on the specialist path called the preflight.** `require_runtime`'s only caller was
`nodes/coder.py`, on the SDLC graph — and ADR-0020 routes a specialist run away from that graph
*before it starts*. So no specialist run had ever reached it. ADR-0016, ADR-0017, ADR-0018 and
ADR-0022 all describe the preflight as guarding specialist runs; all four were describing an
intention.

**They had to be fixed together.** Wiring the preflight in while the probe still asked for a
client would have taken the one working specialist deployment and failed it at `generate` — a
correctly-provisioned environment refused by a check that could not see the daemon it was
mounted on.

## Decision

### 1. The probe asks the daemon over the socket, and keeps the client as a fallback

`_daemon_answers_on_socket` connects to the unix socket and sends `GET /_ping`, which is the
daemon's own liveness endpoint and the same route a library client takes. `DOCKER_HOST` decides
which socket: a `unix://` value names it, an unset value means the conventional
`/var/run/docker.sock`, and anything else — `tcp://`, a context — returns `None`, because only a
client knows how to follow those.

The client probe stays, second. A remote or context-addressed daemon is genuinely reachable only
through it, and dropping it to make the socket path look tidier would have swapped one false
negative for another.

**Order matters and is the fix.** Socket first means the deployment this repository actually ships
answers correctly with no `docker` binary present.

**Installing the CLI into the image was the alternative and is refused.** It would have made the
old probe work while leaving it wrong, and it puts a client in an image to satisfy a check rather
than because anything uses it. The requirement is a *daemon*; asking the daemon is the direct
question.

### 2. Both specialist phases preflight, and `generate` does it before it clones

`design` and `generate` call `require_runtime` through one `_preflight` helper before invoking
anything. In `generate` the check sits **above** `ensure_output_checkout`: that phase declares more
than `design` does — a JDK, a build tool, a daemon — so it is where a half-provisioned deployment
is caught, and catching it after the clone would leave a checkout behind for a run that was never
going to proceed.

An unknown phase name is deliberately *not* handled here. `invoke` reports it and names what the
specialist does declare while doing so, which is more than this could say.

### 3. `docker_daemon: true` stays on `generate`, and the observation is written down

The first real run of that phase compiled and did not test — 15 main classes, **0 test classes**,
no surefire directory — so Testcontainers never started and the daemon went unused. The
requirement is left in place anyway, for two reasons: it is the specialist's own declared
provisioning list, and this repository should not overrule another's statement of what it needs
from one observation of one version; and a phase that compiles today may test tomorrow, at which
point a deployment allowed to skip the daemon fails deep inside Maven rather than at the preflight.

It does mean the preflight currently demands something the phase does not use. That is the
conservative direction to be wrong in, and it is now recorded at the requirement rather than
discovered by the next person.

## Consequences

**ADR-0017 § 5's question is still open, and this ADR narrows it.** The socket is reachable from
inside the container — proven. Whether *Testcontainers* can use it, which needs host-path agreement
for bind mounts, is still unproven, because no test has run. The daemon mount is verified as far as
`/_ping` and no further.

**The tests for the client fallback needed an autouse fixture, and that is worth knowing.** Every
existing "no daemon" assertion patched `shutil.which` and `subprocess.run`, which stopped being
sufficient the moment a socket probe ran first: on a CI runner with a real
`/var/run/docker.sock` they would have passed on a laptop and failed in CI. They are now
explicitly given no socket, so they keep testing the fallback they were written for.

**The two new graph tests fail without this change and the old ones do not.** Every pre-existing
specialist-graph test passes with or without a preflight, because none of their phase fixtures
declares a requirement — which is exactly why the missing call survived. Confirmed by removing the
preflight: the ordering test fails on `assert not ['clone']`, the naming test on the absent
executable.

**The socket probe is tested against a fake socket, not a real one.** `AF_UNIX` is not dependable
across the platforms this is developed and built on, and a test that skips on the developer's
machine is a test nobody watches. The fake covers the request sent, a `200`, a non-`200`, and a
refused connection; `specialists.py` reaches 100%.
