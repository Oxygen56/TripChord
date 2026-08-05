from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _assert_subprocess_succeeded(
    completed: subprocess.CompletedProcess[str],
    *,
    operation: str,
) -> None:
    assert completed.returncode == 0, (
        f"{operation} failed with exit code {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )


def test_built_wheel_imports_and_serves_health_from_isolated_cwd(tmp_path: Path) -> None:
    wheel_directory = tmp_path / "wheel"
    wheel_directory.mkdir()
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_directory)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    _assert_subprocess_succeeded(build, operation="wheel build")
    wheels = tuple(wheel_directory.glob("tripchord-*.whl"))
    assert len(wheels) == 1

    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        assert "tripchord/data/replay/offers.json" in names
        assert "tripchord/data/replay/places.json" in names
        assert "tripchord/data/artifacts/replan-policy.json" in names
        archive.extractall(installed)

    isolated_cwd = tmp_path / "unrelated-working-directory"
    isolated_cwd.mkdir()
    probe = """
import asyncio
import json
from httpx import ASGITransport, AsyncClient
import tripchord
from tripchord.main import app

async def probe():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/health')
    print(json.dumps({
        'package_file': tripchord.__file__,
        'status_code': response.status_code,
        'body': response.json(),
    }))

asyncio.run(probe())
"""
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(installed),
            "MODEL_PROVIDER": "none",
            "MEMORY_STATE_PATH": ".runtime/agent-memory.json",
            "TRIPCHORD_LIVE_RUN_CACHE_STATE_PATH": ".runtime/live-run-cache.json",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=isolated_cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    _assert_subprocess_succeeded(completed, operation="isolated wheel runtime probe")
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert Path(result["package_file"]).is_relative_to(installed)
    assert result["status_code"] == 200
    assert result["body"]["service"] == "tripchord"
    assert (isolated_cwd / ".runtime").is_dir()
