"""Resolves which generator handles a run, and with what model, from a config file.

The Coder node used to hold its whole routing policy in two module constants - a model
id and a CLI timeout - which is enough for exactly one tenant generating code exactly
one way. This module replaces that with a table read from
`config/scenario_specialists.yaml` (ADR-0016).

Nothing here knows any tenant's name, and that is a requirement rather than a
preference: ADR-0001 makes this package domain-agnostic and CI fails the build when it
is not. Tenant vocabulary lives in the config file as data; this module parses,
validates and matches it.

Three properties are load-bearing and each has a test:

- **An absent file is not an error.** It yields a table holding only the built-in
  default, whose values are the constants that were in `coder.py` before this existed,
  so a deployment that never creates the file behaves exactly as it did.
- **A malformed file fails at load**, naming the file and the offending entry, rather
  than deferring a typo in a specialist name to whichever run first matches that route.
- **An external specialist's runtime requirements are declared and checked**, so a
  missing JDK is reported by name instead of arriving as an errno from `subprocess`.
  The shipped image is `python:3.12-slim` and carries none of a specialist's runtime;
  this follows `coder.py`'s existing posture for the `claude` CLI, where a capable
  deployment is a customised image rather than a config toggle.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_SPECIALIST = "default"

# Previously CLAUDE_MODEL and CLAUDE_CLI_TIMEOUT_SECONDS in nodes/coder.py. They are
# the defaults here so that an absent or minimal config reproduces the old behaviour
# rather than merely resembling it.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_CLI_TIMEOUT_SECONDS = 480

_DOCKER_PROBE_TIMEOUT_SECONDS = 10


class SpecialistConfigError(RuntimeError):
    """Raised when the routing table cannot be read, validated or resolved."""


class SpecialistRuntimeUnavailableError(RuntimeError):
    """Raised when the environment cannot supply what a specialist phase declared."""


class RuntimeRequirements(BaseModel):
    """What must exist in the invoking environment before a phase can run."""

    model_config = ConfigDict(extra="forbid")

    executables: list[str] = Field(default_factory=list)
    docker_daemon: bool = False


class SpecialistPhase(BaseModel):
    """One bounded subcommand of an external specialist, and what it needs to run.

    `timeout_seconds` is per phase because phases are not comparable: one that asks a model for a
    document and one that drives a compiler through a repair loop differ by an order of
    magnitude, and a single number for both is either too tight for the second or useless for the
    first. The default is deliberately generous - a phase killed early leaves a half-written
    output directory, which is worse than waiting.
    """

    model_config = ConfigDict(extra="forbid")

    args: list[str] = Field(min_length=1)
    requires: RuntimeRequirements = Field(default_factory=RuntimeRequirements)
    timeout_seconds: int = Field(default=1800, gt=0)


class BuiltinSpecialist(BaseModel):
    """The generator in the Coder node: one LLM call for a {path: contents} object."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["builtin"] = "builtin"
    model: str = DEFAULT_MODEL
    cli_timeout_seconds: int = Field(default=DEFAULT_CLI_TIMEOUT_SECONDS, gt=0)


class ExternalSpecialist(BaseModel):
    """A separately released tool invoked as a subprocess, with named phases.

    `distribution` is how the invoking environment obtains it - a pip requirement
    pinned to an immutable ref. Optional because a specialist may be provisioned into
    an image rather than installed per-run, and because recording a placeholder pin
    would be worse than recording none.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["external"]
    command: str = Field(min_length=1)
    distribution: str | None = None
    phases: dict[str, SpecialistPhase] = Field(min_length=1)


Specialist = Annotated[
    Union[BuiltinSpecialist, ExternalSpecialist], Field(discriminator="kind")
]


class Route(BaseModel):
    """One target repository, and the specialist that handles it.

    `scenario` is a label carried into the audit trail so a run can say *why* it was
    routed the way it was. It selects nothing; `repository` does the matching.
    """

    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    specialist: str = Field(min_length=1)

    #: Where generated code is written - a *different* project from the one matched above, which
    #: is read-only for the whole run (ADR-0009). Optional: a route without one still runs, and
    #: its specialist writes to a local directory that is never published. That is the honest
    #: default, because publishing to a repository nobody named is the one mistake here that
    #: cannot be undone by deleting a directory.
    output_repository: str | None = None
    output_branch: str = "main"


class RoutingTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    specialists: dict[str, Specialist]
    routes: list[Route] = Field(default_factory=list)


class Resolution(BaseModel):
    """Which specialist a run got, under which name, why, and where its output belongs."""

    scenario: str = ""
    name: str
    specialist: Specialist
    #: Copied from the matched route so a caller never has to hold the route as well. Empty on
    #: the default resolution, which is what "nothing is published" looks like.
    output_repository: str | None = None
    output_branch: str = "main"


def default_routing_table() -> RoutingTable:
    """The table an absent config file yields: the built-in generator, nothing else."""
    return RoutingTable(specialists={DEFAULT_SPECIALIST: BuiltinSpecialist()})


def routing_file() -> Path:
    """Where the routing table lives.

    `config/` sits beside the package, not inside it, so that CI's tenant-vocabulary
    guard - scoped to the package and its tests - stays true while the table names
    tenants. The image is built by `COPY . .` into /app, so the same relative position
    holds there; this repository is not installed as a wheel, which is the one case
    that would break a `parents[...]` walk out of the package - see ADR-0016 § 1,
    where a sibling repository is recorded doing exactly that.
    """
    override = os.environ.get("SPECIALIST_ROUTING_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / "config" / "scenario_specialists.yaml"


def load_routing_table(path: Path | None = None) -> RoutingTable:
    """Read, parse and validate the routing table. An absent file is not an error."""
    path = path or routing_file()
    if not path.is_file():
        logger.info("No specialist routing table at %s; using the built-in default", path)
        return default_routing_table()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SpecialistConfigError(f"{path} could not be read as YAML: {exc}") from exc

    # An empty file parses to None, which is a plausible way to say "route nothing
    # specially" - treat it as the default table rather than as a broken document.
    if raw is None:
        return default_routing_table()
    if not isinstance(raw, dict):
        raise SpecialistConfigError(
            f"{path} must contain a mapping, not {type(raw).__name__}"
        )

    try:
        table = RoutingTable.model_validate(raw)
    except ValidationError as exc:
        raise SpecialistConfigError(f"{path} is not a valid routing table: {exc}") from exc

    _check_referential_integrity(table, path)
    return table


def _check_referential_integrity(table: RoutingTable, path: Path) -> None:
    """The checks pydantic cannot express, each one a real failure mode.

    A route naming a specialist that does not exist, and two routes claiming the same
    repository, are both typos that would otherwise surface as a mis-routed run rather
    than as a bad config - one silently taking the default, the other taking whichever
    entry happened to be listed first.
    """
    default = table.specialists.get(DEFAULT_SPECIALIST)
    if default is None:
        raise SpecialistConfigError(
            f"{path} defines no '{DEFAULT_SPECIALIST}' specialist; it is what any "
            "target with no matching route resolves to"
        )
    if not isinstance(default, BuiltinSpecialist):
        raise SpecialistConfigError(
            f"{path} makes '{DEFAULT_SPECIALIST}' an external specialist. The default "
            "is the generator this repo runs itself, so it must be kind: builtin"
        )

    seen: dict[str, str] = {}
    for route in table.routes:
        if route.specialist not in table.specialists:
            raise SpecialistConfigError(
                f"{path}: route for scenario '{route.scenario}' names specialist "
                f"'{route.specialist}', which is not defined"
            )
        key = route.repository.casefold()
        if key in seen:
            raise SpecialistConfigError(
                f"{path}: repository '{route.repository}' is routed twice - by "
                f"scenario '{seen[key]}' and by '{route.scenario}'"
            )
        seen[key] = route.scenario


def repository_name(remote_url: str | None) -> str | None:
    """The repository a remote URL points at, spelling-independent.

    `https://host/o/r.git`, `https://host/o/r/` and `git@host:o/r.git` are one
    repository. Matching on the whole URL would mean a table that has to enumerate
    spellings, and therefore one that silently fails to match.
    """
    if not remote_url:
        return None
    tail = remote_url.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # An scp-style remote whose path has no separator at all: git@host:repo.git
    tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail or None


def resolve(table: RoutingTable, repository: str | None) -> Resolution:
    """Which specialist handles this target. An unidentified target takes the default.

    That is the correct answer rather than a fallback: a workspace with no origin
    remote - a fresh `git init`, which is what the suite and the local quickstart
    produce - is a target this platform cannot identify, and a general-purpose
    generator is what an unidentified target should get.
    """
    if repository:
        wanted = repository.casefold()
        for route in table.routes:
            if route.repository.casefold() == wanted:
                return Resolution(
                    scenario=route.scenario,
                    name=route.specialist,
                    specialist=table.specialists[route.specialist],
                    output_repository=route.output_repository,
                    output_branch=route.output_branch,
                )
    return Resolution(
        name=DEFAULT_SPECIALIST, specialist=table.specialists[DEFAULT_SPECIALIST]
    )


#: Where a mounted daemon socket conventionally lives, when `DOCKER_HOST` does not say otherwise.
_DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"


def _docker_socket_path() -> str | None:
    """The unix socket a daemon would answer on, or None when it is reached some other way.

    `DOCKER_HOST=tcp://...` and docker contexts are the other ways, and only a client knows how
    to follow them - hence None rather than a guess.
    """
    host = os.environ.get("DOCKER_HOST", "")
    if host.startswith("unix://"):
        return host[len("unix://") :]
    if host:
        return None
    return _DEFAULT_DOCKER_SOCKET


def _daemon_answers_on_socket() -> bool:
    """Ask the daemon directly, over the socket, the way a library client does.

    **This is what the docstring below has always promised and what the client lookup could not
    deliver.** A deployment that mounts the host's socket (ADR-0017 § 5, ADR-0022 § 3) has a
    reachable daemon and frequently no `docker` binary at all - the CLI is a client, nothing in
    the image needs one, and Testcontainers speaks to the socket over HTTP rather than shelling
    out. Observed on the first real specialist deployment: the daemon returned `200` on
    `/version` from inside the container while this function reported it absent.
    """
    path = _docker_socket_path()
    af_unix = getattr(socket, "AF_UNIX", None)
    if path is None or af_unix is None or not Path(path).exists():
        return False
    try:
        with socket.socket(af_unix, socket.SOCK_STREAM) as sock:
            sock.settimeout(_DOCKER_PROBE_TIMEOUT_SECONDS)
            sock.connect(path)
            sock.sendall(b"GET /_ping HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n\r\n")
            return b"200 OK" in sock.recv(256)
    except OSError:
        return False


def _docker_daemon_reachable() -> bool:
    """Probe the daemon, not the client: `docker` on PATH proves nothing about it."""
    if _daemon_answers_on_socket():
        return True
    # A daemon reached any other way - `DOCKER_HOST=tcp://...`, a context, a remote - is only
    # reachable through the client, so ask it rather than declaring the daemon absent.
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=_DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def missing_requirements(requires: RuntimeRequirements) -> list[str]:
    """What the environment does not supply, named the way an operator would name it."""
    missing = [name for name in requires.executables if shutil.which(name) is None]
    if requires.docker_daemon and not _docker_daemon_reachable():
        missing.append("a reachable Docker daemon")
    return missing


def require_runtime(specialist_name: str, phase_name: str, phase: SpecialistPhase) -> None:
    """Fail with what is absent and what to do about it, before anything is invoked.

    Same posture as the Coder node's `_require_claude_cli`, and for the same reason:
    the shipped image installs none of this deliberately, so the expected outcome of
    routing to a specialist on a stock image deserves an explanation rather than an
    errno from `subprocess`.
    """
    missing = missing_requirements(phase.requires)
    if not missing:
        return
    raise SpecialistRuntimeUnavailableError(
        f"specialist '{specialist_name}' phase '{phase_name}' needs "
        f"{', '.join(missing)}, which this environment does not provide. The shipped "
        "image is python:3.12-slim and carries no specialist runtime; a "
        "specialist-capable deployment is a customised image, not a config toggle "
        "(ADR-0016)."
    )
