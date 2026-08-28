"""The graph a specialist run executes: design, a human gate, then generate.

**A separate topology from `graph.py`, and the reason is measured rather than aesthetic**
(ADR-0019). The SDLC graph's nodes after `coder` assume the built-in Python generator: the test
executor runs `pytest` in the run's workspace, the guardrails match Python patterns, and the
release gate commits that workspace. A specialist writes its output somewhere else and has
already run its own compile-and-heal loop, so `pytest` finds nothing, returns exit code 5, and
the retry loop re-invokes the specialist up to four times - each one a model call and a real
build - before rolling back a run that succeeded.

What this reuses is everything that made the SDLC graph durable: the same `interrupt()`, the same
`PostgresSaver`, the same `runner`, the same audit vocabulary. That combination was proved by
this repo's own PR #9 spike before any of it was written here - a two-node graph with its own
state, paused and resumed across two independent saver contexts with an opaque artifact intact.

The shape:

    START -> design -> (gate: specialist_design_review) -> generate
                            |                                 |
                            +-- rejected -> END               v
                                              (gate: merge_release_approval) -> publish -> END
                                                    |
                                                    +-- rejected -> END

Two gates, guarding different things. The first protects the expensive phase and asks about an
artifact a person can read. The second protects a **repository**, and is the only point at which
anything leaves this run.

`design` and `generate` are separate processes with no shared state, which is why the approved
artifact travels on the state rather than being re-derived: the path written by the first phase is
handed to the second verbatim.

**Nothing commits before the release gate approves.** That is why there is no rollback node here
and no equivalent of the SDLC graph's revert: a `generate` that fails half-way leaves an untouched
checkout rather than a reverted one, and a run that is never approved leaves the target project
exactly as it found it.

A route that names no output repository stops after `generate`, having written to a local
directory. That is the honest default - publishing to a repository nobody named is the one mistake
in this graph that cannot be undone by deleting a directory.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agentic_control_plane import publish, specialist_invocation, specialists, tools, workspace
from agentic_control_plane.specialist_state import SpecialistPhaseResult, SpecialistRunState
from agentic_control_plane.specialists import ExternalSpecialist
from agentic_control_plane.state import AuditEvent, GateRecord

logger = logging.getLogger(__name__)

DESIGN_PHASE = "design"
GENERATE_PHASE = "generate"
DESIGN_GATE = "specialist_design_review"

#: The same gate name the SDLC graph uses before it commits anything. Reused rather than given a
#: seventh name of its own: publishing generated Java and publishing generated Python are the same
#: decision - *may this change land* - and the decision consumer already understands this one.
RELEASE_GATE = "merge_release_approval"

#: The flag names control-plane fills in. Part of the contract in ADR-0018, not per-specialist
#: configuration - a specialist that does not accept them cannot be told what to read or where to
#: write, which is the whole of what an orchestrator has to say.
TENANT_REPO_FLAG = "--tenant-repo"
OUTPUT_FLAG = "--output"
DESIGN_FLAG = "--design"

#: Where `design` writes, inside the run's own workspace. `generate` writes elsewhere entirely -
#: never into the tenant checkout it reads (ADR-0009).
DESIGN_OUTPUT_DIRNAME = ".specialist-design"

#: What `design` is expected to leave behind for `generate` to consume. Named here rather than
#: discovered so a phase that silently produced nothing fails at the hand-off with a clear cause
#: instead of inside the next process.
DESIGN_ARTIFACT_NAME = "design.json"


def _failure(node: str, exc: Exception) -> dict:
    """Every failure ends the run the same way: terminal, stated, and still audited."""
    logger.error("Specialist run stopping at %s: %s", node, exc)
    return {
        "safe_stop": True,
        "run_status": "failed",
        "finished_at": datetime.now(timezone.utc),
        "events": [AuditEvent(node=node, event_type="safe_stop", detail=str(exc))],
    }


def _record(result: specialist_invocation.SpecialistResult, output_path: Path) -> SpecialistPhaseResult:
    return SpecialistPhaseResult(
        phase=result.phase,
        status=result.status,
        detail=result.detail,
        output_path=str(output_path),
        payload=result.payload,
        duration_seconds=result.duration_seconds,
    )


def _preflight(
    state: SpecialistRunState, specialist: ExternalSpecialist, phase_name: str
) -> dict | None:
    """Check what the phase declares it needs, before anything is invoked. `None` means go.

    **ADR-0016 wrote this check and nothing on this path called it.** `require_runtime`'s only
    caller was `nodes/coder.py`, on the SDLC graph - and ADR-0020 routes a specialist run away
    from that graph before it starts, so no specialist run had ever reached it. Every ADR that
    describes the preflight as guarding specialist runs was describing an intention.

    An unprovisioned deployment therefore met a missing JDK as an errno from `subprocess`, deep
    inside a phase, which is the exact outcome `require_runtime` exists to replace with a
    sentence naming what is absent.
    """
    phase = specialist.phases.get(phase_name)
    if phase is None:
        # `invoke` reports an unknown phase, and names what the specialist does declare while
        # doing so. Reporting it twice, worse, here would help nobody.
        return None
    try:
        specialists.require_runtime(state.specialist, phase_name, phase)
    except specialists.SpecialistRuntimeUnavailableError as exc:
        return _failure(phase_name, exc)
    return None


def design_node(
    state: SpecialistRunState,
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
) -> dict:
    """Run the first phase. Both its inputs resolve inside the run's own clone (ADR-0009)."""
    start = time.monotonic()
    unavailable = _preflight(state, specialist, DESIGN_PHASE)
    if unavailable is not None:
        return unavailable

    output_path = tenant_repo / DESIGN_OUTPUT_DIRNAME
    output_path.mkdir(parents=True, exist_ok=True)

    try:
        result = specialist_invocation.invoke(
            specialist=specialist,
            specialist_name=state.specialist,
            phase_name=DESIGN_PHASE,
            run_id=state.run_id,
            options={TENANT_REPO_FLAG: str(tenant_repo), OUTPUT_FLAG: str(output_path)},
            cwd=tenant_repo,
        )
    except specialist_invocation.SpecialistInvocationError as exc:
        return _failure(DESIGN_PHASE, exc)

    return {
        "phases": {DESIGN_PHASE: _record(result, output_path)},
        "events": [
            AuditEvent(
                node=DESIGN_PHASE,
                event_type="node_end",
                detail=f"specialist '{state.specialist}' produced a design: {result.detail}",
                latency_ms=(time.monotonic() - start) * 1000,
            )
        ],
    }


def design_review_gate(state: SpecialistRunState) -> dict:
    """Pause for a human on the artifact, before anything is generated from it.

    The gate exists here rather than after generation because generation is the expensive half -
    a model pass per step and a real build - and because a design is what a reviewer can actually
    read. The specialist deliberately does not decide this: its `--json` contract reports what it
    found and leaves whether that warrants a pause to control-plane (its ADR-0008).
    """
    design = state.phases.get(DESIGN_PHASE)
    decision = interrupt(
        {
            "gate_type": DESIGN_GATE,
            "run_id": state.run_id,
            "specialist": state.specialist,
            "scenario": state.scenario,
            "target_repository": state.target_repository,
            "detail": design.detail if design else "",
            # The whole artifact, unmodelled. A reviewer needs what the specialist actually said,
            # not this package's summary of it.
            "design": design.payload if design else {},
        }
    )
    status = decision.get("status", "approved")
    gates = dict(state.gates)
    gates[DESIGN_GATE] = GateRecord(
        gate_type=DESIGN_GATE,
        status=status,
        decision_payload=str(decision),
        decided_by=decision.get("decided_by", "human"),
        replayed_from_fixture=decision.get("replayed_from_fixture", False),
        timestamp=datetime.now(timezone.utc),
    )

    updates: dict = {
        "gates": gates,
        "events": [
            AuditEvent(
                node=DESIGN_GATE,
                event_type="gate_decision",
                detail=f"design review {status}",
                decision=status,
            )
        ],
    }
    if status != "approved":
        # A rejected design is a completed run, not a failed one: the governance step did its job.
        updates |= {"run_status": "completed", "finished_at": datetime.now(timezone.utc)}
    return updates


def ensure_output_checkout(
    output_path: Path, output_repository: str | None, output_branch: str
) -> None:
    """Make sure the project generated code is written into is present and is a real checkout.

    **Lazily, in the node that needs it, rather than at trigger time**, and that is not just
    tidiness: this runs after a human gate, so it routinely runs in a different process from the
    one that started the run and after any number of restarts. A clone taken at trigger time
    would have to be reconstructed here anyway.

    An existing checkout is reused rather than re-cloned - a design was approved against a
    particular state of the world, and silently replacing it mid-run would discard that.
    """
    if (output_path / ".git").is_dir():
        logger.info("Reusing the existing output checkout at %s", output_path)
        return
    if output_repository:
        workspace.clone_repository(output_path, output_repository, output_branch)
        return
    # No repository configured: a local directory the specialist writes into, and which nothing
    # publishes. `_route_after_generate` is what makes that a terminal path rather than a
    # half-finished one.
    output_path.mkdir(parents=True, exist_ok=True)


def generate_node(
    state: SpecialistRunState,
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
    output_repo: Path,
    output_repository: str | None = None,
    output_branch: str = "main",
) -> dict:
    """Run the second phase, reading the approved design and writing to the target project.

    `tenant_repo` stays the same read-only checkout `design` read. `output_repo` is a different
    repository entirely - the generated code's home, never the source it was derived from
    (ADR-0009). Getting those two the same way round is the whole of this node's care.
    """
    start = time.monotonic()
    design = state.phases.get(DESIGN_PHASE)
    if design is None:
        return _failure(GENERATE_PHASE, RuntimeError("no design phase result to generate from"))

    # Before the clone below, deliberately. `generate` declares more than `design` does - a JDK,
    # Maven, a daemon - so this is the phase where a half-provisioned deployment is caught, and
    # catching it after cloning the output repository would leave a checkout behind for an
    # environment that was never going to run.
    unavailable = _preflight(state, specialist, GENERATE_PHASE)
    if unavailable is not None:
        return unavailable

    design_artifact = Path(design.output_path) / DESIGN_ARTIFACT_NAME
    if not design_artifact.is_file():
        return _failure(
            GENERATE_PHASE,
            FileNotFoundError(
                f"the approved design is not where the design phase reported writing it "
                f"({design_artifact}). The two phases are separate processes with no shared "
                f"state, so this path is the entire hand-off between them."
            ),
        )

    try:
        ensure_output_checkout(output_repo, output_repository, output_branch)
    except workspace.CloneError as exc:
        return _failure(GENERATE_PHASE, exc)

    try:
        result = specialist_invocation.invoke(
            specialist=specialist,
            specialist_name=state.specialist,
            phase_name=GENERATE_PHASE,
            run_id=state.run_id,
            options={
                DESIGN_FLAG: str(design_artifact),
                TENANT_REPO_FLAG: str(tenant_repo),
                OUTPUT_FLAG: str(output_repo),
            },
            cwd=tenant_repo,
        )
    except specialist_invocation.SpecialistInvocationError as exc:
        return _failure(GENERATE_PHASE, exc)

    phases = dict(state.phases)
    phases[GENERATE_PHASE] = _record(result, output_repo)
    updates: dict = {
        "phases": phases,
        "output_repository": output_repository or "",
        "output_branch": output_branch if output_repository else "",
        "events": [
            AuditEvent(
                node=GENERATE_PHASE,
                event_type="node_end",
                detail=f"specialist '{state.specialist}' generated into {output_repo}: {result.detail}",
                latency_ms=(time.monotonic() - start) * 1000,
            )
        ],
    }
    if not output_repository:
        # Nothing to publish, so this is where the run ends. Marked here rather than left for the
        # router to infer, so "completed" is set by the node that knows the work is finished.
        updates |= {"run_status": "completed", "finished_at": datetime.now(timezone.utc)}
    return updates


def release_review_gate(state: SpecialistRunState) -> dict:
    """Pause on the generated change, before anything leaves this run.

    The second gate, and the one that guards something irreversible. The design gate protects the
    expensive phase; this one protects a repository. It carries the same name the SDLC graph's
    release gate carries, because it is the same decision.
    """
    generated = state.phases.get(GENERATE_PHASE)
    decision = interrupt(
        {
            "gate_type": RELEASE_GATE,
            "run_id": state.run_id,
            "specialist": state.specialist,
            "scenario": state.scenario,
            "output_repository": state.output_repository,
            "output_branch": state.output_branch,
            "detail": generated.detail if generated else "",
            # What the specialist reported about its own work - step counts, what it could not
            # generate - unmodelled, so a reviewer sees the specialist's account rather than ours.
            "generated": generated.payload if generated else {},
            "files": _generated_file_names(Path(generated.output_path)) if generated else [],
        }
    )
    status = decision.get("status", "approved")
    gates = dict(state.gates)
    gates[RELEASE_GATE] = GateRecord(
        gate_type=RELEASE_GATE,
        status=status,
        decision_payload=str(decision),
        decided_by=decision.get("decided_by", "human"),
        replayed_from_fixture=decision.get("replayed_from_fixture", False),
        timestamp=datetime.now(timezone.utc),
    )
    updates: dict = {
        "gates": gates,
        "events": [
            AuditEvent(
                node=RELEASE_GATE,
                event_type="gate_decision",
                detail=f"release review {status}",
                decision=status,
            )
        ],
    }
    if status != "approved":
        updates |= {"run_status": "completed", "finished_at": datetime.now(timezone.utc)}
    return updates


#: A reviewer needs to know what changed, not to read it in a gate payload. Bounded because a
#: generated project can be large and this crosses a checkpoint.
_MAX_LISTED_FILES = 200


def _generated_file_names(output_path: Path) -> list[str]:
    if not output_path.is_dir():
        return []
    names = sorted(
        str(path.relative_to(output_path)).replace("\\", "/")
        for path in output_path.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    return names[:_MAX_LISTED_FILES]


def publish_node(state: SpecialistRunState, *, output_repo: Path) -> dict:
    """Commit what was generated and deliver it according to PUBLISH_MODE.

    **The commit happens here and nowhere earlier**, which is the whole shape of this graph's
    safety: a `generate` that fails half-way leaves an untouched checkout rather than a reverted
    one, and there is no rollback node because there is nothing to roll back.

    Delivery failure does not fail the run. The work was generated and approved either way, and
    the outcome records what happened to it - the same posture ADR-0012 set for the SDLC path.
    """
    start = time.monotonic()
    try:
        commit_sha = tools.git_commit_all(
            output_repo, f"[agentic-sdlc] generated by '{state.specialist}' for run {state.run_id}"
        )
    except tools.GitOperationError as exc:
        return _failure("publish", exc)

    result = publish.publish_change(
        workspace=output_repo,
        run_id=state.run_id,
        repo_url=state.output_repository,
        base_branch=state.output_branch or "main",
        requirement=state.scenario,
    )
    detail = result.error or (
        f"published to {result.branch}" if result.published else f"mode={result.mode}, not published"
    )
    return {
        "run_status": "completed",
        "finished_at": datetime.now(timezone.utc),
        "commit_sha_after": commit_sha,
        "published": result.published,
        "publish_branch": result.branch or "",
        "publish_detail": detail,
        "events": [
            AuditEvent(
                node="publish",
                event_type="node_end",
                detail=f"commit {commit_sha[:8]}: {detail}",
                latency_ms=(time.monotonic() - start) * 1000,
            )
        ],
    }


def _route_after_design(state: SpecialistRunState) -> str:
    return "end" if state.safe_stop else "design_review"


def _route_after_gate(state: SpecialistRunState) -> str:
    gate = state.gates.get(DESIGN_GATE)
    if gate is not None and gate.status == "approved":
        return "generate"
    return "end"


def _route_after_generate(state: SpecialistRunState) -> str:
    if state.safe_stop:
        return "end"
    # A route that names no output repository ends here: there is nowhere to publish to, and a
    # release gate over a local directory would ask a human to approve nothing.
    return "release_review" if state.output_repository else "end"


def _route_after_release_gate(state: SpecialistRunState) -> str:
    gate = state.gates.get(RELEASE_GATE)
    if gate is not None and gate.status == "approved":
        return "publish"
    return "end"


def build_specialist_graph(
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
    output_repo: Path,
    checkpointer,
    output_repository: str | None = None,
    output_branch: str = "main",
):
    """Compile the specialist topology against one run's paths.

    Bound the same way `build_graph` binds the SDLC one - by `functools.partial` over the node
    functions - so that a resume in a different process reconstructs identical bindings from the
    run id alone.
    """
    from functools import partial

    graph = StateGraph(SpecialistRunState)
    graph.add_node(
        "design", partial(design_node, specialist=specialist, tenant_repo=tenant_repo)
    )
    graph.add_node("design_review", design_review_gate)
    graph.add_node(
        "generate",
        partial(
            generate_node,
            specialist=specialist,
            tenant_repo=tenant_repo,
            output_repo=output_repo,
            output_repository=output_repository,
            output_branch=output_branch,
        ),
    )
    graph.add_node("release_review", release_review_gate)
    graph.add_node("publish", partial(publish_node, output_repo=output_repo))

    graph.add_edge(START, "design")
    graph.add_conditional_edges(
        "design", _route_after_design, {"design_review": "design_review", "end": END}
    )
    graph.add_conditional_edges(
        "design_review", _route_after_gate, {"generate": "generate", "end": END}
    )
    graph.add_conditional_edges(
        "generate", _route_after_generate, {"release_review": "release_review", "end": END}
    )
    graph.add_conditional_edges(
        "release_review", _route_after_release_gate, {"publish": "publish", "end": END}
    )
    graph.add_edge("publish", END)

    return graph.compile(checkpointer=checkpointer)
