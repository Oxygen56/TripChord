from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.evaluate_package_candidate_diversity import (
    FROZEN_INPUT_SHA256,
    SCENARIO,
    _canonical_bytes,
    evaluate,
    load_scenario,
)

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "benchmarks" / "results" / "package-candidate-diversity-v1.json"
FROZEN_RESULT_SHA256 = "2dcd2897d830d8f40298d73c74c0028ae68b3a29505df3214bf15dcfeb0c4f68"


def test_package_candidate_diversity_fixture_and_result_are_frozen() -> None:
    scenario = load_scenario()
    expected = json.loads(RESULT.read_text())
    actual = evaluate()
    unsigned = {key: value for key, value in actual.items() if key != "result_sha256"}

    assert hashlib.sha256(SCENARIO.read_bytes()).hexdigest() == FROZEN_INPUT_SHA256
    assert scenario["input_classification"] == {
        "kind": "synthetic_normalized_quotes",
        "live": False,
        "bookability_verified": False,
    }
    assert actual == expected
    assert hashlib.sha256(_canonical_bytes(unsigned)).hexdigest() == FROZEN_RESULT_SHA256
    assert actual["result_sha256"] == FROZEN_RESULT_SHA256
    assert actual["passed"] is True
    assert all(actual["checks"].values())
    assert actual["claim_boundary"] == {
        "small_cap_selection_claim_allowed": True,
        "live_ota_quality_claim_allowed": False,
        "bookability_claim_allowed": False,
        "platform_superiority_claim_allowed": False,
        "exhaustive_search_claim_allowed": False,
    }


def test_scenario_hash_rejects_silent_fixture_mutation(tmp_path: Path) -> None:
    scenario = json.loads(SCENARIO.read_text())
    scenario["candidate_cap"] = 2
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(scenario, ensure_ascii=False))

    with pytest.raises(ValueError, match="scenario hash mismatch"):
        load_scenario(tampered)
