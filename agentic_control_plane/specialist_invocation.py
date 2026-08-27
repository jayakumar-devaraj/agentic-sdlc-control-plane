"""Runs an external specialist as a subprocess, over a contract this repo defines.

`specialists.py` decides *which* specialist handles a run and whether the environment can
support it. This module is the other half: it builds the command line, runs it, and turns what
comes back into something the graph can act on.

**The contract is generic, and that is the point** (ADR-0018). Nothing here knows what any
specialist does. What it requires is that one exists at all:

- the phase's own subcommand and any static flags come from `config/scenario_specialists.yaml`,
  so every tenant-specific word stays data (ADR-0001, ADR-0016);
- control-plane appends the four flags only it can fill - `--tenant-repo`, `--output`,
  `--run-id`, `--json`;
- the specialist replies on **stdout** with one JSON object carrying `status`, `phase`,
  `run_id` and `detail`, and puts everything else on stderr.

Everything past those four fields is carried through as an opaque `dict`. A specialist that
reports twelve fields and one that reports five both work here, and neither needs a change in
this package - which is what stops a second specialist from being a second code path.

**Parsing is strict, deliberately.** `nodes/coder.py` parses the `claude` CLI leniently because
that CLI genuinely narrates around its answer and cannot be told not to. A specialist is held to
a contract instead: unparseable stdout is a broken specialist, and reporting that is more useful
than salvaging a brace-delimited substring and continuing on a guess.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from agentic_control_plane.specialists import ExternalSpecialist, SpecialistPhase

logger = logging.getLogger(__name__)

#: Flags control-plane always appends. A specialist that does not accept them cannot join the
#: audit chain (`--run-id`) or be read by a machine (`--json`), so they are part of the contract
#: rather than per-specialist configuration.
RUN_ID_FLAG = "--run-id"
JSON_FLAG = "--json"

#: The envelope every specialist must return, whatever else it returns.
REQUIRED_FIELDS = ("status", "phase", "run_id", "detail")


class SpecialistInvocationError(RuntimeError):
    """Base for every way invoking a specialist can fail."""


class SpecialistProtocolError(SpecialistInvocationError):
    """The specialist ran but did not answer in the shape the contract requires."""


class SpecialistFailedError(SpecialistInvocationError):
    """The specialist ran, answered correctly, and reported that its own work failed."""


class SpecialistResult(BaseModel):
    """One completed specialist invocation.

    `payload` is the whole decoded object, opaque on purpose: PR #9 established that the durable
    checkpointer round-trips an arbitrary dict unchanged, so an artifact can cross a human gate
    without this package having a schema for it.
    """

    status: str
    phase: str
    run_id: str
    detail: str = ""
    payload: dict = Field(default_factory=dict)
    duration_seconds: float = 0.0


def build_argv(
    specialist: ExternalSpecialist,
    phase: SpecialistPhase,
    *,
    run_id: str,
    options: Mapping[str, str | Sequence[str]],
) -> list[str]:
    """The command line, in one place so a test can assert it without running anything.

    Order matters for readability in an audit log rather than for correctness: the subcommand and
    its configured flags first, exactly as an operator wrote them, then what control-plane knows.
    """
    argv: list[str] = [specialist.command, *phase.args]
    for flag, value in options.items():
        argv.append(flag)
        if isinstance(value, str):
            argv.append(value)
        else:
            argv.extend(value)
    argv += [RUN_ID_FLAG, run_id, JSON_FLAG]
    return argv


def _decode(stdout: str, stderr: str, returncode: int, argv: Sequence[str]) -> dict:
    text = stdout.strip()
    if not text:
        raise SpecialistProtocolError(
            f"`{' '.join(argv[:2])}` exited {returncode} and wrote nothing to stdout. "
            f"The contract is one JSON object on stdout with logs on stderr (ADR-0018). "
            f"stderr: {stderr.strip()[-400:] or '<empty>'}"
        )
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SpecialistProtocolError(
            f"`{' '.join(argv[:2])}` exited {returncode} and its stdout is not JSON: {exc}. "
            f"stdout starts: {text[:200]!r}"
        ) from exc
    if not isinstance(decoded, dict):
        raise SpecialistProtocolError(
            f"`{' '.join(argv[:2])}` returned a {type(decoded).__name__} on stdout, not an object"
        )
    missing = [field for field in REQUIRED_FIELDS if field not in decoded]
    if missing:
        raise SpecialistProtocolError(
            f"`{' '.join(argv[:2])}` returned an object missing {missing}. Every specialist must "
            f"report {list(REQUIRED_FIELDS)} whatever else it reports (ADR-0018)."
        )
    return decoded


def invoke(
    *,
    specialist: ExternalSpecialist,
    specialist_name: str,
    phase_name: str,
    run_id: str,
    options: Mapping[str, str | Sequence[str]],
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> SpecialistResult:
    """Run one phase of a specialist and return what it reported.

    Raises rather than returning a failure result, so the caller's existing safe-stop handles
    every branch the same way - a specialist that fails, one that answers unintelligibly, one
    that hangs and one that is not installed all end the run in a defined state with a reason.
    """
    phase = specialist.phases.get(phase_name)
    if phase is None:
        raise SpecialistInvocationError(
            f"specialist '{specialist_name}' has no phase '{phase_name}'; it declares "
            f"{sorted(specialist.phases)}"
        )

    argv = build_argv(specialist, phase, run_id=run_id, options=options)
    logger.info(
        "Invoking specialist '%s' phase '%s' for run %s: %s",
        specialist_name,
        phase_name,
        run_id,
        " ".join(argv),
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=phase.timeout_seconds,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        # Distinct from the preflight in specialists.require_runtime, which checks what a phase
        # *declares* it needs. This is the specialist's own entrypoint being absent - the wheel
        # was never installed, or was installed somewhere off PATH.
        raise SpecialistInvocationError(
            f"specialist '{specialist_name}' declares command '{specialist.command}', which is "
            f"not on PATH. Install it - see `distribution` in the routing table - or build an "
            f"image that carries it (ADR-0017)."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SpecialistInvocationError(
            f"specialist '{specialist_name}' phase '{phase_name}' exceeded "
            f"{phase.timeout_seconds}s and was killed. Raise `timeout_seconds` for this phase in "
            f"the routing table if the work legitimately takes longer."
        ) from exc

    duration = time.monotonic() - start
    decoded = _decode(proc.stdout, proc.stderr, proc.returncode, argv)

    result = SpecialistResult(
        status=str(decoded["status"]),
        phase=str(decoded["phase"]),
        run_id=str(decoded["run_id"]),
        detail=str(decoded["detail"]),
        payload=decoded,
        duration_seconds=duration,
    )

    if result.status != "ok":
        raise SpecialistFailedError(
            f"specialist '{specialist_name}' phase '{phase_name}' reported "
            f"status={result.status!r}: {result.detail}"
        )

    # A zero-status answer with a non-zero exit is a contradiction, and guessing which half to
    # believe would hide a real defect in whichever specialist does it.
    if proc.returncode != 0:
        raise SpecialistProtocolError(
            f"specialist '{specialist_name}' phase '{phase_name}' reported status='ok' but "
            f"exited {proc.returncode}. One of the two is wrong and this is not the place to "
            f"decide which."
        )

    logger.info(
        "Specialist '%s' phase '%s' completed in %.1fs: %s",
        specialist_name,
        phase_name,
        duration,
        result.detail,
    )
    return result
