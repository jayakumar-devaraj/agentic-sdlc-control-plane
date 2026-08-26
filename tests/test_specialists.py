"""Tests for the specialist/model routing table (ADR-0016).

Every table these tests build is synthetic and its vocabulary is invented, which is
deliberate rather than incidental: CI fails the build when `tests/` carries any
tenant's words, and the whole point of the design is that the resolver never sees them
either. The one test that touches the *shipped* table asserts its structure and says
nothing about what is in it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_control_plane import specialists
from agentic_control_plane.specialists import (
    DEFAULT_CLI_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    BuiltinSpecialist,
    ExternalSpecialist,
    RuntimeRequirements,
    SpecialistConfigError,
    SpecialistPhase,
    SpecialistRuntimeUnavailableError,
    load_routing_table,
    missing_requirements,
    repository_name,
    require_runtime,
    resolve,
    routing_file,
)

MINIMAL = """
version: 1
specialists:
  default:
    kind: builtin
"""

TWO_SPECIALISTS = """
version: 1
specialists:
  default:
    kind: builtin
    model: some-model-id
    cli_timeout_seconds: 90
  widget-migrator:
    kind: external
    command: widget-migrator
    distribution: "widget-migrator @ git+https://example.invalid/widget-migrator@v1.2.3"
    phases:
      plan:
        args: [plan]
        requires:
          executables: [some-cli]
      build:
        args: [build, --strict]
        requires:
          executables: [some-cli, some-compiler]
          docker_daemon: true
routes:
  - scenario: widget-modernisation
    repository: Widget-Service
    specialist: widget-migrator
"""


def write_table(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "routing.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# --- an absent or empty table is the old behaviour, exactly ------------------------


def test_absent_file_yields_the_previous_hardcoded_behaviour(tmp_path: Path):
    """The refactor must not change what a deployment that never configures it does."""
    table = load_routing_table(tmp_path / "nothing-here.yaml")

    resolution = resolve(table, None)
    assert isinstance(resolution.specialist, BuiltinSpecialist)
    assert resolution.specialist.model == DEFAULT_MODEL
    assert resolution.specialist.cli_timeout_seconds == DEFAULT_CLI_TIMEOUT_SECONDS


def test_empty_file_is_read_as_route_nothing_specially(tmp_path: Path):
    table = load_routing_table(write_table(tmp_path, "\n"))
    assert resolve(table, "anything").name == "default"


def test_minimal_table_fills_in_the_previous_constants(tmp_path: Path):
    table = load_routing_table(write_table(tmp_path, MINIMAL))
    builtin = table.specialists["default"]
    assert builtin.model == DEFAULT_MODEL
    assert builtin.cli_timeout_seconds == DEFAULT_CLI_TIMEOUT_SECONDS


# --- where the table is found -----------------------------------------------------


def test_routing_file_defaults_beside_the_package_not_inside_it():
    path = routing_file()
    assert path.name == "scenario_specialists.yaml"
    assert path.parent.name == "config"
    # The rule the whole design rests on: no tenant vocabulary inside the package.
    assert "agentic_control_plane" not in path.parent.parts


def test_env_var_overrides_the_default_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    elsewhere = write_table(tmp_path, MINIMAL)
    monkeypatch.setenv("SPECIALIST_ROUTING_FILE", str(elsewhere))
    assert routing_file() == elsewhere
    assert load_routing_table().specialists["default"].kind == "builtin"


# --- the table this repository actually ships -------------------------------------


def test_the_shipped_table_is_valid_and_keeps_the_historical_defaults():
    """Structure only. What the shipped table routes is data, and not this test's business."""
    table = load_routing_table()

    builtin = table.specialists["default"]
    assert isinstance(builtin, BuiltinSpecialist)
    assert builtin.model == DEFAULT_MODEL
    assert builtin.cli_timeout_seconds == DEFAULT_CLI_TIMEOUT_SECONDS

    for route in table.routes:
        assert route.specialist in table.specialists
    for name, specialist in table.specialists.items():
        if isinstance(specialist, ExternalSpecialist):
            assert specialist.phases, name
            for phase in specialist.phases.values():
                assert phase.args


def test_every_shipped_route_resolves_to_the_specialist_it_names():
    table = load_routing_table()
    for route in table.routes:
        resolution = resolve(table, route.repository)
        assert resolution.name == route.specialist
        assert resolution.scenario == route.scenario


# --- a bad table fails at load, naming the file -----------------------------------


def test_unparseable_yaml_names_the_file(tmp_path: Path):
    path = write_table(tmp_path, "version: 1\n  bad: [indent\n")
    with pytest.raises(SpecialistConfigError, match=str(path.name)):
        load_routing_table(path)


def test_a_scalar_document_is_not_a_table(tmp_path: Path):
    with pytest.raises(SpecialistConfigError, match="must contain a mapping"):
        load_routing_table(write_table(tmp_path, "just a string\n"))


def test_an_unknown_key_is_rejected_rather_than_ignored(tmp_path: Path):
    """A silently-ignored typo in a key is a route that quietly never applies."""
    text = MINIMAL + "rowtes:\n  - scenario: x\n"
    with pytest.raises(SpecialistConfigError):
        load_routing_table(write_table(tmp_path, text))


def test_a_table_with_no_default_is_rejected(tmp_path: Path):
    text = """
version: 1
specialists:
  widget-migrator:
    kind: external
    command: widget-migrator
    phases:
      plan:
        args: [plan]
"""
    with pytest.raises(SpecialistConfigError, match="defines no 'default'"):
        load_routing_table(write_table(tmp_path, text))


def test_an_external_default_is_rejected(tmp_path: Path):
    text = """
version: 1
specialists:
  default:
    kind: external
    command: widget-migrator
    phases:
      plan:
        args: [plan]
"""
    with pytest.raises(SpecialistConfigError, match="must be kind: builtin"):
        load_routing_table(write_table(tmp_path, text))


def test_a_route_naming_an_undefined_specialist_is_rejected(tmp_path: Path):
    text = MINIMAL + """
routes:
  - scenario: widget-modernisation
    repository: widget-service
    specialist: widgt-migrator
"""
    with pytest.raises(SpecialistConfigError, match="not defined"):
        load_routing_table(write_table(tmp_path, text))


def test_one_repository_routed_twice_is_rejected(tmp_path: Path):
    text = TWO_SPECIALISTS + """
  - scenario: widget-modernisation-again
    repository: widget-service
    specialist: default
"""
    with pytest.raises(SpecialistConfigError, match="routed twice"):
        load_routing_table(write_table(tmp_path, text))


def test_a_zero_timeout_is_rejected(tmp_path: Path):
    text = "version: 1\nspecialists:\n  default:\n    kind: builtin\n    cli_timeout_seconds: 0\n"
    with pytest.raises(SpecialistConfigError):
        load_routing_table(write_table(tmp_path, text))


# --- matching the target repository -----------------------------------------------


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://example.invalid/owner/widget-service.git", "widget-service"),
        ("https://example.invalid/owner/widget-service", "widget-service"),
        ("https://example.invalid/owner/widget-service/", "widget-service"),
        ("git@example.invalid:owner/widget-service.git", "widget-service"),
        ("git@example.invalid:widget-service.git", "widget-service"),
        ("C:\\checkouts\\widget-service", "widget-service"),
        ("", None),
        (None, None),
    ],
)
def test_repository_name_is_spelling_independent(remote: str | None, expected: str | None):
    assert repository_name(remote) == expected


def test_a_routed_repository_resolves_to_its_specialist(tmp_path: Path):
    table = load_routing_table(write_table(tmp_path, TWO_SPECIALISTS))
    resolution = resolve(table, "widget-service")
    assert resolution.name == "widget-migrator"
    assert resolution.scenario == "widget-modernisation"
    assert isinstance(resolution.specialist, ExternalSpecialist)
    assert resolution.specialist.phases["build"].args == ["build", "--strict"]


def test_matching_ignores_case_on_both_sides(tmp_path: Path):
    """The table says Widget-Service; a remote URL is whatever the producer wrote."""
    table = load_routing_table(write_table(tmp_path, TWO_SPECIALISTS))
    assert resolve(table, "WIDGET-service").name == "widget-migrator"


def test_an_unrouted_repository_takes_the_default(tmp_path: Path):
    table = load_routing_table(write_table(tmp_path, TWO_SPECIALISTS))
    resolution = resolve(table, "some-other-service")
    assert resolution.name == "default"
    assert resolution.scenario == ""


def test_an_unidentified_target_takes_the_default(tmp_path: Path):
    table = load_routing_table(write_table(tmp_path, TWO_SPECIALISTS))
    assert resolve(table, None).name == "default"


# --- the runtime a specialist declares --------------------------------------------


def test_nothing_is_missing_when_every_executable_is_present(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: f"/usr/bin/{name}")
    requires = RuntimeRequirements(executables=["some-cli", "some-compiler"])
    assert missing_requirements(requires) == []


def test_missing_executables_are_reported_by_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        specialists.shutil, "which", lambda name: None if name == "some-compiler" else "/usr/bin/x"
    )
    requires = RuntimeRequirements(executables=["some-cli", "some-compiler"])
    assert missing_requirements(requires) == ["some-compiler"]


def test_a_daemon_requirement_is_a_probe_not_a_path_lookup(monkeypatch: pytest.MonkeyPatch):
    """`docker` on PATH inside a container proves nothing about a reachable daemon."""
    monkeypatch.setattr(specialists.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        specialists.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=1, stdout="", stderr="no"),
    )
    assert missing_requirements(RuntimeRequirements(docker_daemon=True)) == [
        "a reachable Docker daemon"
    ]


def test_a_reachable_daemon_satisfies_the_requirement(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        specialists.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], returncode=0, stdout="29.6.1", stderr=""),
    )
    assert missing_requirements(RuntimeRequirements(docker_daemon=True)) == []


def test_no_docker_client_at_all_is_a_missing_daemon(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: None)
    assert missing_requirements(RuntimeRequirements(docker_daemon=True)) == [
        "a reachable Docker daemon"
    ]


def test_a_probe_that_cannot_run_is_a_missing_daemon(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: "/usr/bin/docker")

    def explode(*args, **kwargs):
        raise OSError("socket is gone")

    monkeypatch.setattr(specialists.subprocess, "run", explode)
    assert missing_requirements(RuntimeRequirements(docker_daemon=True)) == [
        "a reachable Docker daemon"
    ]


def test_a_probe_that_hangs_is_a_missing_daemon(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: "/usr/bin/docker")

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker info", timeout=10)

    monkeypatch.setattr(specialists.subprocess, "run", time_out)
    assert missing_requirements(RuntimeRequirements(docker_daemon=True)) == [
        "a reachable Docker daemon"
    ]


def test_require_runtime_explains_the_remedy_rather_than_raising_an_errno(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: None)
    phase = SpecialistPhase(args=["build"], requires=RuntimeRequirements(executables=["some-compiler"]))

    with pytest.raises(SpecialistRuntimeUnavailableError) as excinfo:
        require_runtime("widget-migrator", "build", phase)

    message = str(excinfo.value)
    assert "widget-migrator" in message and "build" in message
    assert "some-compiler" in message
    assert "customised image" in message


def test_require_runtime_is_silent_when_the_environment_satisfies_the_phase(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: f"/usr/bin/{name}")
    phase = SpecialistPhase(args=["plan"], requires=RuntimeRequirements(executables=["some-cli"]))
    require_runtime("widget-migrator", "plan", phase)


def test_a_phase_with_no_declared_requirements_needs_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(specialists.shutil, "which", lambda name: None)
    require_runtime("widget-migrator", "plan", SpecialistPhase(args=["plan"]))
