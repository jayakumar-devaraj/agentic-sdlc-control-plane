"""Tests for deciding which graph a run executes (ADR-0020).

Two directions, and they are not symmetric. Starting a run reads the fresh clone; resuming one
reads the durable checkpoint, because the resuming process is routinely not the one that started
it. Both are exercised here against real git remotes and a real saver rather than mocks - the
whole decision is "what does this workspace actually say", and a mock would answer whatever the
author expected.

Invented vocabulary, as everywhere in `tests/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentic_control_plane import run_routing, tools, workspace
from agentic_control_plane.checkpointer import build_memory_checkpointer
from agentic_control_plane.run_routing import RoutingChangedError
from agentic_control_plane.specialist_state import SpecialistRunState
from agentic_control_plane.specialists import (
    BuiltinSpecialist,
    ExternalSpecialist,
    Route,
    RoutingTable,
    SpecialistPhase,
)
from agentic_control_plane.state import GraphState

ROUTED_REPOSITORY = "widget-service"


def routing_table(repository: str = ROUTED_REPOSITORY) -> RoutingTable:
    return RoutingTable(
        specialists={
            "default": BuiltinSpecialist(),
            "widget-migrator": ExternalSpecialist(
                kind="external",
                command=sys.executable,
                phases={
                    "design": SpecialistPhase(args=["-c", "pass", "design"]),
                    "generate": SpecialistPhase(args=["-c", "pass", "generate"]),
                },
            ),
        },
        routes=[
            Route(
                scenario="widget-modernisation",
                repository=repository,
                specialist="widget-migrator",
            )
        ],
    )


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SPECIALIST_OUTPUT_ROOT", str(tmp_path / "specialist-output"))
    return tmp_path


def clone_with_origin(run_id: str, remote: str | None) -> Path:
    """A workspace shaped the way `clone_for_run` leaves one."""
    path = workspace.workspace_for(run_id)
    tools.git_init_if_needed(path)
    if remote is not None:
        tools._run_git(path, "remote", "add", "origin", remote)
    return path


def plan(run_id: str, table=None):
    return run_routing.plan_for_trigger(
        run_id=run_id,
        scenario_type="brownfield",
        requirement="something drifted",
        mode="replay",
        table=table if table is not None else routing_table(),
    )


# --- where the output goes ---------------------------------------------------------


def test_specialist_output_lives_outside_the_workspaces_root(roots: Path):
    """ADR-0009's lesson, applied before it costs anything a second time: reconciliation

    sweeps the workspaces root and deletes directories for terminal runs.
    """
    output = run_routing.output_path_for("run-1")
    workspaces = workspace.workspaces_root().resolve()
    assert workspaces not in output.resolve().parents
    assert output.name == "run-1"


def test_the_output_root_is_configurable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SPECIALIST_OUTPUT_ROOT", str(tmp_path / "elsewhere"))
    assert run_routing.output_path_for("run-1") == tmp_path / "elsewhere" / "run-1"


# --- starting a run ----------------------------------------------------------------


def test_an_unrouted_target_starts_the_sdlc_graph_exactly_as_before(roots: Path):
    """The regression this change could plausibly cause. `None` rather than the SDLC factory

    by name, so an unrouted run reaches `runner` on the path it always took.
    """
    clone_with_origin("run-1", "https://example.invalid/o/some-other-service.git")
    state, factory = plan("run-1")

    assert isinstance(state, GraphState)
    assert state.scenario_type == "brownfield"
    assert state.requirement_raw == "something drifted"
    assert factory is None


def test_a_workspace_with_no_remote_starts_the_sdlc_graph(roots: Path):
    clone_with_origin("run-1", None)
    state, factory = plan("run-1")
    assert isinstance(state, GraphState)
    assert factory is None


def test_a_routed_target_starts_a_specialist_run(roots: Path):
    clone_with_origin("run-1", f"https://example.invalid/o/{ROUTED_REPOSITORY}.git")
    state, factory = plan("run-1")

    assert isinstance(state, SpecialistRunState)
    assert state.run_id == "run-1"
    assert state.specialist == "widget-migrator"
    assert state.scenario == "widget-modernisation"
    assert state.target_repository == ROUTED_REPOSITORY
    assert factory is not None


def test_the_specialist_run_carries_no_sdlc_gate_and_no_requirement(roots: Path):
    """The point of routing here rather than at the coder node: a specialist run never enters

    the graph whose first three nodes would gate it on a Python impact analysis.
    """
    clone_with_origin("run-1", f"https://example.invalid/o/{ROUTED_REPOSITORY}.git")
    state, _ = plan("run-1")

    assert state.gates == {}
    assert not hasattr(state, "requirement_raw")
    assert not hasattr(state, "retry_count")


def test_the_factory_binds_this_run_s_own_paths(roots: Path):
    clone_with_origin("run-1", f"https://example.invalid/o/{ROUTED_REPOSITORY}.git")
    _, factory = plan("run-1")

    compiled = factory("run-1", build_memory_checkpointer())
    assert compiled is not None
    # The graph is the specialist one, not the SDLC one: its nodes are the two phases and a gate.
    assert set(compiled.get_graph().nodes) >= {"design", "design_review", "generate"}


def test_matching_ignores_url_spelling(roots: Path):
    clone_with_origin("run-1", f"git@example.invalid:o/{ROUTED_REPOSITORY}.git")
    state, factory = plan("run-1")
    assert factory is not None
    assert state.target_repository == ROUTED_REPOSITORY


# --- resuming one ------------------------------------------------------------------


def park_a_specialist_run(run_id: str, checkpointer) -> None:
    """Write a real checkpoint carrying specialist state, the way a parked run leaves one."""
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(SpecialistRunState)
    graph.add_node("noop", lambda state: {"scenario": "seen"})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    graph.compile(checkpointer=checkpointer).invoke(
        SpecialistRunState(run_id=run_id, specialist="widget-migrator"),
        {"configurable": {"thread_id": run_id}},
    )


def park_an_sdlc_run(run_id: str, checkpointer) -> None:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(GraphState)
    graph.add_node("noop", lambda state: {"requirement_clarified": "seen"})
    graph.add_edge(START, "noop")
    graph.add_edge("noop", END)
    graph.compile(checkpointer=checkpointer).invoke(
        GraphState(scenario_type="brownfield"), {"configurable": {"thread_id": run_id}}
    )


def test_a_parked_specialist_run_is_recognised_from_its_checkpoint_alone(roots: Path):
    """No clone consulted, no memory of the trigger - which is the situation a resume is in."""
    checkpointer = build_memory_checkpointer()
    park_a_specialist_run("run-1", checkpointer)
    clone_with_origin("run-1", f"https://example.invalid/o/{ROUTED_REPOSITORY}.git")

    factory = run_routing.graph_factory_for_existing_run(
        "run-1", checkpointer, routing_table()
    )
    assert factory is not None


def test_a_parked_sdlc_run_resumes_on_the_sdlc_graph(roots: Path):
    checkpointer = build_memory_checkpointer()
    park_an_sdlc_run("run-1", checkpointer)

    assert run_routing.graph_factory_for_existing_run("run-1", checkpointer, routing_table()) is None


def test_an_unknown_run_defaults_to_the_graph_that_existed_first(roots: Path):
    """Also the right answer for a checkpoint written before specialist runs existed."""
    checkpointer = build_memory_checkpointer()
    assert (
        run_routing.graph_factory_for_existing_run("never-seen", checkpointer, routing_table())
        is None
    )


def test_an_unreadable_checkpoint_is_treated_as_unknown_not_as_a_crash(roots: Path):
    class Broken:
        def get_tuple(self, config):
            raise RuntimeError("the store is unreachable")

    assert run_routing.graph_factory_for_existing_run("run-1", Broken(), routing_table()) is None


def test_a_routing_table_that_changed_under_a_parked_run_refuses_to_resume(roots: Path):
    """The expensive silent failure this prevents: generating from a design its specialist did

    not write, because the table was edited while a human was still looking at the gate.
    """
    checkpointer = build_memory_checkpointer()
    park_a_specialist_run("run-1", checkpointer)
    clone_with_origin("run-1", f"https://example.invalid/o/{ROUTED_REPOSITORY}.git")

    # The same target now routes nowhere - as an edit removing the route would leave it.
    with pytest.raises(RoutingChangedError, match="did not write"):
        run_routing.graph_factory_for_existing_run(
            "run-1", checkpointer, routing_table(repository="a-different-service")
        )
