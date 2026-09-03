"""Tests for the durable delivery target.

The property under test is the one ADR 0026 closes: where a run's change is delivered
survives the process that started the run. A parked run routinely waits longer than the
process does, so an in-memory stand-in here would assert that the code runs, not that the
target survives - which is exactly the mistake ADR 0026 was written about.

These share a database with a possibly-running control plane, so every test scopes itself
to run ids it created and cleans up only those. A `TRUNCATE` here would delete a live
service's record of where to deliver its in-flight runs.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from agentic_control_plane.checkpointer import _postgres_conn_string
from agentic_control_plane.run_targets import (
    InMemoryRunTargets,
    PostgresRunTargets,
    RunTarget,
)


def _postgres_reachable() -> bool:
    if not os.environ.get("POSTGRES_USER"):
        return False
    try:
        with psycopg.connect(_postgres_conn_string(), connect_timeout=3):
            return True
    except psycopg.OperationalError:
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(), reason="needs a reachable Postgres; see the README"
)


@pytest.fixture
def targets():
    """A real store, with only this test's own rows removed afterwards."""
    store = PostgresRunTargets(_postgres_conn_string())
    store.setup()
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    yield store, prefix
    with psycopg.connect(_postgres_conn_string(), connect_timeout=3) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM run_targets WHERE run_id LIKE %s", (f"{prefix}%",))


def _target(commit: str | None = None) -> RunTarget:
    return RunTarget(
        repo_url="https://example.invalid/target-service.git",
        branch="main",
        scenario_type="brownfield",
        commit_sha_before=commit,
    )


def test_a_recorded_target_is_readable_by_a_second_store(targets):
    """The property that matters: a different object, as a different process would be."""
    store, prefix = targets
    run_id = f"{prefix}-1"
    store.record(run_id, _target(commit="abc1234"))

    fresh = PostgresRunTargets(_postgres_conn_string())
    recovered = fresh.get(run_id)

    assert recovered == _target(commit="abc1234")


def test_recording_twice_fills_in_the_commit_rather_than_colliding(targets):
    """The clone path records before and after; the second write must not fail."""
    store, prefix = targets
    run_id = f"{prefix}-2"
    store.record(run_id, _target())
    assert store.get(run_id).commit_sha_before is None

    store.record(run_id, _target(commit="def5678"))
    assert store.get(run_id).commit_sha_before == "def5678"


def test_an_unknown_run_reads_as_none(targets):
    store, prefix = targets
    assert store.get(f"{prefix}-never-recorded") is None


def test_forget_removes_only_that_run(targets):
    store, prefix = targets
    store.record(f"{prefix}-keep", _target())
    store.record(f"{prefix}-drop", _target())

    store.forget(f"{prefix}-drop")

    assert store.get(f"{prefix}-drop") is None
    assert store.get(f"{prefix}-keep") is not None


def test_forget_of_an_unknown_run_is_not_an_error(targets):
    store, prefix = targets
    store.forget(f"{prefix}-never-recorded")


def test_a_read_failure_degrades_rather_than_raising():
    """`get` is called while publishing an outcome; raising there would fail a finished run."""
    unreachable = PostgresRunTargets("postgresql://nobody@127.0.0.1:1/nothing")
    assert unreachable.get("any-run") is None


def test_a_forget_failure_degrades_rather_than_raising():
    unreachable = PostgresRunTargets("postgresql://nobody@127.0.0.1:1/nothing")
    unreachable.forget("any-run")


def test_recording_raises_when_it_cannot_write():
    """The opposite policy, deliberately: an unrecorded target must refuse the run."""
    unreachable = PostgresRunTargets("postgresql://nobody@127.0.0.1:1/nothing")
    with pytest.raises(Exception):
        unreachable.record("any-run", _target())


def test_the_in_memory_store_satisfies_the_same_surface():
    store = InMemoryRunTargets()
    store.setup()
    store.record("run-1", _target(commit="aaa"))
    assert store.get("run-1") == _target(commit="aaa")
    store.forget("run-1")
    assert store.get("run-1") is None
    store.forget("run-1")
