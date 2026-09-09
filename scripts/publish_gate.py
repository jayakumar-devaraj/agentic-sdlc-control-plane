"""Answer one human gate on a parked run, and prove the decision landed by offsets.

    python scripts/publish_gate.py <run_id> <gate> <approve|reject> <comment>

Same shape as `publish_drift.py`, and for the same reason: a host-side publish to this broker
intermittently times out, so the only trustworthy confirmation is the topic's end offset moving by
exactly one. A non-zero exit means the decision was **not** published and the run is still parked.

**The gate names are not free text.** A specialist run parks at, in order:

1. `specialist_design_review` -- after the design phase. The run has produced a `design.json` and
   nothing has been generated yet, so this is the cheapest place to reject.
2. `merge_release_approval` -- after generate, with the differential verdict in hand. Approving
   this is what commits, pushes and attempts to open the pull request.

A brownfield run *without* a specialist parks at `codebase_impact_review` instead of the first of
those; the README's demo recipe covers that path.

`run_id` must be the same value the drift signal carried: it is the `correlation_id` the parked run
is resumed by, and a decision with a mismatched one is written successfully and never consumed,
which looks exactly like a hang.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime

from kafka import KafkaConsumer, KafkaProducer
from kafka.structs import TopicPartition

TOPIC = "control-plane.gate-decision.v1"
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
TENANT_REPO = os.environ.get(
    "TENANT_REPO_URL",
    "https://github.com/jayakumar-devaraj/carddemo-tenant-service.git",
)
DECIDED_BY = os.environ.get("GATE_DECIDED_BY", "jayakumar-devaraj")

if len(sys.argv) != 5:
    raise SystemExit(__doc__)
run_id, gate, decision, comment = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
if decision not in {"approve", "reject"}:
    raise SystemExit(f"decision must be 'approve' or 'reject', got {decision!r}")

event = {
    "event_id": str(uuid.uuid4()),
    "correlation_id": run_id,
    "service": "control-plane",
    "event_type": "gate-decision",
    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "producer": {"service": "control-plane", "instance_id": "operator-cli"},
    "git_target": {"repo_url": TENANT_REPO, "branch": "main"},
    "scenario_type": "brownfield",
    "payload": {
        "decision": decision,
        "decided_by": DECIDED_BY,
        "gate": gate,
        "comment": comment,
    },
}


def end_offsets() -> int:
    consumer = KafkaConsumer(bootstrap_servers=BOOTSTRAP, consumer_timeout_ms=10000)
    partitions = consumer.partitions_for_topic(TOPIC) or set()
    offsets = consumer.end_offsets([TopicPartition(TOPIC, p) for p in partitions])
    consumer.close()
    return sum(offsets.values())


before = end_offsets()
print(f"end offset before: {before}")

producer = KafkaProducer(
    bootstrap_servers=BOOTSTRAP,
    value_serializer=lambda v: json.dumps(v).encode(),
    request_timeout_ms=30000,
    retries=3,
)
try:
    meta = producer.send(TOPIC, event).get(timeout=30)
    print(f"send reported partition={meta.partition} offset={meta.offset}")
except Exception as exc:  # noqa: BLE001 - the timeout is the case this script exists for
    print(f"send FAILED: {type(exc).__name__}: {exc}")
finally:
    producer.flush(timeout=15)
    producer.close(timeout=15)

after = end_offsets()
print(f"end offset after: {after}")
if after == before + 1:
    print(f"LANDED  run_id={run_id}  gate={gate}  decision={decision}")
    raise SystemExit(0)
print(f"DID NOT LAND (offset moved {after - before}); retry")
raise SystemExit(1)
