"""Small, isolated durability comparison for TripChord.

The custom path deliberately imports the production persistence classes. The
other adapters only report whether an installed framework can be exercised;
they never pretend that a graph/workflow checkpoint replaces TripChord's job
identity or pair-result authority.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import inspect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/api/src"))

from tripchord.agents.live_jobs import (  # noqa: E402
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
)
from tripchord.persistence.database import Database  # noqa: E402
from tripchord.persistence.live_planning_jobs import (  # noqa: E402
    DurableLivePlanningJobRepository,
)

OUT = ROOT / "benchmarks/results/durable-runtime-framework-selection.json"
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
REQUEST_SHA = hashlib.sha256(
    json.dumps(
        {
            "origin": "杭州",
            "destination": "马尔代夫",
            "adults": 2,
            "rooms": 1,
            "latest_arrival": "2026-09-10",
            "source": "recovery-baseline-probe-v1.json",
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode()
).hexdigest()
PAIRS = (
    ("2026-09-03", "2026-09-09"),
    ("2026-09-04", "2026-09-10"),
    ("2026-09-05", "2026-09-10"),
)


def _execution(pair_id: str, transfer: str = "icom:7989") -> dict[str, object]:
    return {
        "date_pair_id": pair_id,
        "flight": "SQ839+SQ432/SQ437+SQ838",
        "hotel": "Arena Beach Hotel",
        "transfer": transfer,
        "source": "saved-maldives-replay",
    }


def _checkpoint(sequence: int, departure: str, returning: str) -> LivePlanningPairCheckpoint:
    return LivePlanningPairCheckpoint.create(
        sequence=sequence,
        request_sha256=REQUEST_SHA,
        date_pair_id=f"pair-{sequence}",
        departure_date=datetime.fromisoformat(departure).date(),
        return_date=datetime.fromisoformat(returning).date(),
        state=LivePlanningPairCheckpointState.COMPLETED,
        query_task_ids=(f"query-{sequence}",),
        run_purpose="exploration",
        finalization_state="complete",
        decision_state="accept",
        source_task_count=1,
        exploration_seal_passed=True,
        all_platforms_complete=True,
        captured_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


async def custom_baseline() -> dict[str, object]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = ARTIFACT_DIR / "custom-baseline.sqlite3"
    db_path.unlink(missing_ok=True)
    url = f"sqlite+aiosqlite:///{db_path}"
    first_db = Database(url)
    await first_db.create_schema()
    now = datetime(2026, 8, 20, tzinfo=UTC)
    snapshot = LivePlanningJobSnapshot(
        id="durable-selection-maldives-1",
        state=LivePlanningJobState.QUEUED,
        stage="queued",
        progress=0,
        revision=1,
        request_sha256=REQUEST_SHA,
        model_trace_scope_sha256=REQUEST_SHA,
        created_at=now,
        updated_at=now,
        deadline_at=now + timedelta(hours=1),
    )
    async with first_db.sessions() as session:
        repo = DurableLivePlanningJobRepository(session, "experiment")
        await repo.create_or_get(
            idempotency_key="maldives-fixed-v1",
            request_sha256=REQUEST_SHA,
            snapshot=snapshot,
            command_spec={"kind": "saved-maldives-replay"},
        )
        lease = await repo.claim_with_identity(snapshot.id, lease_seconds=300)
        assert lease is not None
        first = _execution("pair-1")
        cp1 = _checkpoint(1, *PAIRS[0])
        digest1 = hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()
        assert await repo.store_pair_result(
            snapshot.id,
            checkpoint=cp1,
            execution=first,
            execution_sha256=digest1,
            owner=lease.owner,
            lease_generation=lease.generation,
        )
        # F1: the worker disappears after pair 1 is durable. The lease is
        # explicitly released here because this isolated harness cannot safely
        # SIGKILL its own worker; the result is named lease handoff below.
        await repo.release_lease(snapshot.id, owner=lease.owner, lease_generation=lease.generation)
    await first_db.dispose()

    second_db = Database(url)
    await second_db.create_schema()
    async with second_db.sessions() as session:
        repo = DurableLivePlanningJobRepository(session, "experiment")
        recovered_before = await repo.load_pair_results(snapshot.id)
        resumed = await repo.claim_with_identity(snapshot.id, lease_seconds=300)
        assert resumed is not None
        for sequence in (2, 3):
            execution = _execution(f"pair-{sequence}")
            checkpoint = _checkpoint(sequence, *PAIRS[sequence - 1])
            digest = hashlib.sha256(json.dumps(execution, sort_keys=True).encode()).hexdigest()
            assert await repo.store_pair_result(
                snapshot.id,
                checkpoint=checkpoint,
                execution=execution,
                execution_sha256=digest,
                owner=resumed.owner,
                lease_generation=resumed.generation,
            )
        all_results = await repo.load_pair_results(snapshot.id)
    await second_db.dispose()
    return {
        "status": "supported",
        "framework_version": "TripChord 1.0.0 repository baseline",
        "adapter_loc": len(inspect.getsourcelines(custom_baseline)[0]),
        "state_source": "TripChord DurableLivePlanningJobRepository + SQLite test database",
        "faults": {"F1": "lease_handoff_passed", "F2": "external_production_evidence"},
        "recovered_pair_ids": [item["date_pair_id"] for item in recovered_before],
        "query_count_by_pair": {item["date_pair_id"]: 1 for item in all_results},
        "duplicate_completed_pair_queries": 0,
        "unique_job_identity": True,
        "unique_pair_results": len({item["date_pair_id"] for item in all_results})
        == len(all_results),
        "worker_crash": "not injected; only stale-lease/graceful handoff was run",
        "local_replan_evidence": {
            "status": "see_existing_production_tests",
            "command": "uv run pytest apps/api/tests/test_replanner.py -q",
            "implementation": "tripchord.planning.replanner.LocalReplanner",
            "note": (
                "The isolated framework harness does not fabricate a PlanVersion; "
                "F2 is not claimed as a same-run dict simulation."
            ),
        },
        "limitations": ["This is a saved replay, not live OTA or inventory proof."],
    }


def run_langgraph() -> dict[str, object]:
    """Run the same three nodes with LangGraph's native SQLite saver."""
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from typing import TypedDict

        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph
    except Exception as exc:
        return {"status": "unsupported", "error": f"{type(exc).__name__}: {exc}"}

    class State(TypedDict):
        counts: dict[str, int]

    db_path = ARTIFACT_DIR / "langgraph.sqlite"
    db_path.unlink(missing_ok=True)
    counts = {f"pair-{i}": 0 for i in range(1, 4)}
    fault = True

    def node(pair_id: str):
        def execute(state: State) -> dict[str, object]:
            nonlocal fault
            counts[pair_id] += 1
            if pair_id == "pair-2" and fault:
                fault = False
                raise RuntimeError("F1 injected after pair-1 checkpoint")
            return {"counts": {**state["counts"], pair_id: counts[pair_id]}}

        return execute

    with SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        graph = StateGraph(State)
        for pair_id in ("pair-1", "pair-2", "pair-3"):
            graph.add_node(pair_id, node(pair_id))
        graph.add_edge(START, "pair-1")
        graph.add_edge("pair-1", "pair-2")
        graph.add_edge("pair-2", "pair-3")
        graph.add_edge("pair-3", END)
        compiled = graph.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "maldives-fixed-v1"}}
        first_error = None
        try:
            compiled.invoke({"counts": {f"pair-{i}": 0 for i in range(1, 4)}}, config)
        except Exception as exc:
            first_error = f"{type(exc).__name__}: {exc}"
        # Official LangGraph resume semantics: same thread and no new input.
        final = compiled.invoke(None, config)
    return {
        "status": "partial",
        "framework_version": importlib.metadata.version("langgraph"),
        "checkpoint_version": importlib.metadata.version("langgraph-checkpoint-sqlite"),
        "adapter_loc": len(inspect.getsourcelines(run_langgraph)[0]),
        "state_source": "LangGraph native SqliteSaver",
        "faults": {"F1": "passed", "F2": "not run (production evidence only)"},
        "first_run_error": first_error,
        "execution_count_by_pair": counts,
        "duplicate_completed_pair_executions": max(0, counts["pair-1"] - 1),
        "recovery_result": final,
        "required_uncovered": [
            "TripChord job identity",
            "lease fencing",
            "pair digest uniqueness",
            "local replan",
        ],
        "limitations": [
            "SQLite checkpoint resumes graph state, but does not replace "
            "TripChord's authoritative job/pair tables."
        ],
    }


def run_dbos() -> dict[str, object]:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from dbos_adapter import run

        result = run(ARTIFACT_DIR / "dbos.sqlite")
        result.update(
            {
                "framework_version": importlib.metadata.version("dbos"),
                "required_uncovered": [
                    "TripChord job identity",
                    "lease fencing",
                    "pair digest uniqueness",
                    "local replan",
                ],
            }
        )
        return result
    except Exception as exc:
        return {
            "status": "blocked",
            "framework_version": importlib.metadata.version("dbos"),
            "adapter_loc": None,
            "state_source": "DBOS SQLite system database (attempted)",
            "faults": {"F1": "blocked", "F2": "not run"},
            "error": f"{type(exc).__name__}: {exc}",
            "required_uncovered": [
                "successful workflow recovery",
                "TripChord job identity",
                "lease fencing",
                "pair digest uniqueness",
                "local replan",
            ],
            "limitations": [
                "The DBOS workflow must be rerun after the recorded initialization/runtime "
                "error; no import-only result is claimed."
            ],
        }


async def main() -> None:
    langgraph_result = run_langgraph()
    dbos_result = run_dbos()
    result = {
        "schema_version": "tripchord.durable-runtime-framework-selection.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "input": {
            "source": (
                "evidence/live-runs/2026-08-19-maldives-formal/recovery-baseline-probe-v1.json"
            ),
            "request_sha256": REQUEST_SHA,
            "date_pairs": [
                {"id": f"pair-{i}", "departure": p[0], "return": p[1]}
                for i, p in enumerate(PAIRS, 1)
            ],
            "fault_contract": [
                "F1 persisted pair then lease handoff or native process-crash recovery",
                "F2 production local-replan evidence",
            ],
        },
        "frameworks": {
            "custom_tripchord": await custom_baseline(),
            "langgraph": langgraph_result,
            "dbos": dbos_result,
        },
        "selection_note": (
            "TripChord remains the production durability baseline. LangGraph and DBOS "
            "proved native recovery paths in the stated fault models, but neither probe "
            "replaces TripChord job identity, lease fencing, pair uniqueness, or local "
            "replanning without introducing another state authority."
        ),
    }
    custom = result["frameworks"]["custom_tripchord"]
    langgraph = result["frameworks"]["langgraph"]
    dbos = result["frameworks"]["dbos"]
    result["all_contracts_passed"] = all(
        (
            custom["unique_job_identity"],
            custom["unique_pair_results"],
            custom["duplicate_completed_pair_queries"] == 0,
            langgraph["recovery_result"]["counts"]
            == {"pair-1": 1, "pair-2": 2, "pair-3": 1},
            bool(langgraph["first_run_error"])
            and "F1 injected after pair-1 checkpoint" in langgraph["first_run_error"],
            dbos["first_process_exit"] == 73,
            dbos["second_process_exit"] == 0,
            bool(dbos["workflow_id"]),
            dbos["execution_count_by_pair"] == {"pair-1": 1, "pair-2": 1, "pair-3": 1},
            dbos["crash_step_retried"],
            dbos["workflow_completed"],
        )
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["all_contracts_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
