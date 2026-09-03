"""The durability property the whole gate design rests on, against a real Postgres.

Moved out of ``tests/unit/test_checkpointer.py``: everything else in that module
tests the serde allowlist and connection-string construction with nothing running,
while this one needs a database and proves a property no amount of mocking can.
Its own docstring is the clearest statement of why the tiers are separate - the
thing being tested is precisely that MemorySaver would not do.

Skips rather than fails when no Postgres is reachable, because the unit and contract
tiers must run clean on a laptop with nothing started. CI attaches a real
``postgres:16-alpine`` service container, which is where this actually runs.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from langgraph.graph import END, START, StateGraph

from agentic_control_plane.checkpointer import _postgres_conn_string, build_postgres_checkpointer
from agentic_control_plane.state import GraphState


def _postgres_reachable() -> bool:
    if not os.environ.get("POSTGRES_USER"):
        return False
    try:
        with psycopg.connect(_postgres_conn_string(), connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


@pytest.mark.skipif(
    not _postgres_reachable(), reason="requires PostgreSQL reachable via POSTGRES_* env vars"
)
def test_postgres_checkpointer_resumes_a_thread_from_a_fresh_connection():
    """The whole reason PostgresSaver was chosen over MemorySaver: a pending gate

    must survive something equivalent to a container restart. Simulates that by
    entering a *second*, independent build_postgres_checkpointer() context and
    confirming it can read back a thread a prior context wrote.
    """
    graph = StateGraph(GraphState)
    graph.add_node("noop", lambda state: {"requirement_clarified": "seen"})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)

    config = {"configurable": {"thread_id": "postgres-durability-test"}}

    with build_postgres_checkpointer() as checkpointer:
        compiled = graph.compile(checkpointer=checkpointer)
        compiled.invoke(GraphState(scenario_type="brownfield", requirement_raw="x"), config=config)

    # fresh checkpointer instance/connection - simulates resuming after a restart
    with build_postgres_checkpointer() as checkpointer2:
        compiled2 = graph.compile(checkpointer=checkpointer2)
        state = compiled2.get_state(config)
        assert state.values["requirement_clarified"] == "seen"
