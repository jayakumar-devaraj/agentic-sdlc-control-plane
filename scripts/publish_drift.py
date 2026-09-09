"""Publish one drift signal to start a specialist run, and prove it landed by offsets.

    python scripts/publish_drift.py <run_id>

**Why this exists beside the README's `kafka-console-producer` recipe.** That recipe drives the
*demo* path from inside a container on the broker's own network. A specialist run is driven from the
host, and a host-side publish to this broker **intermittently times out** -- measured at roughly
three in eight. The producer then reports a failure for a message that was in fact written, or
returns before the write is visible, and the run either starts twice or appears not to start at all.

So the only trustworthy confirmation is the topic's **end offset moving by exactly one**, which is
what this script asserts. A non-zero exit means "not published"; retry it. Do not infer success from
the absence of a traceback.

`run_id` becomes the `correlation_id` on the envelope and the LangGraph `thread_id` the run is keyed
by, so it must be unique per run and is the value every gate decision correlates on. The convention
used by the recorded runs is `stepNN-<program>-<YYYYmmdd-HHMMSS>`.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime

from kafka import KafkaConsumer, KafkaProducer
from kafka.structs import TopicPartition

TOPIC = "mlops.drift-detected.v1"
BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "127.0.0.1:9092")
TENANT_REPO = os.environ.get(
    "TENANT_REPO_URL",
    "https://github.com/jayakumar-devaraj/carddemo-tenant-service.git",
)

if len(sys.argv) != 2:
    raise SystemExit(__doc__)
run_id = sys.argv[1]

event = {
    "event_id": str(uuid.uuid4()),
    "correlation_id": run_id,
    "service": "mlops",
    "event_type": "drift-detected",
    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "producer": {"service": "mlops", "instance_id": "operator-cli"},
    "git_target": {"repo_url": TENANT_REPO, "branch": "main"},
    "scenario_type": "brownfield",
    # Only the signal is injected. Genuine detection compares a trailing reference window against
    # the current hour, which no manual run can populate; everything after this point is the
    # production path.
    "metrics": {
        "metric_name": "interest_posting_accuracy",
        "reference_value": 0.997,
        "current_value": 0.951,
        "relative_change_pct": -4.6,
        "threshold_pct": 2.0,
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
    print(f"LANDED  run_id={run_id}  event_id={event['event_id']}")
    raise SystemExit(0)
print(f"DID NOT LAND (offset moved {after - before}); retry")
raise SystemExit(1)
