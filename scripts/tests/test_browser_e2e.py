"""v0.9 clean-Chrome + local fixture browser E2E contract.

The E2E boots a replay-mode API on an ephemeral SQLite database, serves the
built SPA, and drives a clean headless Chrome over CDP to verify the
workflow-steps nav and the replay planning flow.  It needs a real Chrome
binary and the built web bundle; when either is missing the script exits 2
(SKIP) and the pytest test is skipped rather than failed.

C-122 round-18 gate-8: the E2E writes its JSON + screenshot into an
ephemeral temp dir and cleans it up, so the tracked
``benchmarks/results/browser-e2e.json`` is never rewritten by the test run.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "browser_e2e.py"


def test_browser_e2e_skips_when_chrome_is_unavailable() -> None:
    """No Chrome / no dist must be an honest skip, never a forged pass."""
    # dir=ROOT keeps the screenshot inside the repo so browser_e2e.py's own
    # relative_to(ROOT) guard accepts it; the temp dir is still cleaned up.
    with tempfile.TemporaryDirectory(
        prefix="tripchord-e2e-test-", dir=ROOT
    ) as tmp:
        out_dir = Path(tmp)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--output-json",
                str(out_dir / "browser-e2e.json"),
                "--output-screenshot",
                str(out_dir / "browser-e2e-screenshot.png"),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode == 2:
            pytest.skip("clean Chrome or built SPA not available")
        assert result.returncode == 0, (
            f"browser E2E failed with {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert '"passed": true' in result.stdout
        # gate-8: the E2E output must land in the temp dir, not the tracked tree
        assert (out_dir / "browser-e2e.json").is_file()
