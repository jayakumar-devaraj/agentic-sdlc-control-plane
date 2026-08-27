"""Executes a run to its next human gate, parks it, and resumes it later.

This module is what makes a gate a real pause rather than a blocked thread. The graph's gate nodes
already call `interrupt()`; what did not exist was a way to answer one without
holding a thread open. The behaviour being replaced drove gates with a synchronous
in-process loop that read a recorded decision and immediately resumed - fine for a
scripted run, unusable when the answer comes from a human minutes or hours later.

The rule that makes the difference: **nothing here waits for a decision.**
`start_run` returns as soon as the graph parks, and `resume_run` is a separate call
made later, by whichever process happens to read the decision. Between the two, the
run exists only in Postgres. That is what lets the Kafka consumer return to polling
immediately - a poll loop blocked across a human decision would exceed
`max.poll.interval.ms`, trigger a rebalance, and take every other in-flight run on
that partition with it.

Because parked state is durable and not consumer-local, the process that resumes a
run need not be the one that started it. A rebalance, a restart, or a redeploy in
between changes nothing.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.types import Command

from pydantic import BaseModel

from agentic_control_plane import workspace
from agentic_control_plane.graph import build_graph

logger = logging.getLogger(__name__)

# How long a run may sit parked before the sweep gives up on it. A parked run holds
# a workspace and a checkpoint indefinitely otherwise, and "waiting forever" is not
# a state an operator can act on.
DEFAULT_PARKED_TTL_HOURS = 24


def parked_ttl() -> timedelta:
    return timedelta(hours=float(os.environ.get("PARKED_RUN_TTL_HOURS", DEFAULT_PARKED_TTL_HOURS)))


def fixtures_dir() -> Path:
    return Path(os.environ.get("FIXTURES_DIR", "/fixtures"))


class UnknownRunError(KeyError):
    """No checkpoint exists for the run a decision refers to."""


class RunAlreadyTerminalError(ValueError):
    """The run a decision refers to has already finished.

    Distinct types rather than bare KeyError/ValueError because the caller has to
    tell these two - which are expected and benign - apart from the same builtin
    types raised from inside graph execution, which are neither. Catching the
    builtins around the whole resume call silently relabelled real failures as
    "already terminal" and dropped them.
    """


@dataclass
class RunResult:
    """What happened to a run during one non-blocking slice of execution."""

    run_id: str
    parked: bool = False
    gate_type: str | None = None
    gate_payload: dict | None = None
    terminal_state: str | None = None
    detail: str = ""
    values: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.terminal_state is not None


def _config(run_id: str) -> dict:
    """thread_id == run_id. The correlation that lets a decision published minutes

    later find the run it belongs to, with no lookup table in between.
    """
    return {"configurable": {"thread_id": run_id}}


def sdlc_graph_factory(run_id: str, checkpointer):
    """This module's original and default graph: the SDLC workflow.

    The workspace path is derived from run_id rather than stored, so a resume in a
    different process reconstructs exactly the same binding without consulting
    anything but the run_id it was given.
    """
    return build_graph(workspace.workspace_for(run_id), fixtures_dir(), checkpointer)


GraphFactory = Callable[[str, object], object]


def _compiled_for(run_id: str, checkpointer, graph_factory: GraphFactory | None = None):
    """Rebuild the compiled graph for a run.

    `graph_factory` is what makes this module host more than one topology (ADR-0019). It defaults
    to the SDLC graph, so every existing caller is unchanged and a caller that wants the other one
    says so. Nothing below this line reads a field either graph does not have: `_interpret` and
    `_terminal_state_of` classify on `safe_stop` and `run_status`, which are lifecycle rather than
    workflow, and both states carry them.
    """
    factory = graph_factory or sdlc_graph_factory
    return factory(run_id, checkpointer)


def _terminal_state_of(values: dict) -> tuple[str, str]:
    """Classify a finished run. Returns (terminal_state, detail)."""
    if values.get("safe_stop"):
        return "safe_stop", "run safe-stopped"
    run_status = values.get("run_status")
    if run_status == "completed":
        return "completed", "run completed"
    return "failed", f"run finished with status {run_status!r}"


def _interpret(run_id: str, result: dict, snapshot) -> RunResult:
    """Turn one invoke() return into a RunResult - parked, or terminal."""
    if "__interrupt__" in result and result["__interrupt__"]:
        payload = result["__interrupt__"][0].value
        gate_type = payload.get("gate_type") if isinstance(payload, dict) else None
        logger.info("Run %s parked at gate %s", run_id, gate_type)
        return RunResult(
            run_id=run_id,
            parked=True,
            gate_type=gate_type,
            gate_payload=payload if isinstance(payload, dict) else {"value": payload},
            values=snapshot.values if snapshot else {},
        )

    values = snapshot.values if snapshot else result
    terminal_state, detail = _terminal_state_of(values)
    logger.info("Run %s reached terminal state %s", run_id, terminal_state)
    return RunResult(
        run_id=run_id, terminal_state=terminal_state, detail=detail, values=values
    )


def snapshot_for(run_id: str, checkpointer, graph_factory: GraphFactory | None = None):
    return _compiled_for(run_id, checkpointer, graph_factory).get_state(_config(run_id))


def already_known(run_id: str, checkpointer, graph_factory: GraphFactory | None = None) -> bool:
    """Has this run_id ever been checkpointed?

    The idempotency check. Drift events are delivered at least once, and the
    correlation_id a producer derives from the drift condition is stable across
    redeliveries - so a second delivery of the same condition arrives with a run_id
    that already exists here, and must be acknowledged rather than run again.
    """
    return snapshot_for(run_id, checkpointer, graph_factory).created_at is not None


def is_resumable(run_id: str, checkpointer, graph_factory: GraphFactory | None = None) -> bool:
    """Could this run still legitimately continue?

    True while work remains - including a run parked at a gate, whose `next` names
    the interrupted node. False both for a run that finished and for one that was
    never checkpointed; the caller that needs to tell those apart uses
    `already_known`. Reconciliation deliberately does not, because both answers mean
    the same thing there: the workspace is safe to delete.
    """
    snapshot = snapshot_for(run_id, checkpointer, graph_factory)
    return snapshot.created_at is not None and bool(snapshot.next)


def parked_since(run_id: str, checkpointer, graph_factory: GraphFactory | None = None) -> datetime | None:
    """When the current parked checkpoint was written, for TTL purposes."""
    snapshot = snapshot_for(run_id, checkpointer, graph_factory)
    if snapshot.created_at is None or not snapshot.next:
        return None
    return datetime.fromisoformat(snapshot.created_at)


def start_run(
    run_id: str,
    initial_state: BaseModel,
    checkpointer,
    graph_factory: GraphFactory | None = None,
) -> RunResult:
    """Run from the start until the graph parks at a gate or reaches a terminal state.

    Returns either way. Never waits for a decision.
    """
    compiled = _compiled_for(run_id, checkpointer, graph_factory)
    config = _config(run_id)
    logger.info("Starting run %s (%s)", run_id, type(initial_state).__name__)
    result = compiled.invoke(initial_state, config=config)
    return _interpret(run_id, result, compiled.get_state(config))


def resume_run(
    run_id: str,
    decision: dict,
    checkpointer,
    graph_factory: GraphFactory | None = None,
) -> RunResult:
    """Resume a parked run with a human decision, until the next gate or the end.

    A run may pass through several gates, so this returning `parked=True` again is
    the normal case, not an error.
    """
    compiled = _compiled_for(run_id, checkpointer, graph_factory)
    config = _config(run_id)
    snapshot = compiled.get_state(config)
    if snapshot.created_at is None:
        raise UnknownRunError(f"no checkpoint exists for run {run_id}")
    if not snapshot.next:
        raise RunAlreadyTerminalError(f"run {run_id} has already reached a terminal state")

    logger.info(
        "Resuming run %s with decision %s by %s",
        run_id,
        decision.get("status"),
        decision.get("decided_by_identity", decision.get("decided_by")),
    )
    result = compiled.invoke(Command(resume=decision), config=config)
    return _interpret(run_id, result, compiled.get_state(config))


def find_stale_runs(run_ids: list[str], checkpointer, now: datetime | None = None) -> list[str]:
    """Which of these runs have been parked longer than the TTL.

    A human decision that never arrives would otherwise hold a workspace and a
    checkpoint forever. Expiring them makes the outcome visible - the caller emits a
    `stale` outcome event - rather than leaving a run silently stuck.
    """
    now = now or datetime.now(timezone.utc)
    ttl = parked_ttl()
    stale: list[str] = []
    for run_id in run_ids:
        since = parked_since(run_id, checkpointer)
        if since is not None and now - since > ttl:
            stale.append(run_id)
    return stale
