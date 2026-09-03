"""Tests for checkpointer.py: the serde allowlist and connection-string construction.

Everything here runs with nothing started. The durability property this module used to
also carry - a run paused at a gate resuming from a completely fresh connection - needs
a real database, so it lives in tests/evaluation/test_checkpointer_durability.py.
"""

from __future__ import annotations

import pytest
from langgraph.graph import END, START, StateGraph

from agentic_control_plane.checkpointer import (
    _discover_state_model_allowlist,
    _postgres_conn_string,
    _read_secret,
    build_memory_checkpointer,
)
from agentic_control_plane.state import GraphState, RunMetrics


def test_serde_allowlist_discovers_every_state_submodel():
    allowlist = _discover_state_model_allowlist()
    names = {name for _, name in allowlist}
    assert {
        "GraphState",
        "AuditEvent",
        "GateRecord",
        "CodebaseImpact",
        "ArchitectureDesign",
        "Task",
        "CoderOutput",
        "TestResult",
        "GuardrailViolation",
        "DocumentationOutput",
        "RunMetrics",
    } <= names


def test_memory_checkpointer_round_trips_nested_submodels():
    """Without the allowlist, LangGraph's default serde falls back to a path it

    warns will be blocked in a future version for any custom Pydantic type nested in
    state. This confirms the round-trip works with the configured serde in place.
    """
    graph = StateGraph(GraphState)
    graph.add_node("noop", lambda state: {})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    compiled = graph.compile(checkpointer=build_memory_checkpointer())

    config = {"configurable": {"thread_id": "state-roundtrip-test"}}
    result = compiled.invoke(
        GraphState(
            scenario_type="ambiguous",
            requirement_raw="x",
            metrics=RunMetrics(success_rate=1.0),
        ),
        config=config,
    )
    assert result["metrics"].success_rate == 1.0


def test_read_secret_prefers_direct_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SOME_SECRET", "direct-value")
    assert _read_secret("SOME_SECRET") == "direct-value"


def test_read_secret_falls_back_to_file(monkeypatch: pytest.MonkeyPatch, tmp_path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("file-value\n", encoding="utf-8")
    monkeypatch.delenv("SOME_SECRET", raising=False)
    monkeypatch.setenv("SOME_SECRET_FILE", str(secret_file))
    assert _read_secret("SOME_SECRET") == "file-value"


def test_read_secret_returns_default_when_neither_is_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SOME_SECRET", raising=False)
    monkeypatch.delenv("SOME_SECRET_FILE", raising=False)
    assert _read_secret("SOME_SECRET", "fallback") == "fallback"


def test_conn_string_percent_encodes_credentials(monkeypatch: pytest.MonkeyPatch):
    """A password containing '@' or '/' - both legal - would otherwise produce a

    URI that silently parses into the wrong host or database instead of failing.
    """
    monkeypatch.setenv("POSTGRES_USER", "control_plane")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word")
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "control_plane")

    conn = _postgres_conn_string()

    assert conn == "postgresql://control_plane:p%40ss%2Fword@postgres:5432/control_plane"


def test_conn_string_defaults_carry_no_monolith_leftovers(monkeypatch: pytest.MonkeyPatch):
    """The source system defaulted user/database to 'orchestrator'. This repo's own

    Postgres is named control_plane; a stale default would connect somewhere real
    but wrong on a partially-configured environment.
    """
    for var in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_PASSWORD_FILE", "POSTGRES_DB"):
        monkeypatch.delenv(var, raising=False)

    conn = _postgres_conn_string()

    assert "orchestrator" not in conn
    assert conn.endswith("/control_plane")
