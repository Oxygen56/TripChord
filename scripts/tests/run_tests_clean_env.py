#!/usr/bin/env python3
"""Launch the TripChord test suite in a scrubbed (clean-env) child process.

C-122 round-18 gate-8: real host secrets must never reach the test process.
This launcher scrubs every secret-bearing environment variable IN THE PARENT
process BEFORE spawning pytest, so the child — and every pytest-internal fixture
frame, traceback, assertion capture or subprocess it starts — can only ever see
the dummy values the tests themselves set via monkeypatch.  Nothing real is
loaded into a fixture frame at all, because it never enters the process.

The scrub is a denylist (a secret-name regex + the gate's explicit secret
candidates), so non-secret build/runtime variables (PATH, VIRTUAL_ENV, UV_*,
DATABASE_URL, ...) pass through unchanged and the suite behaves exactly like a
normal run.

Usage:
  uv run python scripts/tests/run_tests_clean_env.py [pytest args...]

Forwards pytest's exit code (0 = pass, 1 = tests failed, 2 = interrupted).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passwd|password|cookie|session|auth)"
)
_EXTRA_SECRET_ENV = frozenset(
    {
        "COOKIE",
        "SESSION",
        "SESSION_ID",
        "SESSIONID",
        "AUTHORIZATION",
        "AUTH",
        "PAYLOAD",
    }
)


def _scrub_secrets(environ: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``environ`` with every secret-bearing variable removed.

    Mirrors the in-process guard in ``scripts/tests/conftest.py`` so a direct
    ``pytest`` run and the clean-env launcher reject the same variable names:
    the launcher removes them BEFORE the child starts; the conftest re-removes
    anything injected in-process as a second line of defence.
    """
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import run_product_done_gate as gate

    candidates = set(gate._SECRET_ENV_CANDIDATES) | set(_EXTRA_SECRET_ENV)
    scrubbed = dict(environ)
    for name in list(scrubbed):
        if name in candidates or _SECRET_ENV_RE.search(name):
            scrubbed.pop(name, None)
    return scrubbed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    scripts_tests = Path(__file__).resolve().parent
    env = _scrub_secrets(dict(os.environ))
    cmd = [sys.executable, "-m", "pytest", str(scripts_tests), *args]
    result = subprocess.run(cmd, env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
