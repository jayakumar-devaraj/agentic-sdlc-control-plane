"""Can a specialist's result artifact reach a human gate and survive being paused on?

**Why this exists.** The platform's migration half produces a design artifact in a separate
process and hands it here for review; this repo's ADR-0003 counterpart specifies the whole
exchange, and until now it had been implemented and tested **on the producing side only**. The
receiving side was assumed. That is the sort of assumption that holds until the day it does not,
and then holds up a milestone.

**What this is, and what it is not.** A spike against the primitives, not a new capability. It adds
no node, no gate type and no state field: the point is to find out whether a specialist artifact can
travel through the machinery *as it stands*, and to make whatever it needs a measured requirement
rather than a guess. Building the invocation node before knowing that would be designing against an
untested assumption, which is what this repo's own planning notes keep warning about.

**Deliberately domain-blind** (ADR-0001). The artifact here is an opaque JSON document with a
`schema_version`, a list of review facts, and a payload this repo never parses. It carries no
vocabulary from any tenant, and the test asserts that opacity rather than merely observing it -- a
control plane that had to understand a specialist's payload would be a control plane with a domain.

The three things a real handoff needs, and what each test here settles:

1. the artifact survives the checkpointer's **allowlisted serde** -- an artifact that cannot be
   persisted cannot be paused on;
2. the facts a specialist emits reach the interrupt payload **as facts**, not as a decision this
   repo has already made on the human's behalf;
3. it survives a **real durable** pause and resume, because a gate that loses its subject on a
   container restart is not a gate.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from agentic_control_plane.checkpointer import (
    _postgres_conn_string,
    build_memory_checkpointer,
    build_serde,
)

#: A specialist's result, in the shape the handoff contract describes: a version, facts for a
#: reviewer, and an opaque payload. The payload below is deliberately meaningless to this repo --
#: nested, mixed-type, and about nothing it knows.
SPECIALIST_ARTIFACT: dict = {
    "schema_version": "3.0.0",
    "produced_by": "specialist-cli",
    "review_facts": [
        {"category": "unsupported_construct", "summary": "a construct the specialist declined"},
        {"category": "low_confidence_rule", "summary": "a rule it scored below its threshold"},
    ],
    "payload": {
        "entities": [{"name": "A", "fields": [{"name": "x", "precision": 11, "scale": 2}]}],
        "units": [{"name": "one", "inputs": ["A"], "outputs": ["A"], "condition": None}],
        "endpoints": [],
    },
}


class SpikeState(BaseModel):
    """The smallest state that can carry a handoff. Not `GraphState`, on purpose.

    Reusing the production state would mean adding a field for the artifact, and that is the very
    decision this spike exists to inform rather than pre-empt.
    """

    artifact: dict = Field(default_factory=dict)
    approved: bool = False
    decided_by: str = ""


def _build(checkpointer):
    """A two-node graph: pause on the artifact, then record what the human said."""

    def review(state: SpikeState) -> dict:
        decision = interrupt(
            {
                # The gate names the artifact and the facts, and stops there. No verdict is
                # computed here: whether facts should block is the operator's policy, not this
                # graph's, which is the same line the production gates already hold.
                "gate_type": "specialist_artifact_review",
                "schema_version": state.artifact["schema_version"],
                "review_facts": state.artifact["review_facts"],
            }
        )
        return {
            "approved": decision.get("status") == "approved",
            "decided_by": decision.get("decided_by", ""),
        }

    graph = StateGraph(SpikeState)
    graph.add_node("review", review)
    graph.add_edge(START, "review")
    graph.add_edge("review", END)
    return graph.compile(checkpointer=checkpointer)


def _resume(compiled, config, decision: dict):
    return compiled.invoke(Command(resume=decision), config=config)


# --- 1. The artifact survives the serde the checkpointer actually uses ------------------------------


def test_an_opaque_specialist_artifact_survives_the_allowlisted_serde():
    """The allowlist is the first thing that could have refused this, and it does not.

    `build_serde` restricts msgpack to this package's own state models. A specialist artifact is
    none of them, so the question is real: had it required a registered type, the handoff would
    have needed a package change before it could even be persisted.
    """
    serde = build_serde()
    restored = serde.loads_typed(serde.dumps_typed(SPECIALIST_ARTIFACT))

    assert restored == SPECIALIST_ARTIFACT
    # Byte-identical under a canonical dump: a handoff that reorders or coerces a specialist's
    # numbers would be corrupting evidence a reviewer is about to approve.
    assert json.dumps(restored, sort_keys=True) == json.dumps(SPECIALIST_ARTIFACT, sort_keys=True)


def test_the_artifact_needs_no_type_this_repo_declares():
    # Stated as an assertion because it is the whole domain-agnostic claim: the artifact is plain
    # JSON, so the control plane can carry it without importing, declaring or parsing a tenant type.
    assert json.loads(json.dumps(SPECIALIST_ARTIFACT)) == SPECIALIST_ARTIFACT


# --- 2. The facts reach the gate as facts -----------------------------------------------------------


def test_the_gate_surfaces_the_specialists_facts_without_deciding_on_them():
    compiled = _build(build_memory_checkpointer())
    config = {"configurable": {"thread_id": f"spike-{uuid.uuid4()}"}}

    result = compiled.invoke(SpikeState(artifact=SPECIALIST_ARTIFACT), config=config)

    payload = result["__interrupt__"][0].value
    assert payload["gate_type"] == "specialist_artifact_review"
    assert [f["category"] for f in payload["review_facts"]] == [
        "unsupported_construct",
        "low_confidence_rule",
    ]
    # No verdict, no score, no "blocked" flag. Whether two facts should stop a merge is operator
    # policy; a gate that pre-judged them would be making that decision for every deployment.
    assert not {"status", "approved", "blocked", "decision"} & set(payload)


def test_a_human_can_approve_or_reject_the_same_artifact():
    for status, expected in (("approved", True), ("rejected", False)):
        compiled = _build(build_memory_checkpointer())
        config = {"configurable": {"thread_id": f"spike-{uuid.uuid4()}"}}
        compiled.invoke(SpikeState(artifact=SPECIALIST_ARTIFACT), config=config)

        final = _resume(compiled, config, {"status": status, "decided_by": "human"})

        assert final["approved"] is expected
        assert final["decided_by"] == "human"


# --- 3. A real durable pause, because a gate that loses its subject is not a gate -------------------

#: Built by the app's own helper, so this test authenticates the way the running control plane
#: does -- reading the password from the Docker secret file rather than carrying a copy. Only the
#: host and port are overridden: inside compose the service is `postgres:5432`, and from a test on
#: the host it is the published `localhost:5433`.
def _dsn() -> str:
    os.environ.setdefault("POSTGRES_HOST", "localhost")
    os.environ.setdefault("POSTGRES_PORT", "5433")
    os.environ.setdefault(
        "POSTGRES_PASSWORD_FILE", str(Path(__file__).resolve().parents[1] / "secrets" / "postgres_password.txt")
    )
    return _postgres_conn_string()


_SKIP = (
    "No reachable Postgres for the durable checkpointer (`docker compose up -d postgres`, "
    "published on 5433). A MemorySaver would pass this while proving nothing about surviving a "
    "restart, so it skips rather than silently weakening."
)


def _postgres_or_skip() -> str:
    dsn = _dsn()
    try:
        psycopg.connect(dsn, connect_timeout=3).close()
    except (psycopg.Error, OSError):
        pytest.skip(_SKIP)
    return dsn


def test_the_artifact_survives_a_durable_pause_and_a_separate_resume():
    """The claim the handoff actually rests on, against the real checkpointer.

    Two independent `PostgresSaver` contexts: the first pauses at the gate and is closed, the
    second resumes on the same thread id. Nothing about the artifact is held in memory between
    them, so this is the container-restart case rather than a simulation of it.
    """
    dsn = _postgres_or_skip()
    thread_id = f"spike-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.serde = build_serde()
        saver.setup()
        paused = _build(saver).invoke(SpikeState(artifact=SPECIALIST_ARTIFACT), config=config)
        assert "__interrupt__" in paused, "the graph must be waiting on a human"

    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.serde = build_serde()
        compiled = _build(saver)

        # The artifact is readable from the store alone, before anyone resumes -- which is what
        # lets an approval interface show a reviewer what they are approving.
        state = compiled.get_state(config)
        assert state.values["artifact"] == SPECIALIST_ARTIFACT

        final = _resume(compiled, config, {"status": "approved", "decided_by": "human"})

    assert final["approved"] is True
    assert final["artifact"] == SPECIALIST_ARTIFACT, "the artifact must outlive the pause intact"
