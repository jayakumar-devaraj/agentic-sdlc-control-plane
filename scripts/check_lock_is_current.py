"""Fail if requirements.lock has drifted from requirements.txt.

WHY NOT JUST RECOMPILE AND DIFF. `uv pip compile` resolves transitive dependencies to
the newest compatible versions available *at the moment it runs*. requirements.txt pins
its nine direct dependencies with `==` but says nothing about the forty-one transitive
ones, so a byte-for-byte diff against a fresh compile turns green or red depending on
whether anything downstream published that morning. A gate that fails for reasons
unrelated to the change in front of it gets switched off, which is worse than no gate.

So this checks the property that actually matters and is stable: **every version pinned
in requirements.txt appears in requirements.lock at that same version.** That catches the
real regression - someone edits a pin and forgets to regenerate the lock - and stays
quiet when the world moves underneath an unchanged repository.

WHAT THIS DOES NOT CLAIM. The lock is not the install path. Both Dockerfiles install from
requirements.txt, and this change deliberately did not touch them. The lock records what a
resolution of those pins actually produced - including transitive versions and hashes that
requirements.txt does not state, and including the immutable commit SHA that the mutable
`agentic-events ... @v0.1.0` tag resolved to. That is reproducibility documentation plus
this gate, not a guarantee about what a container installed.

Run directly, or via the `ci` workflow:

    python scripts/check_lock_is_current.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO / "requirements.txt"
LOCK = REPO / "requirements.lock"

# `name==version`, with optional extras: `psycopg[binary]==3.2.3`. Comments and the
# `agentic-events @ git+...` line are not this shape and are skipped deliberately - a git
# dependency has no version to compare, and the lock records its resolved SHA instead.
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;#]+)")


def _pins(text: str) -> dict[str, str]:
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.match(line)
        if match:
            found[match.group(1).lower().replace("_", "-")] = match.group(2)
    return found


def main() -> int:
    for path in (REQUIREMENTS, LOCK):
        if not path.is_file():
            print(f"error: {path.name} is missing", file=sys.stderr)
            return 1

    declared = _pins(REQUIREMENTS.read_text(encoding="utf-8"))
    locked = _pins(LOCK.read_text(encoding="utf-8"))

    if not declared:
        print("error: no pins found in requirements.txt; this check is broken", file=sys.stderr)
        return 1

    problems = []
    for name, version in sorted(declared.items()):
        if name not in locked:
            problems.append(f"  {name}=={version} is pinned but absent from requirements.lock")
        elif locked[name] != version:
            problems.append(f"  {name}: requirements.txt says {version}, lock says {locked[name]}")

    if problems:
        print("requirements.lock has drifted from requirements.txt:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        print(
            "\nregenerate it with:\n"
            "    uv pip compile requirements.txt --generate-hashes -o requirements.lock",
            file=sys.stderr,
        )
        return 1

    print(f"requirements.lock is current: all {len(declared)} pins agree.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
