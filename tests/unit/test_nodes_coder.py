"""Tests for the Coder node's replay-mode logic and prompt construction.

Deliberately excludes live-mode tests that call the real `claude` CLI - a
committed test suite must run offline and deterministically on any machine,
not require auth/network/cost on every run. Live-mode behavior was validated
manually during development (see PLANNING notes) and is exercised for real every
time a fixture is captured or --live mode is demoed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_control_plane import specialists, tools
from agentic_control_plane.nodes import coder as coder_module
from agentic_control_plane.nodes.coder import (
    _available_dependencies_note,
    _build_prompt,
    _extract_json_object,
    _task_plan_note,
    coder,
)
from agentic_control_plane.specialists import (
    DEFAULT_CLI_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    BuiltinSpecialist,
    ExternalSpecialist,
    Route,
    RoutingTable,
    RuntimeRequirements,
    SpecialistPhase,
    default_routing_table,
)
from agentic_control_plane.state import ArchitectureDesign, GraphState, Task


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    fixtures = tmp_path / "fixtures"
    scenario_dir = fixtures / "brownfield"
    scenario_dir.mkdir(parents=True)
    (scenario_dir / "transcript.json").write_text(
        json.dumps(
            {
                "attempts": [
                    {
                        "attempt_number": 1,
                        "code_files": {"app/analytics.py": "counter = 0\n"},
                        "rationale": "buggy attempt",
                    },
                    {
                        "attempt_number": 2,
                        "code_files": {
                            "app/analytics.py": "import threading\ncounter = 0\n"
                        },
                        "rationale": "fixed with lock",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return fixtures


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def test_replay_selects_attempt_matching_retry_count(fixture_dir: Path, workspace: Path):
    state = GraphState(scenario_type="brownfield", requirement_raw="x", mode="replay")
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert result["coder"].attempt_number == 1
    assert result["coder"].rationale == "buggy attempt"
    assert (workspace / "app" / "analytics.py").read_text() == "counter = 0\n"


def test_replay_selects_second_attempt_after_one_retry(fixture_dir: Path, workspace: Path):
    state = GraphState(
        scenario_type="brownfield", requirement_raw="x", mode="replay", retry_count=1
    )
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert result["coder"].attempt_number == 2
    assert "threading" in result["coder"].code_files["app/analytics.py"]


def test_replay_missing_scenario_fixture_fails_safely(fixture_dir: Path, workspace: Path):
    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="replay")
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    assert "no recorded fixture" in result["coder"].rationale


def test_replay_attempt_overrun_fails_safely(fixture_dir: Path, workspace: Path):
    state = GraphState(
        scenario_type="brownfield", requirement_raw="x", mode="replay", retry_count=5
    )
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert result["safe_stop"] is True
    assert "no recorded fixture attempt" in result["coder"].rationale


def test_replay_fallback_triggered_without_recorded_fallback_fails_safely(
    fixture_dir: Path, workspace: Path
):
    state = GraphState(
        scenario_type="brownfield", requirement_raw="x", mode="replay", fallback_triggered=True
    )
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert result["safe_stop"] is True
    assert "fallback" in result["coder"].rationale.lower()


def test_replay_selects_recorded_fallback_attempt(tmp_path: Path, workspace: Path):
    fixtures = tmp_path / "fixtures"
    scenario_dir = fixtures / "brownfield"
    scenario_dir.mkdir(parents=True)
    scenario_dir.joinpath("transcript.json").write_text(
        json.dumps(
            {
                "attempts": [
                    {
                        "attempt_number": 4,
                        "fallback": True,
                        "code_files": {"app/x.py": "minimal fix\n"},
                        "rationale": "fallback attempt",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    state = GraphState(
        scenario_type="brownfield",
        requirement_raw="x",
        mode="replay",
        retry_count=3,
        fallback_triggered=True,
    )
    result = coder(state, workspace=workspace, fixtures_dir=fixtures)
    assert result["coder"].rationale == "fallback attempt"


def test_extract_json_object_parses_pure_json():
    assert _extract_json_object('{"a": "b"}') == {"a": "b"}


def test_extract_json_object_falls_back_to_brace_substring():
    """Regression test: heavier `claude -p` generations can prefix their JSON with

    a short narration despite explicit "no prose" instructions - found during
    brownfield fixture capture (num_turns=46, "All content verified. Here is the
    final JSON output." before the actual JSON).
    """
    prose = 'All content verified. Here is the final JSON output.\n\n{"app/x.py": "content"}'
    assert _extract_json_object(prose) == {"app/x.py": "content"}


def test_extract_json_object_raises_when_no_json_present():
    with pytest.raises(json.JSONDecodeError):
        _extract_json_object("no json here at all")


def test_available_dependencies_note_lists_requirements(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("somelib==0.1\notherlib==2.0\n", encoding="utf-8")
    note = _available_dependencies_note(tmp_path)
    assert note is not None
    assert "somelib==0.1" in note
    assert "otherlib==2.0" in note


def test_available_dependencies_note_none_without_requirements_file(tmp_path: Path):
    assert _available_dependencies_note(tmp_path) is None


def test_task_plan_note_includes_task_descriptions():
    state = GraphState(
        scenario_type="ambiguous",
        requirement_raw="x",
        tasks=[Task(id="T0", description="Reconcile with X", depends_on=[])],
    )
    note = _task_plan_note(state)
    assert note is not None
    assert "T0" in note and "Reconcile with X" in note


def test_task_plan_note_none_when_no_tasks():
    state = GraphState(scenario_type="greenfield", requirement_raw="x")
    assert _task_plan_note(state) is None


def test_build_prompt_includes_task_plan_and_consistency_instruction(tmp_path: Path):
    state = GraphState(
        scenario_type="ambiguous",
        requirement_raw="x",
        requirement_clarified="make it more reliable",
        architecture_design=ArchitectureDesign(summary="Reuse the existing throttling module."),
        tasks=[Task(id="T0", description="Reconcile with existing throttling.py", depends_on=[])],
    )
    prompt = _build_prompt(state, tmp_path)
    assert "Reconcile with existing throttling.py" in prompt
    assert "MUST also return updated versions" in prompt


def test_build_prompt_fallback_branch_uses_simplest_framing(tmp_path: Path):
    state = GraphState(
        scenario_type="brownfield",
        requirement_raw="x",
        requirement_clarified="fix the bug",
        fallback_triggered=True,
    )
    prompt = _build_prompt(state, tmp_path)
    assert "SIMPLEST possible" in prompt


def test_build_prompt_includes_prior_test_failures(tmp_path: Path):
    from agentic_control_plane.state import TestResult

    state = GraphState(
        scenario_type="brownfield",
        requirement_raw="x",
        requirement_clarified="fix the bug",
        test=TestResult(passed=False, failures=["FAILED tests/test_x.py::test_thing"]),
    )
    prompt = _build_prompt(state, tmp_path)
    assert "FAILED tests/test_x.py::test_thing" in prompt


# --- specialist and model routing (ADR-0016) --------------------------------------
#
# Every table below is synthetic and its vocabulary invented: CI fails the build when
# `tests/` carries a tenant's words, which is the same rule that put the real routing
# table in `config/` rather than in the package.


def builtin_table(model: str = "some-model-id", timeout: int = 42) -> RoutingTable:
    return RoutingTable(
        specialists={"default": BuiltinSpecialist(model=model, cli_timeout_seconds=timeout)}
    )


def external_table(repository: str = "widget-service") -> RoutingTable:
    return RoutingTable(
        specialists={
            "default": BuiltinSpecialist(),
            "widget-migrator": ExternalSpecialist(
                kind="external",
                command="widget-migrator",
                phases={
                    "plan": SpecialistPhase(
                        args=["plan"], requires=RuntimeRequirements(executables=["some-cli"])
                    )
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


def test_replay_records_which_specialist_the_run_resolved_to(
    fixture_dir: Path, workspace: Path
):
    """A replayed run still says how it was routed - that is what makes it auditable."""
    state = GraphState(scenario_type="brownfield", requirement_raw="x", mode="replay")
    result = coder(state, workspace=workspace, fixtures_dir=fixture_dir)
    assert "specialist 'default'" in result["events"][0].detail


def test_live_uses_the_model_and_timeout_the_table_gives_it(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """The two values that used to be module constants now come from configuration."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(
            argv,
            returncode=0,
            stdout=json.dumps(
                {"subtype": "success", "result": json.dumps({"app/x.py": "x = 1\n"})}
            ),
            stderr="",
        )

    monkeypatch.setattr(coder_module.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="live")
    result = coder(
        state,
        workspace=workspace,
        fixtures_dir=workspace,
        routing_table=builtin_table(model="some-model-id", timeout=42),
    )

    assert captured["argv"][captured["argv"].index("--model") + 1] == "some-model-id"
    assert captured["timeout"] == 42
    assert result["coder"].code_files == {"app/x.py": "x = 1\n"}
    assert "'default'" in result["coder"].rationale


def test_an_unconfigured_deployment_still_uses_the_historical_model(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """The regression this refactor could plausibly cause, pinned against its own defaults."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(
            argv,
            returncode=0,
            stdout=json.dumps({"subtype": "success", "result": json.dumps({"a.py": "x = 1\n"})}),
            stderr="",
        )

    monkeypatch.setattr(coder_module.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(coder_module.subprocess, "run", fake_run)

    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="live")
    coder(
        state,
        workspace=workspace,
        fixtures_dir=workspace,
        routing_table=default_routing_table(),
    )

    assert captured["argv"][captured["argv"].index("--model") + 1] == DEFAULT_MODEL
    assert captured["timeout"] == DEFAULT_CLI_TIMEOUT_SECONDS


def test_routing_matches_the_repository_the_workspace_was_cloned_from(workspace: Path):
    tools.git_init_if_needed(workspace)
    tools._run_git(
        workspace, "remote", "add", "origin", "https://example.invalid/o/widget-service.git"
    )

    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="live")
    result = coder(
        state,
        workspace=workspace,
        fixtures_dir=workspace,
        routing_table=external_table(),
    )

    assert result["safe_stop"] is True
    assert "widget-migrator" in result["coder"].rationale


def test_a_workspace_with_no_remote_takes_the_default(
    fixture_dir: Path, workspace: Path
):
    """A fresh `git init` is an unidentified target, and gets the general-purpose generator."""
    tools.git_init_if_needed(workspace)
    state = GraphState(scenario_type="brownfield", requirement_raw="x", mode="replay")
    result = coder(
        state, workspace=workspace, fixtures_dir=fixture_dir, routing_table=external_table()
    )
    assert "specialist 'default'" in result["events"][0].detail


def test_a_missing_specialist_runtime_is_named_rather_than_thrown_as_an_errno(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: None)
    tools.git_init_if_needed(workspace)
    tools._run_git(
        workspace, "remote", "add", "origin", "https://example.invalid/o/widget-service.git"
    )

    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="live")
    result = coder(
        state, workspace=workspace, fixtures_dir=workspace, routing_table=external_table()
    )

    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    assert "some-cli" in result["coder"].rationale
    assert "customised image" in result["coder"].rationale


def test_a_routed_run_stops_rather_than_falling_back_to_the_builtin_generator(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """The failure mode this refuses: a target routed away from the general-purpose

    generator quietly receiving its output anyway, committed through a release gate,
    with nothing in the audit trail saying the routing never happened.
    """
    monkeypatch.setattr(specialists.shutil, "which", lambda name: f"/usr/bin/{name}")

    def explode(*args, **kwargs):
        raise AssertionError("the built-in generator must not run for a routed target")

    monkeypatch.setattr(coder_module, "_invoke_claude_cli", explode)
    tools.git_init_if_needed(workspace)
    tools._run_git(
        workspace, "remote", "add", "origin", "https://example.invalid/o/widget-service.git"
    )

    state = GraphState(scenario_type="greenfield", requirement_raw="x", mode="live")
    result = coder(
        state, workspace=workspace, fixtures_dir=workspace, routing_table=external_table()
    )

    assert result["safe_stop"] is True
    assert "not wired yet" in result["coder"].rationale
    assert result["coder"].code_files == {}
    assert list(workspace.glob("*.py")) == []


def test_a_broken_routing_table_safe_stops_rather_than_crashing_the_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    bad = workspace.parent / "broken.yaml"
    bad.write_text("version: 1\nspecialists: {}\n", encoding="utf-8")
    monkeypatch.setenv("SPECIALIST_ROUTING_FILE", str(bad))

    state = GraphState(scenario_type="brownfield", requirement_raw="x", mode="replay")
    result = coder(state, workspace=workspace, fixtures_dir=workspace)

    assert result["safe_stop"] is True
    assert "defines no 'default'" in result["coder"].rationale
