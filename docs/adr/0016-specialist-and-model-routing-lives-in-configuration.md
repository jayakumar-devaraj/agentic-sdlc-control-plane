# ADR-0016: Specialist and model routing lives in configuration, not in the package

## Status

**Accepted** (2026-08-26). Extends ADR-0001 (keep the control plane domain-agnostic) and inherits
the provisioning list named by `agentic-sdlc-cobol-modernizer` ADR-0055. Does not supersede
anything.

## Context

The Coder node is the only node that generates code, and it can do exactly one thing: ask a
general-purpose LLM through the `claude` CLI, or replay a fixture of a previous such ask. Two
constants in `nodes/coder.py` — the model id and the CLI timeout — were the whole of its routing
policy.

That is not enough for a platform whose stated job is to run a governed workflow against *whichever*
tenant service triggered it. Tenants are not interchangeable in how their code should be produced.
One of this platform's tenants is a legacy-modernization service whose generation step is not "ask a
model for files" at all: it is a separate installed tool with two bounded subcommands, its own
prompts, its own model configuration, and its own build-and-heal loop. Routing to it is a
first-class decision, not a prompt variation.

**The constraint that shapes the whole design is ADR-0001.** This package must contain no tenant
vocabulary, and since 2026-08-26 (PR #10) CI fails the build when it
does — for both tenants, in `agentic_control_plane/` and `tests/`. So a routing table that names a
tenant cannot live in the package or in its tests, and the resolver cannot special-case anything.

## Decision

### 1. The routing table is data, in `config/scenario_specialists.yaml`

The file sits at the repository root, beside the package rather than inside it, and is the only
place a tenant is ever named. `agentic_control_plane/specialists.py` parses and validates it and
knows nothing about what is in it; the CI vocabulary guard's scope — the package and its tests — is
therefore unchanged, and both continue to pass with the second tenant fully routed.

The path is overridable with `SPECIALIST_ROUTING_FILE` so a deployment can mount its own table
without rebuilding, which is the same posture `FIXTURES_DIR` already has.

**The resolver walks out of the package to find it, and that is safe here for a reason worth
recording.** `agentic-sdlc-cobol-modernizer` ADR-0055 found that six modules resolving their data
files by walking `parents[3]` all broke the moment the project was installed as a wheel — a built
wheel contained zero non-Python files, and nothing in CI could catch it because CI installs with
`pip install -e`, where the walk still lands on the repository root. This repository is never
installed as a wheel: its image is built by `COPY . .` into `/app`, so `config/` sits beside the
package at runtime exactly as it does in a checkout. The env override exists so that a deployment
which does move it is not relying on that walk at all.

### 2. The routing key is the target repository, not the scenario type

`GraphState.scenario_type` is `greenfield | brownfield | ambiguous`. Those are *shapes of work* that
every tenant has; none of them identifies a tenant, so none of them can select a specialist. Keying
on them would have meant "all brownfield work is COBOL", which is false the moment there are two
tenants.

The identity that is actually available is the repository the run is working on. Every run clones
its target into its own workspace (ADR-0009), so at the moment the Coder node runs, the workspace's
`origin` remote *is* the tenant identity — already present, needing no new field on `GraphState`,
no change to the `drift-detected` event contract, and no change to either consumer.

**The match is on the repository name, not the full URL.** `https://github.com/o/r.git`,
`https://github.com/o/r` and `git@github.com:o/r.git` are the same repository, and a table that had
to enumerate spellings would be a table that silently fails to match. The last path segment, minus
any `.git`, compared case-insensitively, is the key.

A workspace with no git remote — a fresh `git init`, which is what the test suite and the local
quickstart produce — matches no route and takes the default. That is the correct answer, not a
fallback: an unidentified target gets the general-purpose generator.

### 3. The built-in generator is an entry in the table like any other

`claude-sonnet-5` and the 480-second CLI timeout are no longer module constants; they are the
values of the `default` entry. This is the "model routing" half of the change and the half that can
break a working deployment, so the defaults are the previous constants exactly, and **an absent
config file yields a table with only that entry** — a deployment that never creates the file
behaves as it did before this ADR, including in the container image, where the file is present but
could be masked by a mount.

### 4. A specialist declares the runtime it needs, per phase, and the resolver preflights it

`cobol-modernizer` ADR-0055 names what its two subcommands require of whatever invokes them: the
`claude` CLI for either, plus JDK 25, Maven and a Docker daemon for `generate`. Control-plane's
image is `python:3.12-slim` and carries none of them.

That list is recorded in the table, per phase, and checked before anything is invoked. This follows
`coder.py`'s existing posture for `claude` exactly, and deliberately rather than by coincidence: the
shipped image carries nothing, so a capable deployment is **a customized image, not a config
toggle**, and the failure says so instead of surfacing an errno from `subprocess`. `_require_claude_cli`
is the precedent; `preflight` is the same idea generalized to a declared list.

### 5. This change resolves and preflights. It does not invoke

Wiring the actual subprocess call, its artifact hand-off and its gate is a separate change with its
own ADR. Until then a run routed to an external specialist reaches a **defined terminal state with a
stated reason** — the same safe-stop the node already had for a missing fixture and a missing CLI —
rather than a crash or a silent fall-through to the general-purpose generator.

Falling back to the built-in generator was the tempting alternative and is refused. A tenant routed
to a specialist because a general-purpose model cannot do its work would get a general-purpose
model's output, committed through a release gate, with nothing in the audit trail saying the routing
did not happen.

### 6. PyYAML becomes a declared dependency

It was already resolvable in a built environment as a transitive dependency of `langchain-core`.
Importing something the requirements file does not name is a bug regardless of whether it currently
works, so it is pinned here directly.

## Consequences

**The step that wires a real specialist is now a small change in one node**, not a design exercise:
the table, the models, the resolution and the runtime contract exist and are tested.

**Adding a tenant is a config edit plus one CI edit.** The vocabulary guard is still a denylist, and
its own comment already proposes deriving its terms from this file now that this file exists. That
derivation is not done here — it would turn a green build into one that depends on parsing a config
file at lint time, and it deserves its own change — but the input it needs now exists.

**A malformed table fails at load, not at the Coder node.** `SpecialistConfigError` names the file
and the offending entry. The alternative — tolerating unknown keys and unresolvable references —
would defer a typo in a specialist name to whichever run first matched that route.

**What is not verified here.** No specialist has been invoked, because invoking one is the next
change (§ 5). The preflight is exercised against a simulated `PATH` and a simulated Docker probe,
not against an image that actually carries JDK 25 and Maven — no such image exists yet, and building
one is the provisioning work ADR-0055 named. What *is* verified is that a table naming a real
specialist parses, that the route resolves from a real git remote, that a missing runtime is
reported by name, and that the previous behaviour is unchanged in every default case.
