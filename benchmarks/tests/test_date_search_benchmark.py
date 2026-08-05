from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.evaluate_date_search import (
    FROZEN_INPUT_SHA256,
    SCENARIOS,
    STRATEGIES,
    _candidate_views,
    _canonical_bytes,
    _load_scenarios,
    evaluate,
    run_selection,
)
from benchmarks.generate_date_search_scenarios import (
    CONDITIONS,
    _canonical_json,
    generate_scenarios,
)

ROOT = Path(__file__).resolve().parents[2]
RESULT = ROOT / "benchmarks" / "results" / "date-search-full-universe-v1.json"
FROZEN_RESULT_SHA256 = "4283c4e86fd0f5b97512b41aac3890554c063e97f51df6a29cdcfda42ec9e27e"


def test_full_universe_fixture_is_generated_reproducibly_and_frozen() -> None:
    generated = "\n".join(_canonical_json(item) for item in generate_scenarios()) + "\n"

    assert generated.encode() == SCENARIOS.read_bytes()
    assert hashlib.sha256(generated.encode()).hexdigest() == FROZEN_INPUT_SHA256
    scenarios = _load_scenarios()
    assert {item["condition"]["id"] for item in scenarios} == {
        condition.id for condition in CONDITIONS
    }
    assert {item["split"] for item in scenarios} == {"calibration", "test"}
    assert all(len(item["records"]) == 124 for item in scenarios)
    calibration_seeds = {item["seed"] for item in scenarios if item["split"] == "calibration"}
    test_seeds = {item["seed"] for item in scenarios if item["split"] == "test"}
    assert calibration_seeds.isdisjoint(test_seeds)


def test_selection_views_and_trace_cannot_read_unqueried_exact_oracle() -> None:
    scenario = next(item for item in _load_scenarios() if item["split"] == "test")
    candidates = _candidate_views(scenario["records"])

    assert all("exact" not in key for item in candidates for key in item.model_dump())
    for strategy in STRATEGIES:
        first, first_oracle = run_selection(
            records=scenario["records"],
            strategy=strategy,
            budget=3,
        )
        assert first.selected_pair_ids == first.query_read_pair_ids
        assert len(first.selected_pair_ids) == len(set(first.selected_pair_ids)) == 3

        mutated = [dict(item) for item in scenario["records"]]
        for item in mutated:
            if item["id"] not in first.selected_pair_ids:
                item["exact_total_cents"] = (
                    None
                    if item["exact_total_cents"] is None
                    else 9_000_000 - item["exact_total_cents"]
                )
        second, _second_oracle = run_selection(
            records=mutated,
            strategy=strategy,
            budget=3,
        )
        assert second.selected_pair_ids == first.selected_pair_ids
        assert first_oracle.evaluation_totals()


def test_scenario_content_hash_rejects_silent_oracle_mutation(tmp_path: Path) -> None:
    scenario = _load_scenarios()[0]
    scenario["records"][0]["exact_total_cents"] = 1
    tampered = tmp_path / "tampered.jsonl"
    tampered.write_text(json.dumps(scenario, ensure_ascii=False) + "\n")

    with pytest.raises(ValueError, match="scenario hash mismatch"):
        _load_scenarios(tampered)


def test_result_is_reproducible_and_keeps_the_negative_finding_visible() -> None:
    expected = json.loads(RESULT.read_text())
    actual = evaluate()
    unsigned = {key: value for key, value in actual.items() if key != "result_sha256"}

    assert actual == expected
    assert hashlib.sha256(_canonical_bytes(unsigned)).hexdigest() == FROZEN_RESULT_SHA256
    assert actual["result_sha256"] == FROZEN_RESULT_SHA256
    assert actual["real_ota_quality_claim_allowed"] is False
    assert actual["adaptive_winner_claim_allowed"] is False
    assert all(
        row["adaptive_dominates_on_both_metrics"] is False
        for row in actual["adaptive_vs_coarse"]
    )
