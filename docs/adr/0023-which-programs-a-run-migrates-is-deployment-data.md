# ADR-0023: Which programs a run migrates is deployment data, not run data

## Status

**Accepted** (2026-08-28). Fixes the first real specialist invocation, which failed on a contract
gap neither side could see from inside itself. Extends
[ADR-0018](0018-what-control-plane-requires-of-any-specialist.md) without amending it, and stays
inside [ADR-0016](0016-specialist-and-model-routing-lives-in-configuration.md)'s rule that every
tenant-specific word is data.

## Context

The first run to reach a real specialist ended in three seconds:

```
cobol-modernizer design --tenant-repo /workspaces/<run> --output <run>/.specialist-design
                        --run-id <run> --json
cobol-modernizer design: error: the following arguments are required: --programs
```

ADR-0018 states the contract as four flags control-plane appends — `--tenant-repo`, `--output`,
`--run-id`, `--json` — plus whatever static flags the routing table adds. That is still true. What
it did not anticipate is a specialist with a **required input that is neither of those**: a list of
which programs to work on, without which the subcommand will not start.

**Both sides were tested and both sides were complete.** control-plane's suite drives a Python
script standing in for a specialist, and that stand-in accepts the four flags and asks for nothing
else. The specialist's own suite invokes its CLI with `--programs`, because its tests know what it
is for. Neither suite could fail, and the gap lived exactly where neither was looking — the same
shape as G31, and the same shape the platform's own cross-repo testing note describes.

The failure was cheap: it happened at argument parsing, before any model call, and the run reached
`safe_stop` with the workspace removed and nothing published. That is the graph behaving correctly.
It would not have been cheap one phase later.

## Decision

### 1. The program list goes in the routing table, as data

```yaml
design:
  args: [design, --programs, CBACT04C]
```

`build_argv` already lays a phase's configured `args` down before the flags control-plane appends,
so this needs no code: the argv becomes `design --programs CBACT04C --tenant-repo ... --json`, and
`--programs`' `nargs='+'` stops at the next `--`-prefixed token. Verified against the installed CLI
before the config was written — it parsed, fanned out one program branch, and failed only on a
deliberately absent tenant repo, at a cost of zero model calls.

**Teaching control-plane to derive the list is refused, and not narrowly.** A program name is
tenant vocabulary; ADR-0001 forbids the package from carrying it and CI fails the build on it. More
than that, choosing which COBOL program answers a drift in a batch duration metric is domain
reasoning — it is the specialist's whole job, and doing it in the orchestrator would put the
interesting decision in the component that is supposed to be ignorant.

### 2. Carrying it on the drift event is the real alternative, and it is deferred rather than dismissed

The envelope has a `payload`, and control-plane could pass a `payload.programs` through generically
without ever naming a program itself. That would make the program list **run data** — a drift event
about card settlement could migrate the settlement program, and two runs against one tenant could
do different work.

It is not done here because nothing yet produces such an event: the drift producer emits metrics
about an operational condition, and inventing a field for one consumer to read would be designing
a contract from the wrong end. When a producer has a reason to name programs, this is the change
to make, and this ADR is where the reasoning already is.

The cost of deferring is stated in the consequences rather than hidden.

### 3. One program, not four

The specialist's `--help` shows four in its example. This ships one.

G7 closes against a **named instance**, not a mechanism — its own register entry records G31 being
closed on a grep that passed for the only program a renderer had ever seen, after which the second
program needed four declared contract facts before it would build. A first run migrating four
programs at once cannot say which of them the platform actually handles, and a partial failure
across four is harder to read than a clean result on one.

`CBACT04C` because it is the program the gap register and the step-49 brief both name, and it
exists at `app/cbl/CBACT04C.cbl` in the tenant repository.

## Consequences

**Every run against this tenant migrates the same program until someone edits the routing table.**
That is a real limitation and the direct cost of § 2. A second program is a one-line config change
and a redeploy; a *different program per run* is the deferred design, not a config change.

**ADR-0018's four flags are necessary and not sufficient, and the routing table is where the
difference lives.** Any future specialist with required per-run inputs lands here the same way. The
generic contract survives — control-plane still knows nothing about what `--programs` means.

**The stand-in specialist in the suite is now known to be weaker than the real one.** It accepts
the four flags and asks for nothing else, so no test in this repository can catch a required flag
the routing table forgets. Making the stand-in demand a configured flag would close that, and it is
not done here because it is a test-design change, not part of this fix.

**Config changes need an image rebuild.** `config/scenario_specialists.yaml` is baked in, so this
fix reaches a deployment through `docker compose build`, not a restart. `SPECIALIST_ROUTING_FILE`
exists for the case where that is the wrong tradeoff (ADR-0016), and was deliberately not used here:
a permanent contract fix belongs in the committed file, not in a local override.
