"""DBOS 2.x two-process recovery with one stable workflow definition.

The phase variable is read only by the host process. The decorated workflow
and its inputs are identical in both processes. The unfinished ``crash_once``
step writes an external marker and exits the first process; on native DBOS
recovery the same step sees the marker and returns.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dbos import DBOS, DBOSConfig


def _read(path: Path) -> dict[str, Any]:
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"counts": {}, "events": []}
    )


def _append(path: Path, event: str, pair: str | None = None) -> None:
    data = _read(path)
    data.setdefault("events", []).append(event)
    if pair is not None:
        counts = data.setdefault("counts", {})
        counts[pair] = counts.get(pair, 0) + 1
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _process(db_path: Path, log_path: Path) -> None:
    phase = os.environ.get("TRIPCHORD_DBOS_PHASE", "resume")
    dbos = DBOS(
        config=DBOSConfig(
            name="tripchord-durable-selection",
            system_database_url=f"sqlite:///{db_path}",
            disable_otlp=True,
            run_admin_server=False,
        )
    )

    @dbos.step(name="execute-pair")
    def execute_pair(pair_id: str) -> str:
        _append(log_path, f"pair:{pair_id}", pair_id)
        return pair_id

    @dbos.step(name="crash-once")
    def crash_once() -> str:
        data = _read(log_path)
        if "crash-marker" not in data.get("events", []):
            _append(log_path, "crash-marker")
            os._exit(73)
        _append(log_path, "crash-step-retried")
        return "recovered"

    @dbos.workflow(name="maldives-three-pairs", max_recovery_attempts=3)
    def three_pairs() -> list[str]:
        # This body and its input are invariant across both processes.
        first = execute_pair("pair-1")
        crash_once()
        rest = [execute_pair("pair-2"), execute_pair("pair-3")]
        _append(log_path, "workflow-complete")
        return [first, *rest]

    dbos.launch()
    if phase == "start":
        handle = dbos.start_workflow(three_pairs)
        data = _read(log_path)
        data["workflow_id"] = handle.workflow_id
        log_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        time.sleep(30)
    else:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "workflow-complete" in _read(log_path).get("events", []):
                break
            time.sleep(0.1)
        dbos.destroy()


def run(db_path: Path) -> dict[str, Any]:
    log_path = db_path.with_suffix(".side-effects.json")
    db_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    env = os.environ.copy()
    env["TRIPCHORD_DBOS_PHASE"] = "start"
    first = subprocess.run(
        [sys.executable, __file__, str(db_path), str(log_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    env["TRIPCHORD_DBOS_PHASE"] = "resume"
    second = subprocess.run(
        [sys.executable, __file__, str(db_path), str(log_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    data = _read(log_path)
    counts = data.get("counts", {})
    complete = "workflow-complete" in data.get("events", [])
    return {
        "status": "partial"
        if first.returncode == 73 and second.returncode == 0 and complete
        else "blocked",
        "version_probe": "DBOS 2.30.0",
        "adapter_loc": len(inspect.getsourcelines(run)[0]),
        "state_source": "DBOS 2.x SQLite system database + cross-process JSON side-effect log",
        "workflow_definition_stable": True,
        "workflow_input_stable": True,
        "fault_model": (
            "same workflow crash-once step writes marker then first process "
            "os._exit(73); second process DBOS launch native recovery; no restart/fork"
        ),
        "first_process_exit": first.returncode,
        "second_process_exit": second.returncode,
        "workflow_id": data.get("workflow_id"),
        "execution_count_by_pair": {
            key: counts.get(key, 0) for key in ("pair-1", "pair-2", "pair-3")
        },
        "completed_pair_repeated": counts.get("pair-1", 0) > 1,
        "crash_step_retried": "crash-step-retried" in data.get("events", []),
        "workflow_completed": complete,
        "limitations": [
            "SQLite is DBOS development/test mode; PostgreSQL is recommended for production."
        ],
    }


if __name__ == "__main__":
    _process(Path(sys.argv[1]), Path(sys.argv[2]))
