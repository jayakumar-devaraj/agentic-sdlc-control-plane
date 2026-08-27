"""State for a specialist run - the smallest thing that can carry a two-phase hand-off.

Deliberately **not** `GraphState` (ADR-0019). That model is the SDLC workflow's: roughly half its
thirty fields describe a requirement being clarified, decomposed, coded and tested, and none of
them mean anything to a run that hands work to an external tool and gates the artifact that comes
back. Reusing it would have meant carrying sixteen fields that are permanently empty and, worse,
inheriting a graph whose nodes read them.

What *is* reused is the run/gate/audit machinery, imported rather than copied: `AuditEvent`,
`GateRecord`, `GateType` and `RunStatus` are lifecycle, not SDLC, and a second definition of them
would be a second audit vocabulary for one ledger.

**The eventual tidier shape is a split of `state.py`** into a generic run/gate/audit base with the
SDLC fields as a subclass - the audit's own sizing calls that item 2, bounded at one 189-line file
and seventeen test files. It is deliberately not attempted here: nothing in this change needs it,
and doing it in the same PR as a new graph would put a thirty-field refactor and a new execution
path in one diff.
"""

from __future__ import annotations

import operator
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from agentic_control_plane.state import AuditEvent, GateRecord, GateType, RunStatus, _utc_now


class SpecialistPhaseResult(BaseModel):
    """What one invoked phase reported, kept per phase so the trail survives the next one.

    `payload` is the specialist's whole JSON answer, opaque by design: control-plane requires four
    envelope fields (ADR-0018) and stores the rest without a schema. PR #9 established that the
    durable checkpointer round-trips exactly this - an arbitrary dict, across two independent
    saver contexts, intact and readable before resumption - which is what lets an artifact cross a
    human gate without this package modelling it.
    """

    phase: str
    status: str = ""
    detail: str = ""
    output_path: str = ""
    payload: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0


class SpecialistRunState(BaseModel):
    """The whole state of one specialist run.

    The three lifecycle fields `run_status`, `safe_stop` and `finished_at` carry the names
    `runner.py` already classifies on, so its terminal-state reading works here unchanged rather
    than by special case.
    """

    run_id: str
    #: Which entry in the routing table handled this run, and the label it was routed under.
    #: Names of configured data, never of a tenant - the vocabulary stays in `config/`.
    specialist: str
    scenario: str = ""
    target_repository: str = ""

    run_status: RunStatus = "running"
    safe_stop: bool = False

    gates: dict[GateType, GateRecord] = Field(default_factory=dict)

    #: Results keyed by phase name, so a resumed run can tell what already ran. A dict rather than
    #: an appending list because re-running a phase supersedes it rather than adding to it.
    phases: dict[str, SpecialistPhaseResult] = Field(default_factory=dict)

    #: The project generated code is written into, and what happened to it. Empty when the route
    #: names none, which is the case where nothing is published at all.
    output_repository: str = ""
    output_branch: str = ""
    #: Set only once the release gate approved and a commit was made, so its presence is the
    #: answer to "did anything leave this run" rather than something to be inferred from counters.
    commit_sha_after: str | None = None
    published: bool = False
    publish_branch: str = ""
    publish_detail: str = ""

    events: Annotated[list[AuditEvent], operator.add] = Field(default_factory=list)

    started_at: datetime = Field(default_factory=_utc_now)
    finished_at: datetime | None = None
