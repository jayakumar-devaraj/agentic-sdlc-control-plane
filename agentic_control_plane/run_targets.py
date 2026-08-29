"""Where a run's change is delivered, kept for longer than the process that started it.

A run reaches its terminal state on a later slice than the one that created it - after a
gate, in a different call, and routinely in a different process, because a parked run may
wait `PARKED_RUN_TTL_HOURS` (24 by default) for a human. Publishing that change needs four
facts captured at clone time: the repository, the branch, the scenario, and the commit the
clone started from.

**Holding them on the worker object was not enough, and looked like it was.** They lived in
a `dict` on `Worker`, whose own docstring claimed that holding them there "stops it being
lost in between" a gate. It stops it being lost between slices of one process. It does not
survive a restart, which is the ordinary case for a run parked overnight - and the failure
is silent in the worst way: the run reports `completed`, the outcome event carries
`repo_url: "unknown"`, the change is never published, and `workspace.cleanup` then deletes
the only copy of the commit. Observed end to end, not reasoned about: see
[ADR 0026](../docs/adr/0026-a-runs-delivery-target-outlives-its-process.md).

This is the same shape as [ADR 0010](../docs/adr/0010-durable-work-hand-off.md), one level
up, and it is deliberately the same mechanism: a small table in this service's own Postgres,
a connection per operation, and a row whose lifetime is the run's.

**Two implementations, and the in-memory one is not a stand-in for tests.** It is the
behaviour a caller gets when no store is supplied, which keeps `Worker` constructible
without Postgres exactly as `inbox` does. What changed is that the running control plane no
longer gets it by default - `main` wires the durable one, and a deployment that skips it is
choosing the old behaviour rather than inheriting it.

**Failure policy differs by operation, on purpose.** Recording raises, because a run whose
delivery target was not written down is a run that cannot be delivered, and starting it
anyway is the loss this module exists to prevent. Reading and forgetting never raise: both
are called while an outcome is being published, and turning a bookkeeping problem into a
failed run would be a worse trade than the degraded report they fall back to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import psycopg

logger = logging.getLogger(__name__)

#: Matches `inbox.CONNECT_TIMEOUT_SECONDS`, and for the same reason: an unreachable Postgres
#: must surface as a quick refusal rather than stalling the worker.
CONNECT_TIMEOUT_SECONDS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS run_targets (
    run_id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    branch TEXT NOT NULL,
    scenario_type TEXT NOT NULL,
    commit_sha_before TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


@dataclass
class RunTarget:
    """What an outcome event needs, once the run's workspace is gone.

    `commit_sha_before` is nullable because it is only knowable after a successful clone,
    and the clone-failure path still has to publish an outcome naming what it failed to
    clone. The target is therefore recorded twice: once before the clone so that path has
    a repository to report, and once after it with the commit filled in.
    """

    repo_url: str
    branch: str
    scenario_type: str
    commit_sha_before: str | None = None


class InMemoryRunTargets:
    """Process-local targets. Correct for a single-process lifetime, and nothing longer.

    Kept as a real implementation rather than deleted, because `Worker` must stay
    constructible without Postgres. Anything that outlives a process wants the durable one.
    """

    def __init__(self) -> None:
        self._targets: dict[str, RunTarget] = {}

    def setup(self) -> None:
        """No-op. Present so callers need not know which implementation they hold."""

    def record(self, run_id: str, target: RunTarget) -> None:
        self._targets[run_id] = target

    def get(self, run_id: str) -> RunTarget | None:
        return self._targets.get(run_id)

    def forget(self, run_id: str) -> None:
        self._targets.pop(run_id, None)


class PostgresRunTargets:
    """Targets that outlive the process, in this service's own database."""

    def __init__(self, conn_string: str) -> None:
        self.conn_string = conn_string

    def _connect(self):
        return psycopg.connect(self.conn_string, connect_timeout=CONNECT_TIMEOUT_SECONDS)

    def setup(self) -> None:
        """Create the table if absent. Idempotent, called on every startup."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(SCHEMA)

    def record(self, run_id: str, target: RunTarget) -> None:
        """Persist a run's delivery target. Raises if it cannot.

        An upsert rather than an insert, because the clone fills in `commit_sha_before`
        after the row already exists, and because a redelivered trigger for a run already
        in the checkpointer must not fail on a primary-key collision.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_targets
                    (run_id, repo_url, branch, scenario_type, commit_sha_before)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    repo_url = EXCLUDED.repo_url,
                    branch = EXCLUDED.branch,
                    scenario_type = EXCLUDED.scenario_type,
                    commit_sha_before = EXCLUDED.commit_sha_before
                """,
                (
                    run_id,
                    target.repo_url,
                    target.branch,
                    target.scenario_type,
                    target.commit_sha_before,
                ),
            )

    def get(self, run_id: str) -> RunTarget | None:
        """The run's target, or None if it was never recorded or cannot be read.

        Never raises: the caller is publishing an outcome, and its fallback for an absent
        target is a degraded report rather than a crash. An unreachable database is logged
        so the degradation is not silent - which was the whole complaint against the
        in-memory version.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT repo_url, branch, scenario_type, commit_sha_before "
                    "FROM run_targets WHERE run_id = %s",
                    (run_id,),
                )
                row = cur.fetchone()
        except Exception:
            logger.exception("could not read the delivery target for run %s", run_id)
            return None
        if row is None:
            return None
        return RunTarget(repo_url=row[0], branch=row[1], scenario_type=row[2], commit_sha_before=row[3])

    def forget(self, run_id: str) -> None:
        """Retire a finished run's target.

        Logged rather than raised, for the same reason `Inbox.discard` is: the run is over,
        and the cost of a leftover row is a row, not a wrong outcome.
        """
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM run_targets WHERE run_id = %s", (run_id,))
        except Exception:
            logger.exception("could not forget the delivery target for run %s", run_id)
