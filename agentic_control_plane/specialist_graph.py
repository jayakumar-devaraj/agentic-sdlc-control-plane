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

    START -> design -> (gate: specialist_design_review) -> generate -> END
                            |
                            +-- rejected -> END

`design` and `generate` are separate processes with no shared state, which is why the approved
artifact travels on the state rather than being re-derived: the path written by the first phase is
handed to the second verbatim.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agentic_control_plane import specialist_invocation
from agentic_control_plane.specialist_state import SpecialistPhaseResult, SpecialistRunState
from agentic_control_plane.specialists import ExternalSpecialist
from agentic_control_plane.state import AuditEvent, GateRecord

logger = logging.getLogger(__name__)

DESIGN_PHASE = "design"
GENERATE_PHASE = "generate"
DESIGN_GATE = "specialist_design_review"

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


def design_node(
    state: SpecialistRunState,
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
) -> dict:
    """Run the first phase. Both its inputs resolve inside the run's own clone (ADR-0009)."""
    start = time.monotonic()
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


def generate_node(
    state: SpecialistRunState,
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
    output_repo: Path,
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

    output_repo.mkdir(parents=True, exist_ok=True)
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
    return {
        "phases": phases,
        "run_status": "completed",
        "finished_at": datetime.now(timezone.utc),
        "events": [
            AuditEvent(
                node=GENERATE_PHASE,
                event_type="node_end",
                detail=f"specialist '{state.specialist}' generated into {output_repo}: {result.detail}",
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


def build_specialist_graph(
    *,
    specialist: ExternalSpecialist,
    tenant_repo: Path,
    output_repo: Path,
    checkpointer,
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
        ),
    )

    graph.add_edge(START, "design")
    graph.add_conditional_edges(
        "design", _route_after_design, {"design_review": "design_review", "end": END}
    )
    graph.add_conditional_edges(
        "design_review", _route_after_gate, {"generate": "generate", "end": END}
    )
    graph.add_edge("generate", END)

    return graph.compile(checkpointer=checkpointer)
