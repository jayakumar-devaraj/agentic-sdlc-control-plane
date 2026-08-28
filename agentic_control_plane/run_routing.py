"""Decides which graph a run executes, at the moment the run is created.

**Upstream of the graph, deliberately** (ADR-0020). The obvious place to branch was the `coder`
node, since that is where the routing table was first consulted. It is the wrong place, and the
graph says so: `codebase_reasoner` raises `codebase_impact_review` for every brownfield run, and a
drift-triggered modernization run is brownfield. Branching at `coder` therefore means a specialist
run first executes three SDLC-shaped nodes against a repository they were never written for, then
**parks at a human gate about a Python codebase-impact analysis**, and only once someone approves
that irrelevant gate does it reach `coder` and discover what it always was.

The trigger handler already has everything the decision needs. It clones the target before starting
anything, so the workspace's `origin` - the identity routing matches on (ADR-0016) - exists exactly
where the decision belongs.

Two directions have to agree, and they are asymmetric:

- **Starting** a run: resolve the route from the fresh clone.
- **Resuming** one: the clone may be long gone and the process is often a different one, so the
  decision is recovered from the durable checkpoint instead - read straight from the saver rather
  than through a graph, because building a graph is the thing being decided.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel

from agentic_control_plane import runner, specialists, tools, workspace
from agentic_control_plane.specialist_graph import build_specialist_graph
from agentic_control_plane.specialist_state import SpecialistRunState
from agentic_control_plane.specialists import ExternalSpecialist, Resolution
from agentic_control_plane.state import GraphState, OrchestratorMode, ScenarioType

logger = logging.getLogger(__name__)

#: Field present on `SpecialistRunState` and absent from `GraphState`. Which graph a checkpoint
#: belongs to is read from this rather than stored beside it: a second record of the same fact is
#: a second thing that can disagree with the first.
_SPECIALIST_DISCRIMINATOR = "specialist"


def is_specialist_run(values: Mapping[str, object]) -> bool:
    """Whether these graph values came from a specialist run rather than an SDLC one.

    The same read `graph_for_resume` does, exposed for callers that already hold a run's values
    and have no checkpoint to consult. One function answers this so a second caller cannot
    invent a second way of asking - which is the same reason the discriminator above is read off
    the state instead of being stored beside it.
    """
    return _SPECIALIST_DISCRIMINATOR in values


def specialist_output_root() -> Path:
    """Where a specialist's generated output is written.

    **Not a subdirectory of `WORKSPACES_ROOT`**, and that is the whole reason this function exists
    rather than a path expression at the call site. Reconciliation sweeps the workspaces root and
    deletes any directory whose run has reached a terminal state; an output directory sitting
    beside the clones looks exactly like an unrecognised run to that sweep. ADR-0009 records this
    happening once already, to the audit trail, which is why it does not live there either.
    """
    return Path(os.environ.get("SPECIALIST_OUTPUT_ROOT", "/specialist-output"))


def output_path_for(run_id: str) -> Path:
    return specialist_output_root() / run_id


def route_for_workspace(workspace_path: Path, table=None) -> Resolution:
    """Which specialist handles the target cloned into `workspace_path`."""
    routing_table = table if table is not None else specialists.load_routing_table()
    repository = specialists.repository_name(tools.git_remote_url(workspace_path))
    resolution = specialists.resolve(routing_table, repository)
    logger.info(
        "Run target %s routed to specialist '%s'%s",
        repository or "<unidentified>",
        resolution.name,
        f" for scenario '{resolution.scenario}'" if resolution.scenario else "",
    )
    return resolution


def specialist_graph_factory(
    resolution: Resolution, workspace_path: Path, output_path: Path
) -> runner.GraphFactory:
    """A factory closing over one run's paths, in the shape `runner` expects.

    `run_id` is ignored on purpose: the caller resolved the paths from it already, and a factory
    that re-derived them could disagree with the state it is about to load.
    """
    specialist = resolution.specialist
    assert isinstance(specialist, ExternalSpecialist)  # guarded by resolve_plan below

    def factory(run_id: str, checkpointer):
        return build_specialist_graph(
            specialist=specialist,
            tenant_repo=workspace_path,
            output_repo=output_path,
            checkpointer=checkpointer,
            output_repository=resolution.output_repository,
            output_branch=resolution.output_branch,
        )

    return factory


def plan_for_trigger(
    *,
    run_id: str,
    scenario_type: ScenarioType,
    requirement: str,
    mode: OrchestratorMode,
    table=None,
) -> tuple[BaseModel, runner.GraphFactory | None]:
    """The initial state and the graph to run it on, for a run that is starting.

    Returns `None` for the factory on the built-in path rather than naming the SDLC factory, so
    that an unrouted run reaches `runner` exactly as it did before this module existed.
    """
    workspace_path = workspace.workspace_for(run_id)
    resolution = route_for_workspace(workspace_path, table)

    if not isinstance(resolution.specialist, ExternalSpecialist):
        return (
            GraphState(scenario_type=scenario_type, requirement_raw=requirement, mode=mode),
            None,
        )

    state = SpecialistRunState(
        run_id=run_id,
        specialist=resolution.name,
        scenario=resolution.scenario,
        target_repository=specialists.repository_name(tools.git_remote_url(workspace_path)) or "",
    )
    factory = specialist_graph_factory(resolution, workspace_path, output_path_for(run_id))
    return state, factory


def _stored_channel_values(run_id: str, checkpointer) -> dict:
    """Read a checkpoint without compiling a graph to do it.

    `runner.snapshot_for` needs a graph, and which graph to build is precisely the question here,
    so this goes to the saver directly. Every `BaseCheckpointSaver` implements `get_tuple`, which
    is what makes this work identically against the in-memory saver and the Postgres one.
    """
    try:
        stored = checkpointer.get_tuple({"configurable": {"thread_id": run_id}})
    except Exception:  # noqa: BLE001 - an unreadable checkpoint is "unknown", not a crash here
        logger.exception("Could not read the checkpoint for run %s while routing it", run_id)
        return {}
    if stored is None:
        return {}
    return stored.checkpoint.get("channel_values", {}) or {}


def graph_factory_for_existing_run(run_id: str, checkpointer, table=None) -> runner.GraphFactory | None:
    """Rebuild the right graph for a run that already exists, from its checkpoint alone.

    A resume commonly happens in a different process from the one that started the run - that is
    the point of a durable checkpointer - so nothing in memory can be relied on. Returns `None`
    for an SDLC run, and for a run this cannot identify: the default is the graph that existed
    first, which is also the safe answer for a checkpoint written before specialist runs existed.
    """
    values = _stored_channel_values(run_id, checkpointer)
    if _SPECIALIST_DISCRIMINATOR not in values:
        return None

    workspace_path = workspace.workspace_for(run_id)
    resolution = route_for_workspace(workspace_path, table)
    if not isinstance(resolution.specialist, ExternalSpecialist):
        # The routing table changed under a parked run. Refusing is the only honest answer: the
        # run holds a design produced by a specialist that no longer handles this target, and
        # generating from it with something else would silently substitute one for the other.
        raise RoutingChangedError(
            f"run {run_id} was started as a specialist run, and its target now routes to "
            f"'{resolution.name}'. Restore the routing table it was started under, or abandon "
            f"the run - resuming it would generate from a design this specialist did not write."
        )
    return specialist_graph_factory(resolution, workspace_path, output_path_for(run_id))


class RoutingChangedError(RuntimeError):
    """Raised when a parked run's target no longer routes where it did when it started."""
