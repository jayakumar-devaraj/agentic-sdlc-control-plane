"""Tier markers, derived from the directory a test lives in rather than written in it.

CI selects by marker, so a test carrying no tier marker is collected, reported in the
"passed" count, and never actually executed by the gate that was supposed to run it.
``--strict-markers`` does not catch this: it catches a *misspelled* marker, not a
missing one. In agentic-sdlc-eventbus that silently deselected 18 tests and dropped a
module to 69% coverage while the run printed "99 passed".

Deriving the marker from the parent directory means a new file cannot forget one, and
the hard failure below means a test dropped outside the four tiers is a collection
error rather than a test that quietly never runs.

**This file deliberately holds no shared fixtures**, which is where it departs from the
eventbus conftest it is adapted from. Three fixture names in this repository are defined
twice with different bodies - ``env`` (test_main.py and test_runner.py), ``origin``
(test_publish.py and test_consumer.py) and ``workspace`` (test_graph_integration.py and
test_nodes_coder.py). A root conftest is visible to every test below it, so hoisting any
of them here would shadow the other definition silently, in whichever module pytest
resolved second. Fixtures stay in the modules that own them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

TIERS = ("unit", "contract", "integration", "evaluation")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every test the tier marker for its directory, and refuse strays."""
    strays = []
    for item in items:
        tier = next((part for part in item.path.parts if part in TIERS), None)
        if tier is None:
            strays.append(str(item.path.relative_to(REPO_ROOT)))
            continue
        item.add_marker(getattr(pytest.mark, tier))

    if strays:
        raise pytest.UsageError(
            "these test files are not in a tier directory, so a marker-filtered run "
            f"would never execute them: {sorted(set(strays))}. "
            f"Move each into tests/{{{','.join(TIERS)}}}/."
        )
