"""Tests for running an external specialist as a subprocess (ADR-0018).

Every specialist here is a Python script written into `tmp_path` and run through a real
`subprocess`. That is the point rather than an inconvenience: the failures this module exists to
turn into useful messages - an entrypoint that is not installed, a phase that hangs, stdout that
is not JSON, an exit code that contradicts the payload - only happen in a real process, and a
mocked `subprocess.run` would assert that the mock behaves the way the author imagined.

The vocabulary is invented, for the reason it is invented everywhere else in `tests/`: CI fails
the build when this directory names a tenant, and the whole design is that it never has to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentic_control_plane.specialist_invocation import (
    JSON_FLAG,
    RUN_ID_FLAG,
    SpecialistFailedError,
    SpecialistInvocationError,
    SpecialistProtocolError,
    build_argv,
    invoke,
)
from agentic_control_plane.specialists import ExternalSpecialist, SpecialistPhase


def fake_specialist(tmp_path: Path, body: str, *, name: str = "widget-migrator") -> Path:
    """A runnable stand-in whose behaviour the test chooses."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        "import argparse, json, sys, time\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('subcommand')\n"
        "p.add_argument('--tenant-repo')\n"
        "p.add_argument('--output')\n"
        "p.add_argument('--run-id')\n"
        "p.add_argument('--json', action='store_true')\n"
        "p.add_argument('--scope', nargs='+', default=[])\n"
        "args = p.parse_args()\n" + body,
        encoding="utf-8",
    )
    return script


def specialist_for(script: Path, *, timeout_seconds: int = 30) -> ExternalSpecialist:
    """Point the `command` at this interpreter so the fake script is what actually runs."""
    return ExternalSpecialist(
        kind="external",
        command=sys.executable,
        phases={
            "plan": SpecialistPhase(args=[str(script), "plan"], timeout_seconds=timeout_seconds),
            "build": SpecialistPhase(args=[str(script), "build"], timeout_seconds=timeout_seconds),
        },
    )


OK_BODY = (
    "print(json.dumps({'status': 'ok', 'phase': args.subcommand, 'run_id': args.run_id,\n"
    "                  'detail': 'did the thing', 'items': 3, 'scope': args.scope}))\n"
)


# --- the command line -------------------------------------------------------------


def test_argv_puts_configured_flags_first_and_control_plane_s_last():
    specialist = ExternalSpecialist(
        kind="external",
        command="widget-migrator",
        phases={"plan": SpecialistPhase(args=["plan", "--scope", "alpha"])},
    )
    argv = build_argv(
        specialist,
        specialist.phases["plan"],
        run_id="run-42",
        options={"--tenant-repo": "/w/run-42", "--output": "/w/run-42/out"},
    )
    assert argv == [
        "widget-migrator",
        "plan",
        "--scope",
        "alpha",
        "--tenant-repo",
        "/w/run-42",
        "--output",
        "/w/run-42/out",
        RUN_ID_FLAG,
        "run-42",
        JSON_FLAG,
    ]


def test_a_multi_valued_option_expands_rather_than_being_joined():
    """A list-valued flag is one flag and several values, not one comma-joined string."""
    specialist = ExternalSpecialist(
        kind="external", command="widget-migrator", phases={"plan": SpecialistPhase(args=["plan"])}
    )
    argv = build_argv(
        specialist,
        specialist.phases["plan"],
        run_id="r",
        options={"--scope": ["alpha", "beta"]},
    )
    assert argv[:4] == ["widget-migrator", "plan", "--scope", "alpha"]
    assert "beta" in argv


# --- the happy path ---------------------------------------------------------------


def test_a_successful_phase_returns_its_whole_payload(tmp_path: Path):
    script = fake_specialist(tmp_path, OK_BODY)
    result = invoke(
        specialist=specialist_for(script),
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="run-42",
        options={"--output": str(tmp_path / "out")},
        cwd=tmp_path,
    )

    assert result.status == "ok"
    assert result.phase == "plan"
    assert result.run_id == "run-42"
    assert result.detail == "did the thing"
    # Opaque carry-through: this package has no schema for `items` and does not need one.
    assert result.payload["items"] == 3
    assert result.duration_seconds > 0


def test_control_plane_s_run_id_reaches_the_specialist(tmp_path: Path):
    """The audit chain's whole purpose: both sides log under one identifier."""
    script = fake_specialist(tmp_path, OK_BODY)
    result = invoke(
        specialist=specialist_for(script),
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="the-orchestrator-run-id",
        options={},
        cwd=tmp_path,
    )
    assert result.run_id == "the-orchestrator-run-id"


def test_configured_flags_are_passed_through_to_the_process(tmp_path: Path):
    script = fake_specialist(tmp_path, OK_BODY)
    specialist = specialist_for(script)
    specialist.phases["plan"].args = [str(script), "plan", "--scope", "alpha", "beta"]

    result = invoke(
        specialist=specialist,
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="r",
        options={},
        cwd=tmp_path,
    )
    assert result.payload["scope"] == ["alpha", "beta"]


# --- the ways it fails, each with a message worth reading -------------------------


def test_a_specialist_reporting_its_own_failure_raises_with_the_detail(tmp_path: Path):
    body = (
        "print(json.dumps({'status': 'error', 'phase': args.subcommand, 'run_id': args.run_id,\n"
        "                  'detail': 'the input named no work'}))\n"
        "sys.exit(1)\n"
    )
    script = fake_specialist(tmp_path, body)
    with pytest.raises(SpecialistFailedError, match="the input named no work"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_stdout_that_is_not_json_is_a_protocol_error(tmp_path: Path):
    script = fake_specialist(tmp_path, "print('I am a helpful narrator')\n")
    with pytest.raises(SpecialistProtocolError, match="not JSON"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_silence_on_stdout_reports_what_stderr_said(tmp_path: Path):
    """The realistic failure: a specialist that crashed before printing anything."""
    script = fake_specialist(
        tmp_path, "print('a traceback would go here', file=sys.stderr)\nsys.exit(3)\n"
    )
    with pytest.raises(SpecialistProtocolError, match="a traceback would go here"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_a_payload_missing_the_envelope_names_the_missing_fields(tmp_path: Path):
    script = fake_specialist(tmp_path, "print(json.dumps({'status': 'ok'}))\n")
    with pytest.raises(SpecialistProtocolError, match="missing"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_a_json_array_is_not_an_answer(tmp_path: Path):
    script = fake_specialist(tmp_path, "print(json.dumps([1, 2, 3]))\n")
    with pytest.raises(SpecialistProtocolError, match="not an object"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_ok_with_a_non_zero_exit_is_reported_rather_than_reconciled(tmp_path: Path):
    """Believing either half would hide a real defect in whichever specialist does this."""
    body = OK_BODY + "sys.exit(2)\n"
    script = fake_specialist(tmp_path, body)
    with pytest.raises(SpecialistProtocolError, match="exited 2"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_a_phase_that_hangs_is_killed_and_says_which_knob_to_turn(tmp_path: Path):
    script = fake_specialist(tmp_path, "time.sleep(30)\n")
    with pytest.raises(SpecialistInvocationError, match="timeout_seconds"):
        invoke(
            specialist=specialist_for(script, timeout_seconds=1),
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_an_uninstalled_specialist_says_so_rather_than_raising_an_errno(tmp_path: Path):
    specialist = ExternalSpecialist(
        kind="external",
        command="a-specialist-that-was-never-installed",
        phases={"plan": SpecialistPhase(args=["plan"])},
    )
    with pytest.raises(SpecialistInvocationError, match="not on PATH"):
        invoke(
            specialist=specialist,
            specialist_name="widget-migrator",
            phase_name="plan",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_an_unknown_phase_names_the_phases_that_exist(tmp_path: Path):
    script = fake_specialist(tmp_path, OK_BODY)
    with pytest.raises(SpecialistInvocationError, match=r"\['build', 'plan'\]"):
        invoke(
            specialist=specialist_for(script),
            specialist_name="widget-migrator",
            phase_name="deploy",
            run_id="r",
            options={},
            cwd=tmp_path,
        )


def test_the_specialist_runs_in_the_directory_it_is_given(tmp_path: Path):
    """The workspace is a per-run clone; a specialist orienting itself elsewhere reads the

    wrong tree. `nodes/coder.py` records the same lesson costing 46 tool-use turns.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    body = (
        "import os\n"
        "print(json.dumps({'status': 'ok', 'phase': args.subcommand, 'run_id': args.run_id,\n"
        "                  'detail': 'ok', 'cwd': os.getcwd()}))\n"
    )
    script = fake_specialist(tmp_path, body)
    result = invoke(
        specialist=specialist_for(script),
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="r",
        options={},
        cwd=workspace,
    )
    assert Path(result.payload["cwd"]).resolve() == workspace.resolve()


def test_extra_environment_reaches_the_process_without_replacing_the_rest(tmp_path: Path):
    body = (
        "import os\n"
        "print(json.dumps({'status': 'ok', 'phase': args.subcommand, 'run_id': args.run_id,\n"
        "                  'detail': 'ok', 'extra': os.environ.get('SPECIALIST_EXTRA'),\n"
        "                  'inherited': bool(os.environ.get('PATH'))}))\n"
    )
    script = fake_specialist(tmp_path, body)
    result = invoke(
        specialist=specialist_for(script),
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="r",
        options={},
        cwd=tmp_path,
        env={"SPECIALIST_EXTRA": "set-by-the-caller"},
    )
    assert result.payload["extra"] == "set-by-the-caller"
    assert result.payload["inherited"] is True


def test_a_phase_timeout_is_configurable_per_phase(tmp_path: Path):
    """Phases are not comparable: one asks a model for a document, one drives a compiler."""
    script = fake_specialist(tmp_path, OK_BODY)
    specialist = specialist_for(script)
    specialist.phases["plan"].timeout_seconds = 5
    specialist.phases["build"].timeout_seconds = 3600
    assert specialist.phases["plan"].timeout_seconds != specialist.phases["build"].timeout_seconds


def test_the_payload_survives_a_json_round_trip(tmp_path: Path):
    """What the durable checkpointer will have to do with it across a human gate."""
    script = fake_specialist(tmp_path, OK_BODY)
    result = invoke(
        specialist=specialist_for(script),
        specialist_name="widget-migrator",
        phase_name="plan",
        run_id="r",
        options={},
        cwd=tmp_path,
    )
    assert json.loads(json.dumps(result.payload)) == result.payload
