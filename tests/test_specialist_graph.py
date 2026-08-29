"""Tests for the specialist graph: design, a human gate, then generate (ADR-0019).

The specialist is a real subprocess throughout - a Python script written into `tmp_path` - for the
reason `test_specialist_invocation.py` gives: the interesting failures only happen in a real
process. What is *not* faked is the durability machinery either; the resume tests drive the graph
through this repo's real serde and a real saver.

Invented vocabulary, as everywhere in `tests/`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from langgraph.types import Command

from agentic_control_plane import specialist_graph, specialist_invocation, tools
from agentic_control_plane.checkpointer import build_memory_checkpointer
from agentic_control_plane.specialist_graph import (
    DESIGN_ARTIFACT_NAME,
    DESIGN_GATE,
    DESIGN_OUTPUT_DIRNAME,
    build_specialist_graph,
    design_node,
    generate_node,
)
from agentic_control_plane.specialist_state import SpecialistPhaseResult, SpecialistRunState
from agentic_control_plane.specialists import ExternalSpecialist, SpecialistPhase

# Writes the artifact the second phase needs, then answers on the contract.
DESIGN_BODY = f"""
import os
out = args.output
os.makedirs(out, exist_ok=True)
open(os.path.join(out, {DESIGN_ARTIFACT_NAME!r}), 'w').write(json.dumps({{'steps': ['one', 'two']}}))
print(json.dumps({{'status': 'ok', 'phase': 'design', 'run_id': args.run_id,
                  'detail': 'designed 2 steps', 'gate_item_count': 1,
                  'tenant_repo': args.tenant_repo, 'output_path': out}}))
"""

GENERATE_BODY = """
import os
os.makedirs(args.output, exist_ok=True)
open(os.path.join(args.output, 'Generated.java'), 'w').write('class Generated {}')
design = json.load(open(args.design))
print(json.dumps({'status': 'ok', 'phase': 'generate', 'run_id': args.run_id,
                  'detail': 'generated %d steps' % len(design['steps']),
                  'steps_compiled': len(design['steps']),
                  'design_seen': args.design, 'tenant_repo': args.tenant_repo,
                  'output_path': args.output}))
"""


def write_specialist(tmp_path: Path, design_body: str, generate_body: str) -> ExternalSpecialist:
    script = tmp_path / "widget_migrator.py"
    script.write_text(
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('subcommand')\n"
        "p.add_argument('--tenant-repo')\n"
        "p.add_argument('--output')\n"
        "p.add_argument('--design')\n"
        "p.add_argument('--run-id')\n"
        "p.add_argument('--json', action='store_true')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'plan':\n"
        + "".join(f"    {line}\n" for line in design_body.strip().splitlines())
        + "else:\n"
        + "".join(f"    {line}\n" for line in generate_body.strip().splitlines()),
        encoding="utf-8",
    )
    return ExternalSpecialist(
        kind="external",
        command=sys.executable,
        phases={
            "design": SpecialistPhase(args=[str(script), "plan"], timeout_seconds=60),
            "generate": SpecialistPhase(args=[str(script), "build"], timeout_seconds=60),
        },
    )


@pytest.fixture()
def paths(tmp_path: Path) -> tuple[Path, Path]:
    tenant_repo = tmp_path / "workspace" / "run-1"
    output_repo = tmp_path / "workspace" / "run-1-output"
    tenant_repo.mkdir(parents=True)
    return tenant_repo, output_repo


def initial(run_id: str = "run-1") -> SpecialistRunState:
    return SpecialistRunState(
        run_id=run_id,
        specialist="widget-migrator",
        scenario="widget-modernisation",
        target_repository="widget-service",
    )


def config(run_id: str = "run-1") -> dict:
    return {"configurable": {"thread_id": run_id}}


def compile_graph(tmp_path: Path, paths, design=DESIGN_BODY, generate=GENERATE_BODY, saver=None):
    tenant_repo, output_repo = paths
    return build_specialist_graph(
        specialist=write_specialist(tmp_path, design, generate),
        tenant_repo=tenant_repo,
        output_repo=output_repo,
        checkpointer=saver or build_memory_checkpointer(),
    )


# --- the gate is real, and it comes before the expensive half ---------------------


def test_the_run_parks_at_the_design_gate_before_generating(tmp_path: Path, paths):
    tenant_repo, output_repo = paths
    result = compile_graph(tmp_path, paths).invoke(initial(), config())

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["gate_type"] == DESIGN_GATE
    assert payload["design"]["gate_item_count"] == 1
    # The expensive phase has not run. This is the whole reason the gate is placed here.
    assert not output_repo.exists()


def test_the_reviewer_sees_the_artifact_rather_than_a_summary_of_it(tmp_path: Path, paths):
    result = compile_graph(tmp_path, paths).invoke(initial(), config())
    payload = result["__interrupt__"][0].value

    assert payload["design"]["detail"] == "designed 2 steps"
    assert payload["specialist"] == "widget-migrator"
    assert payload["scenario"] == "widget-modernisation"
    assert payload["target_repository"] == "widget-service"


def test_approval_runs_generate_and_completes(tmp_path: Path, paths):
    tenant_repo, output_repo = paths
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())

    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["run_status"] == "completed"
    assert final["safe_stop"] is False
    assert (output_repo / "Generated.java").is_file()
    assert final["phases"]["generate"].payload["steps_compiled"] == 2
    assert final["gates"][DESIGN_GATE].status == "approved"


def test_rejection_ends_the_run_without_generating(tmp_path: Path, paths):
    tenant_repo, output_repo = paths
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())

    final = graph.invoke(Command(resume={"status": "rejected"}), config())

    # Completed, not failed: the governance step did exactly its job.
    assert final["run_status"] == "completed"
    assert final["gates"][DESIGN_GATE].status == "rejected"
    assert not output_repo.exists()
    assert "generate" not in final["phases"]


# --- the two paths, and which repository each one touches -------------------------


def test_design_reads_and_writes_inside_the_run_s_own_clone(tmp_path: Path, paths):
    tenant_repo, _ = paths
    result = compile_graph(tmp_path, paths).invoke(initial(), config())
    design = result["phases"]["design"]

    assert Path(design.payload["tenant_repo"]) == tenant_repo
    assert Path(design.payload["output_path"]) == tenant_repo / DESIGN_OUTPUT_DIRNAME
    assert (tenant_repo / DESIGN_OUTPUT_DIRNAME / DESIGN_ARTIFACT_NAME).is_file()


def test_generate_writes_to_the_target_project_never_to_the_source(tmp_path: Path, paths):
    """ADR-0009's rule, and the one mistake in this node that would be expensive: generated

    code landing in the read-only checkout it was derived from.
    """
    tenant_repo, output_repo = paths
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    generate = final["phases"]["generate"]
    assert Path(generate.payload["output_path"]) == output_repo
    assert Path(generate.payload["tenant_repo"]) == tenant_repo
    assert not list(tenant_repo.rglob("Generated.java"))


def test_the_approved_design_path_is_the_whole_handoff(tmp_path: Path, paths):
    """Two separate processes with no shared state: the second is handed the first's file."""
    tenant_repo, _ = paths
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    seen = Path(final["phases"]["generate"].payload["design_seen"])
    assert seen == tenant_repo / DESIGN_OUTPUT_DIRNAME / DESIGN_ARTIFACT_NAME


def test_one_run_id_reaches_both_phases(tmp_path: Path, paths):
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial("the-orchestrator-run-id"), config("the-orchestrator-run-id"))
    final = graph.invoke(
        Command(resume={"status": "approved"}), config("the-orchestrator-run-id")
    )
    assert final["phases"]["design"].payload["run_id"] == "the-orchestrator-run-id"
    assert final["phases"]["generate"].payload["run_id"] == "the-orchestrator-run-id"


# --- failure ends the run in a defined state, never half-way ----------------------


def test_a_failing_design_stops_before_the_gate(tmp_path: Path, paths):
    body = (
        "print(json.dumps({'status': 'error', 'phase': 'design', 'run_id': args.run_id,\n"
        "                  'detail': 'the target names no work'}))\n"
        "sys.exit(1)\n"
    )
    result = compile_graph(tmp_path, paths, design=body).invoke(initial(), config())

    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    assert "__interrupt__" not in result
    assert any("names no work" in e.detail for e in result["events"])


def test_a_failing_generate_leaves_the_run_failed_with_the_design_intact(tmp_path: Path, paths):
    body = (
        "print(json.dumps({'status': 'error', 'phase': 'generate', 'run_id': args.run_id,\n"
        "                  'detail': 'the build never converged'}))\n"
        "sys.exit(1)\n"
    )
    graph = compile_graph(tmp_path, paths, generate=body)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["safe_stop"] is True
    assert final["run_status"] == "failed"
    # The approved design survives the failure: a rerun does not re-review it.
    assert final["phases"]["design"].status == "ok"


def test_a_design_that_reports_success_without_writing_the_artifact_is_caught(
    tmp_path: Path, paths
):
    """Caught at the hand-off, naming the path, rather than inside the next process."""
    body = (
        "print(json.dumps({'status': 'ok', 'phase': 'design', 'run_id': args.run_id,\n"
        "                  'detail': 'claimed success, wrote nothing'}))\n"
    )
    graph = compile_graph(tmp_path, paths, design=body)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["safe_stop"] is True
    assert any(DESIGN_ARTIFACT_NAME in e.detail for e in final["events"])


# --- durability: the reason a gate can wait for a human at all --------------------


def test_the_artifact_survives_a_pause_and_a_second_saver_context(tmp_path: Path, paths):
    """What PR #9 proved for a spike, asserted here for the graph that ships.

    Two independently constructed savers over one store, which is the container-restart case
    rather than a simulation of it.
    """
    tenant_repo, output_repo = paths
    saver = build_memory_checkpointer()

    parked = compile_graph(tmp_path, paths, saver=saver).invoke(initial(), config())
    assert "__interrupt__" in parked

    # A different compiled graph object, the same store - as a resuming process would build.
    resumed_graph = compile_graph(tmp_path, paths, saver=saver)
    stored = resumed_graph.get_state(config()).values
    # The specialist's own answer, whole and unmodelled, read back from the store before the run
    # resumes. `gate_item_count` is a field this package has no schema for, which is the point.
    assert stored["phases"]["design"].payload["gate_item_count"] == 1
    assert stored["phases"]["design"].detail == "designed 2 steps"
    # And the file the second phase will be handed is still on disk beside it.
    assert (tenant_repo / DESIGN_OUTPUT_DIRNAME / DESIGN_ARTIFACT_NAME).is_file()

    final = resumed_graph.invoke(Command(resume={"status": "approved"}), config())
    assert final["run_status"] == "completed"
    assert (output_repo / "Generated.java").is_file()


def test_the_state_round_trips_through_this_repo_s_real_serde():
    """A model missing from the serde allowlist fails at *resume* - hours later, another

    process - so it is asserted here rather than discovered there.
    """
    from agentic_control_plane.checkpointer import build_serde

    serde = build_serde()
    state = initial()
    state.phases["design"] = __import__(
        "agentic_control_plane.specialist_state", fromlist=["SpecialistPhaseResult"]
    ).SpecialistPhaseResult(phase="design", status="ok", payload={"steps": ["one"]})

    restored = serde.loads_typed(serde.dumps_typed(state))
    assert restored.run_id == state.run_id
    assert restored.phases["design"].payload == {"steps": ["one"]}


def test_the_audit_trail_names_both_phases_and_the_gate(tmp_path: Path, paths):
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    nodes = [event.node for event in final["events"]]
    assert "design" in nodes and DESIGN_GATE in nodes and "generate" in nodes
    assert any(e.event_type == "gate_decision" and e.decision == "approved" for e in final["events"])


def test_the_json_dump_of_a_parked_state_is_serialisable(tmp_path: Path, paths):
    """The audit sink writes JSONL; a payload that cannot be dumped breaks it at write time."""
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())
    values = graph.get_state(config()).values
    assert json.loads(SpecialistRunState(**values).model_dump_json())["run_id"] == "run-1"


def test_generate_called_without_a_design_refuses_rather_than_crashing(tmp_path: Path, paths):
    """Unreachable through the graph - `generate` has no inbound edge except through the gate -

    and kept because this node is a plain function anything could call. Tested directly for the
    same reason: a guard nothing exercises is indistinguishable from one that does not work.
    """
    from agentic_control_plane.specialist_graph import generate_node

    tenant_repo, output_repo = paths
    result = generate_node(
        initial(),
        specialist=write_specialist(tmp_path, DESIGN_BODY, GENERATE_BODY),
        tenant_repo=tenant_repo,
        output_repo=output_repo,
    )

    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    assert any("no design phase result" in event.detail for event in result["events"])
    assert not output_repo.exists()


# --- publishing: the only point at which anything leaves the run -------------------


@pytest.fixture()
def target_project(tmp_path: Path) -> Path:
    """A real bare-ish origin the graph can clone and push to."""
    import subprocess

    from agentic_control_plane import tools

    origin = tmp_path / "target-project"
    tools.write_code_files(origin, {"README.md": "the project generated code lands in\n"})
    tools.git_commit_all(origin, "initial")
    subprocess.run(["git", "branch", "-M", "main"], cwd=origin, check=True, capture_output=True)
    # Pushing to a non-bare repo's checked-out branch is refused by git; this run pushes to its
    # own branch, so only that needs to be allowed.
    subprocess.run(
        ["git", "config", "receive.denyCurrentBranch", "ignore"],
        cwd=origin, check=True, capture_output=True,
    )
    return origin


def publishing_graph(tmp_path: Path, paths, target: Path, saver=None, generate=GENERATE_BODY):
    tenant_repo, output_repo = paths
    return build_specialist_graph(
        specialist=write_specialist(tmp_path, DESIGN_BODY, generate),
        tenant_repo=tenant_repo,
        output_repo=output_repo,
        checkpointer=saver or build_memory_checkpointer(),
        output_repository=str(target),
        output_branch="main",
    )


def approve_through_design(graph):
    graph.invoke(initial(), config())
    return graph.invoke(Command(resume={"status": "approved"}), config())


def test_generate_clones_the_target_project_and_writes_into_it(
    tmp_path: Path, paths, target_project: Path
):
    tenant_repo, output_repo = paths
    graph = publishing_graph(tmp_path, paths, target_project)
    parked = approve_through_design(graph)

    # The checkout is real, and carries the target project's own history.
    assert (output_repo / ".git").is_dir()
    assert (output_repo / "README.md").is_file()
    assert (output_repo / "Generated.java").is_file()
    # And it has parked on the release gate rather than finishing.
    assert parked["__interrupt__"][0].value["gate_type"] == "merge_release_approval"


def test_the_release_gate_shows_what_would_be_published(
    tmp_path: Path, paths, target_project: Path
):
    graph = publishing_graph(tmp_path, paths, target_project)
    payload = approve_through_design(graph)["__interrupt__"][0].value

    assert payload["output_repository"] == str(target_project)
    assert payload["output_branch"] == "main"
    assert "Generated.java" in payload["files"]
    assert payload["generated"]["steps_compiled"] == 2


def test_nothing_is_committed_until_the_release_gate_approves(
    tmp_path: Path, paths, target_project: Path
):
    """The reason this graph needs no rollback: an unapproved run leaves the target project

    exactly as it found it, and a failed generate leaves an untouched checkout rather than a
    reverted one.
    """
    from agentic_control_plane import tools

    tenant_repo, output_repo = paths
    graph = publishing_graph(tmp_path, paths, target_project)
    approve_through_design(graph)

    head_before = tools.git_current_commit(output_repo)
    assert tools._run_git(output_repo, "status", "--porcelain"), "generated files are uncommitted"

    final = graph.invoke(Command(resume={"status": "rejected"}), config())

    assert final["run_status"] == "completed"
    assert final["published"] is False
    # `.get`, not `[...]`: a channel no node ever wrote and whose default is None is absent from
    # the returned mapping rather than present-and-None. Absent and None mean the same thing here
    # and the assertion below is the one that actually settles it.
    assert final.get("commit_sha_after") is None
    assert tools.git_current_commit(output_repo) == head_before, "nothing was committed"


def test_approval_commits_and_reports_what_publish_mode_did(
    tmp_path: Path, paths, target_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """PUBLISH_MODE defaults to `none`: the change is committed and reported, never pushed.

    Governance without delivery, the same default the SDLC path has (ADR-0012).
    """
    from agentic_control_plane import tools

    monkeypatch.delenv("PUBLISH_MODE", raising=False)
    tenant_repo, output_repo = paths
    graph = publishing_graph(tmp_path, paths, target_project)
    approve_through_design(graph)

    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["run_status"] == "completed"
    assert final["commit_sha_after"] == tools.git_current_commit(output_repo)
    assert final["published"] is False
    assert "none" in final["publish_detail"]
    assert final["gates"]["merge_release_approval"].status == "approved"


def test_branch_mode_pushes_the_generated_change_to_its_own_branch(
    tmp_path: Path, paths, target_project: Path, monkeypatch: pytest.MonkeyPatch
):
    """The irreversible half, exercised against a real remote rather than reasoned about."""
    from agentic_control_plane import publish, tools

    monkeypatch.setenv("PUBLISH_MODE", "branch")
    graph = publishing_graph(tmp_path, paths, target_project)
    approve_through_design(graph)

    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["published"] is True
    expected = publish.branch_name("run-1")
    assert final["publish_branch"] == expected
    landed = tools._run_git(target_project, "branch", "--list", expected)
    assert expected in landed, "the branch must exist on the target project"
    # The branch the run cloned is untouched.
    assert tools._run_git(target_project, "rev-parse", "main") != final["commit_sha_after"]


def test_a_route_with_no_output_repository_still_runs_and_publishes_nothing(
    tmp_path: Path, paths
):
    """Unchanged behaviour for a route that names nowhere to write."""
    tenant_repo, output_repo = paths
    graph = compile_graph(tmp_path, paths)
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["run_status"] == "completed"
    assert final["published"] is False
    assert final["output_repository"] == ""
    assert (output_repo / "Generated.java").is_file()
    assert not (output_repo / ".git").exists()


def test_an_unreachable_target_project_fails_before_the_specialist_runs(
    tmp_path: Path, paths
):
    tenant_repo, output_repo = paths
    graph = build_specialist_graph(
        specialist=write_specialist(tmp_path, DESIGN_BODY, GENERATE_BODY),
        tenant_repo=tenant_repo,
        output_repo=output_repo,
        checkpointer=build_memory_checkpointer(),
        output_repository=str(tmp_path / "no-such-project"),
        output_branch="main",
    )
    graph.invoke(initial(), config())
    final = graph.invoke(Command(resume={"status": "approved"}), config())

    assert final["safe_stop"] is True
    assert final["run_status"] == "failed"
    assert not (output_repo / "Generated.java").exists()


def test_an_existing_checkout_is_reused_rather_than_re_cloned(
    tmp_path: Path, paths, target_project: Path
):
    """A design was approved against a particular state of the world; silently replacing the

    checkout mid-run would discard that.
    """
    tenant_repo, output_repo = paths
    graph = publishing_graph(tmp_path, paths, target_project)
    approve_through_design(graph)

    marker = output_repo / "left-by-a-previous-slice.txt"
    marker.write_text("still here", encoding="utf-8")

    from agentic_control_plane.specialist_graph import ensure_output_checkout

    ensure_output_checkout(output_repo, str(target_project), "main")
    assert marker.is_file()


def test_the_file_list_is_empty_when_there_is_no_output_directory(tmp_path: Path):
    from agentic_control_plane.specialist_graph import _generated_file_names

    assert _generated_file_names(tmp_path / "never-created") == []


def test_a_commit_that_fails_stops_the_run_rather_than_reporting_a_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """git is stubbed to fail, because it will not fail on its own here: `git_commit_all`

    initialises a repository when handed a plain directory, so an absent checkout commits
    happily. What this branch actually guards is git failing for its own reasons - an index
    lock, a timeout, a corrupt object - and the property under test is that such a run stops
    rather than reporting a publish of a commit that does not exist.
    """
    from agentic_control_plane import specialist_graph
    from agentic_control_plane.specialist_graph import publish_node

    def refuse(workspace, message):
        raise tools.GitOperationError("git index.lock exists")

    monkeypatch.setattr(specialist_graph.tools, "git_commit_all", refuse)

    def must_not_run(**kwargs):
        raise AssertionError("nothing may be published when the commit failed")

    monkeypatch.setattr(specialist_graph.publish, "publish_change", must_not_run)

    state = initial()
    state.output_repository = "https://example.invalid/o/target.git"

    result = publish_node(state, output_repo=tmp_path)

    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    assert "published" not in result
    assert any("index.lock" in event.detail for event in result["events"])


def test_a_phase_whose_runtime_is_absent_stops_before_invoking_anything(
    tmp_path: Path, paths, monkeypatch: pytest.MonkeyPatch
):
    """The preflight ADR-0016 wrote, on the path that never called it.

    `require_runtime`'s only caller was the SDLC graph's coder node, and ADR-0020 routes a
    specialist run away from that graph before it starts - so no specialist run had ever reached
    it. Every existing test here passes with or without the check, because none of their phases
    declares a requirement. This one declares one that cannot be satisfied.
    """
    tenant_repo, output_repo = paths
    specialist = write_specialist(tmp_path, DESIGN_BODY, GENERATE_BODY)
    specialist.phases["design"].requires.executables = ["a-compiler-nobody-installed"]

    invoked: list[str] = []
    monkeypatch.setattr(
        specialist_invocation,
        "invoke",
        lambda **kwargs: invoked.append(kwargs["phase_name"]),
    )

    result = design_node(initial(), specialist=specialist, tenant_repo=tenant_repo)

    assert not invoked, "the point of a preflight is that nothing runs"
    assert result["safe_stop"] is True
    assert result["run_status"] == "failed"
    detail = json.dumps(result, default=str)
    assert "a-compiler-nobody-installed" in detail, "what is absent is named, not implied"


def test_generate_checks_its_runtime_before_cloning_the_output_repository(
    tmp_path: Path, paths, monkeypatch: pytest.MonkeyPatch
):
    """Ordering, asserted: an environment that cannot run the phase never gets a checkout.

    `generate` declares more than `design` does - a JDK, a build tool, a daemon - so it is where
    a half-provisioned deployment is caught. Catching it after the clone would leave a checkout
    behind for a run that was never going to proceed.
    """
    tenant_repo, output_repo = paths
    specialist = write_specialist(tmp_path, DESIGN_BODY, GENERATE_BODY)
    specialist.phases["generate"].requires.executables = ["a-compiler-nobody-installed"]

    cloned: list[str] = []
    monkeypatch.setattr(
        specialist_graph,
        "ensure_output_checkout",
        lambda *a, **k: cloned.append("clone"),
    )

    # A real design artifact, so the check above the preflight passes and the clone is genuinely
    # the next thing that would happen. Without it this test would short-circuit on a missing
    # design and assert nothing about ordering at all.
    design_dir = tenant_repo / DESIGN_OUTPUT_DIRNAME
    design_dir.mkdir(parents=True, exist_ok=True)
    (design_dir / DESIGN_ARTIFACT_NAME).write_text("{}", encoding="utf-8")

    state = initial()
    state.phases = {
        "design": SpecialistPhaseResult(
            phase="design",
            status="ok",
            detail="",
            output_path=str(design_dir),
        )
    }
    result = generate_node(
        state,
        specialist=specialist,
        tenant_repo=tenant_repo,
        output_repo=output_repo,
        output_repository="https://example.invalid/out.git",
    )

    assert not cloned, "no checkout for an environment that cannot run the phase"
    assert result["run_status"] == "failed"
    assert "a-compiler-nobody-installed" in json.dumps(result, default=str)


def test_the_preflight_defers_an_unknown_phase_to_the_invoker(tmp_path: Path, paths):
    """Not an oversight: `invoke` reports an unknown phase and names what the specialist does
    declare while doing so, which is strictly more useful than anything this could say.
    """
    tenant_repo, _ = paths
    specialist = write_specialist(tmp_path, DESIGN_BODY, GENERATE_BODY)

    assert specialist_graph._preflight(initial(), specialist, "a-phase-that-does-not-exist") is None
