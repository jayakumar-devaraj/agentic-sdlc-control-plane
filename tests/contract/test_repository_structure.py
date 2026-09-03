"""The repository layout is a contract too, and this is what holds it.

A structure that exists only in a README is a suggestion - this repository's own README
claimed "257 tests" against a suite of 399, and claimed CI ran on push against a workflow
with no push trigger, both for some time and at no cost, because prose cannot fail.

It lives in the contract tier because breaking the layout breaks a consumer: the next
contributor, who inherits a tree that is *mostly* the documented one with no way to tell
which parts were deliberate.

Deliberately about **shape, never content**. It does not care what an ADR argues, only that
decisions get recorded as numbered ADRs; not what a spec says, only that it carries the
sections the workflow depends on. See ADR-0028.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "agentic_control_plane"
TIERS = ("unit", "contract", "integration", "evaluation")


# --- the package -----------------------------------------------------------------------


def test_the_package_lives_at_the_repository_root():
    # NOT under src/, which is the reference layout this repo deliberately departs from.
    # src/ exists to stop tests importing the working tree instead of the installed wheel.
    # Nothing installs this package - both Dockerfiles rely on WORKDIR /app with COPY . . -
    # so it would protect against nothing and cost both container builds. See ADR-0028.
    assert PACKAGE.is_dir()
    assert not (REPO / "src").exists(), "the package must not move under src/ - see ADR-0028"


def test_the_routing_table_sits_beside_the_package_not_inside_it():
    # specialists.py resolves it at Path(__file__).resolve().parents[1] / "config", and
    # CI's tenant-vocabulary grep scopes itself around exactly this boundary: tenant names
    # are data the package READS, never something the package CONTAINS (ADR-0001, ADR-0016).
    assert (REPO / "config" / "scenario_specialists.yaml").is_file()
    assert not (PACKAGE / "config").exists(), (
        "config/ inside the package would make tenant vocabulary part of it - see ADR-0016"
    )


def test_the_package_carries_no_pep561_marker():
    # Its absence is the decision. A py.typed marker exists so an INSTALLED package delivers
    # its types; nothing installs this one, so the file would do nothing but imply otherwise.
    assert not (PACKAGE / "py.typed").exists()


def test_every_module_the_runtime_needs_is_present():
    expected = {
        "__init__.py",
        "main.py",
        "heartbeat.py",
        "graph.py",
        "runner.py",
        "checkpointer.py",
        "consumer.py",
        "specialists.py",
    }
    assert expected <= {p.name for p in PACKAGE.glob("*.py")}
    assert (PACKAGE / "nodes" / "__init__.py").is_file()


# --- tests -----------------------------------------------------------------------------


def test_every_test_lives_in_one_of_the_four_tiers():
    # conftest.py turns this into a collection error at runtime; this asserts the layout
    # that makes that possible.
    strays = [p.name for p in (REPO / "tests").rglob("test_*.py") if p.parent.name not in TIERS]
    assert strays == [], f"move these into tests/{{{','.join(TIERS)}}}/: {strays}"


@pytest.mark.parametrize("tier", TIERS)
def test_each_tier_exists_and_is_not_empty(tier):
    directory = REPO / "tests" / tier
    assert directory.is_dir(), f"missing tier: tests/{tier}/"
    assert list(directory.glob("test_*.py")), f"tests/{tier}/ has no tests"


def test_the_conftest_derives_markers_rather_than_trusting_them():
    # The specific failure: a test with no tier marker is collected, counted in "passed",
    # and never run by a marker-filtered command. --strict-markers does not catch a MISSING
    # marker, only a misspelled one.
    source = (REPO / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "pytest_collection_modifyitems" in source
    assert "UsageError" in source, "a stray test must be a hard error, not a warning"


def test_the_evaluation_tier_carries_its_verification_report():
    # For this repository the report outranks the coverage number: five defects in it were
    # found by real runs while a fully green unit suite passed throughout.
    assert (REPO / "tests" / "evaluation" / "REPORT.md").is_file()


def test_the_postgres_exception_in_the_unit_tier_has_not_grown():
    # A BOUNDED, documented exception - five tests needing a real database sit in modules
    # that are otherwise pure unit tests (four in test_consumer.py, one in test_runner.py).
    # Separating them means splitting files rather than moving them, which changes what the
    # tests do. Pinning the count means a sixth has to argue for itself instead of arriving
    # quietly. See README.md -> Testing and ADR-0028.
    # Matched across the whole decorator rather than line by line: test_runner.py writes its
    # gate as a multi-line `@pytest.mark.skipif(\n    not _postgres_reachable(), ...)`, so a
    # per-line check finds "skipif" and "postgres" on different lines and counts four.
    gate = re.compile(r"@needs_postgres\b|@pytest\.mark\.skipif\([^)]*_postgres_reachable")
    gated = sum(
        len(gate.findall(path.read_text(encoding="utf-8")))
        for path in (REPO / "tests" / "unit").glob("test_*.py")
    )
    assert gated == 5, (
        f"tests/unit/ has {gated} Postgres-gated tests, not the 5 recorded as a bounded "
        "exception. A new one belongs in tests/integration/ or tests/evaluation/."
    )


# --- decisions and governance ------------------------------------------------------------


def test_adrs_are_numbered_without_gaps_or_duplicates():
    # A gap means an ADR was deleted rather than superseded, and a superseded decision still
    # has to be readable or the record is worse than none.
    adrs = (REPO / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")
    numbers = sorted(int(p.name[:4]) for p in adrs)
    assert numbers, "docs/adr/ must hold at least ADR 0001"
    assert numbers == list(range(1, len(numbers) + 1)), f"ADR numbering has a gap: {numbers}"


def test_every_adr_appears_in_the_index():
    # docs/README.md listed ADRs 0001-0010 while 27 existed. Shape, not content: this asserts
    # each ADR has a row, never what the row says.
    index = (REPO / "docs" / "README.md").read_text(encoding="utf-8")
    missing = [
        p.name
        for p in (REPO / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md")
        if p.name not in index
    ]
    assert missing == [], f"not indexed in docs/README.md: {missing}"


def test_the_constitution_and_every_template_are_present():
    assert (REPO / ".specify" / "memory" / "constitution.md").is_file()
    templates = REPO / ".specify" / "templates"
    for name in ("spec", "plan", "tasks"):
        assert (templates / f"{name}-template.md").is_file(), f"missing {name}-template.md"


def test_every_spec_directory_is_numbered_and_complete():
    specs = sorted(d for d in (REPO / "specs").iterdir() if d.is_dir())
    assert specs, "specs/ must hold at least the record of how this layout arrived"
    numbers = []
    for spec in specs:
        assert spec.name[:3].isdigit(), f"{spec.name} must start with a three-digit number"
        numbers.append(spec.name[:3])
        for required in ("spec.md", "plan.md", "tasks.md"):
            assert (spec / required).is_file(), f"{spec.name} is missing {required}"
    assert len(numbers) == len(set(numbers)), f"duplicate spec numbers: {numbers}"


# --- required files ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "CLAUDE.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "pyproject.toml",
        "requirements.txt",
        "requirements.lock",
        "docker-compose.yml",
        "docker-compose.specialist.yml",
        "Dockerfile",
        "Dockerfile.specialist",
        ".env.example",
        ".gitignore",
        ".dockerignore",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
        "scripts/check_lock_is_current.py",
        "scripts/validate-mermaid.mjs",
    ],
)
def test_required_file_exists(path):
    assert (REPO / path).is_file(), f"missing {path}"


@pytest.mark.parametrize("workflow", ["ci.yml", "security.yml"])
def test_required_workflow_exists(workflow):
    assert (REPO / ".github" / "workflows" / workflow).is_file()


def test_the_config_files_that_were_folded_are_gone():
    # pytest.ini and .coveragerc became [tool.*] sections. Two files reappearing means the
    # configuration has two homes again, and the one pytest ignores drifts silently.
    assert not (REPO / "pytest.ini").exists()
    assert not (REPO / ".coveragerc").exists()


def test_pyproject_configures_tools_without_declaring_a_distribution():
    # [project] or [build-system] here would invite a src/ move. See ADR-0028.
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in text
    assert "[tool.coverage.run]" in text
    assert "\n[project]" not in text, "this repository is an application, not a distributable"
    assert "\n[build-system]" not in text


# --- secrets and local overrides ----------------------------------------------------------


def test_only_example_secret_templates_are_tracked():
    # Asks GIT, not the filesystem. A developer who has run Quickstart has real
    # secrets/github_pat.txt and secrets/postgres_password.txt sitting right there - that is
    # correct and expected. The rule is about what is COMMITTED, and an ls-based check would
    # fail on every configured machine while passing in CI, which is precisely backwards.
    tracked = subprocess.run(
        ["git", "ls-files", "secrets/"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    real = [name for name in tracked if not name.endswith(".example")]
    assert real == [], f"only .example templates belong under secrets/: {real}"


def test_local_compose_overrides_and_env_are_ignored_not_committed():
    # Compose merges .env and docker-compose.override.yml automatically from beside
    # docker-compose.yml. .env is where OTEL_EXPORTER_OTLP_HEADERS is supplied to the
    # specialist override, and that header carries a credential.
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignored
    assert "docker-compose.override.yml" in ignored


def test_every_variable_the_compose_files_read_is_in_the_example():
    # Shape, not values: an operator must not have to discover a required variable by
    # watching compose fail.
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    declared = {m.group(1) for m in re.finditer(r"^([A-Z_]+)=", example, re.MULTILINE)}
    read: set[str] = set()
    for name in ("docker-compose.yml", "docker-compose.specialist.yml"):
        text = (REPO / name).read_text(encoding="utf-8")
        read |= set(re.findall(r"\$\{([A-Z_]+)", text))
    assert read <= declared, f"not documented in .env.example: {sorted(read - declared)}"


# --- documentation shape ------------------------------------------------------------------


def test_the_readme_sections_are_in_the_order_the_standard_fixes():
    # CLAUDE.md fixes this order. The README says how to RUN this repo; why lives in ADRs.
    expected = [
        "Tech stack",
        "Architecture",
        "Quickstart",
        "Local development",
        "Testing",
        "Deployment / CI",
    ]
    text = (REPO / "README.md").read_text(encoding="utf-8")
    found = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert found == expected, f"README top-level sections are {found}"


def test_python_scripts_use_underscores_so_they_can_be_imported():
    # Conditional rather than a required-file list: scripts/ here holds PowerShell demo
    # drivers and one .mjs validator, whose hyphens are fine. The rule applies to .py only.
    offenders = [p.name for p in (REPO / "scripts").glob("*.py") if "-" in p.stem]
    assert offenders == [], f"rename to underscores: {offenders}"
