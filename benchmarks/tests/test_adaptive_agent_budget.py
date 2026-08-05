from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.evaluate_adaptive_agent_budget import (
    FROZEN_INPUT_SHA256,
    SCENARIO,
    _canonical_bytes,
    evaluate,
    load_scenarios,
)

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "benchmarks" / "results" / "adaptive-agent-budget-v1.json"
FROZEN_RESULT_SHA256 = "f04907cc1fcf637dd12f34e5b158d49617242a193d21e5d435c9637e03b9fcb0"


def test_adaptive_agent_budget_fixture_and_result_are_frozen() -> None:
    fixture = load_scenarios()
    expected = json.loads(RESULT.read_text(encoding="utf-8"))
    actual = evaluate()
    unsigned = {key: value for key, value in actual.items() if key != "result_sha256"}

    assert hashlib.sha256(SCENARIO.read_bytes()).hexdigest() == FROZEN_INPUT_SHA256
    assert fixture["input_classification"] == {
        "kind": "synthetic_controller_state",
        "live": False,
        "model_calls": False,
        "browser_calls": False,
    }
    assert actual == expected
    assert hashlib.sha256(_canonical_bytes(unsigned)).hexdigest() == FROZEN_RESULT_SHA256
    assert actual["result_sha256"] == FROZEN_RESULT_SHA256
    assert actual["passed"] is True
    assert all(actual["checks"].values())


def test_four_requirement_classes_freeze_budget_and_concurrency_ladders() -> None:
    rows = evaluate()["rows"]

    assert [row["class"] for row in rows] == ["simple", "standard", "complex", "audit"]
    assert [row["actual"]["raw_logical_agents"] for row in rows] == [8, 19, 57, 143]
    assert [row["actual"]["logical_agent_cap"] for row in rows] == [8, 19, 57, 96]
    assert [row["actual"]["desired_model_concurrency"] for row in rows] == [2, 6, 8, 12]
    assert all(row["actual"]["browser_concurrency"] == 6 for row in rows)
    assert all(row["actual"]["qunar_lodging_concurrency"] == 1 for row in rows)
    assert rows[-1]["actual"]["logical_saturated"] is True
    assert all(row["passed"] for row in rows)


def test_same_frozen_input_produces_identical_full_result() -> None:
    assert evaluate() == evaluate()


def test_fixture_hash_rejects_silent_mutation(tmp_path: Path) -> None:
    fixture = json.loads(SCENARIO.read_text(encoding="utf-8"))
    fixture["scenarios"][0]["input"]["D"] = 7
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="frozen adaptive Agent budget fixture hash mismatch"):
        load_scenarios(tampered)
