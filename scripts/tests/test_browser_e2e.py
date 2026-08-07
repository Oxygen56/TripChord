"""v0.9 clean-Chrome + local fixture browser E2E contract.

The E2E boots a replay-mode API on an ephemeral SQLite database, serves the
built SPA, and drives a clean headless Chrome over CDP to verify the
workflow-steps nav and the replay planning flow.  It needs a real Chrome
binary and the built web bundle; when either is missing the script exits 2
(SKIP) and the pytest test is skipped rather than failed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "browser_e2e.py"


def _run_script(timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_browser_e2e_skips_when_chrome_is_unavailable() -> None:
    """No Chrome / no dist must be an honest skip, never a forged pass."""
    result = _run_script(timeout=300)
    if result.returncode == 2:
        pytest.skip("clean Chrome or built SPA not available")
    assert result.returncode == 0, (
        f"browser E2E failed with {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert '"passed": true' in result.stdout
