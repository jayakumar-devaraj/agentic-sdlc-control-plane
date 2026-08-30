# 0027 - The specialist subprocess is traced before this service is

## Context

This service has no runtime tracing. Neither does the specialist it invokes — until now, on that
side. The specialist repository's ADR-0046 decided to emit OpenTelemetry spans from the one
function every one of its nodes calls, and its ADR-0060 implemented that, released as `v0.1.2`.

Nothing about that release reaches a deployment on its own. This repository pins the specialist to
an exact tag in two places, and `v0.1.1` predates the instrumentation entirely: a container built
from that pin emits no spans however its environment is configured. The pin is the whole delivery
mechanism, so moving it *is* the change.

The other half is configuration. The specialist runs as a subprocess of the consumer and inherits
its environment, so whether it traces is decided by variables on this service's container.

## Decision

**Move both pins to `v0.1.2`, and pass OpenTelemetry configuration through to the specialist from
the operator's environment.**

Two variables, `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_HEADERS`, both defaulting to
empty. Empty means off: the specialist constructs no exporter unless an endpoint is named, so a
deployment that wants no telemetry sets nothing and pays nothing.

Three things follow from that being the whole mechanism:

- **They are OpenTelemetry's standard variable names, not this platform's.** No file in either
  repository names a tracing backend. Where spans go is a variable, which is what the specialist's
  ADR-0046 traded for when it chose an OTel-compatible collector over a vendor SDK.
- **Passed through rather than written down.** The header carries a credential and
  `docker-compose.specialist.yml` is committed. The endpoint is deployment-specific, and from
  inside the container `localhost` *is* the container — a collector on the operator's machine is
  reached at `host.docker.internal`, and an endpoint copied from a browser's address bar produces
  a connection refused that reads like a broken collector.
- **The pin was verified before it was written.** `v0.1.2` was installed into a clean virtualenv
  and interrogated: the version it reports, that `telemetry/tracing.py` is present, that it stays
  off with no endpoint and on with one, and that the token conversion its release exists to carry
  is correct. This repository's routing table already claims that check for `v0.1.1`; it would be
  a poor time to stop doing it, given the tag is the only thing standing between a release and a
  container.

## Consequences

- **A specialist run is observable; this service still is not.** The spans come from the
  subprocess. The consumer's own work — the Kafka hop, the inbox, the graph, the checkpointer, the
  audit sink — emits nothing, and neither does the `coder` node's own `claude` CLI call, which has
  no package-level instrumentation on either side. Naming that plainly matters more than the
  progress does: "the container emits spans" is true and would be read as more than it is.
- **A specialist run is its own trace, not part of the caller's.** The specialist joins a W3C
  `TRACEPARENT` when its environment carries one (its ADR-0060 § 3), and nothing here exports one
  yet. Correlating the two halves needs this service instrumented first, which is the change above
  and not this one.
- **The default deployment is unaffected.** `docker-compose.yml` alone still runs the base image,
  which installs no specialist at all. Both variables live in the override, alongside the other
  things a specialist-capable deployment must supply and a committed file cannot.
- **The tenant's source reaches the collector.** The specialist captures prompt and completion text
  by default, and those prompts are the tenant's COBOL. That is deliberate on its side — its
  ADR-0046 refused a hosted backend precisely so the prompts could be recorded — but it becomes
  this deployment's decision at the moment an endpoint is named here. A collector outside the
  tenant's estate is a data-egress decision, and
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` is the switch that keeps the timings
  and token counts without the content.
