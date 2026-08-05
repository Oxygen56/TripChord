from __future__ import annotations

import hashlib
import json

from benchmarks.calibrate_date_search_hybrid import (
    CALIBRATION_SCENARIOS,
    DEFAULT_MANIFEST,
    FROZEN_CALIBRATION_SHA256,
    GuardedHybridConfig,
    calibrate,
    run_guarded_selection,
)
from benchmarks.evaluate_date_search import (
    SCENARIOS,
    _canonical_bytes,
    _load_scenarios,
)
from benchmarks.evaluate_date_search_hybrid import (
    DEFAULT_OUTPUT as HYBRID_RESULT,
)
from benchmarks.evaluate_date_search_hybrid import (
    FROZEN_SEALED_HOLDOUT_SHA256,
    evaluate,
)
from benchmarks.generate_date_search_holdout import (
    DEFAULT_OUTPUT as SEALED_HOLDOUT,
)
from benchmarks.generate_date_search_holdout import (
    FROZEN_POLICY_MANIFEST_FILE_SHA256,
    generate_holdout,
)
from benchmarks.generate_date_search_scenarios import _canonical_json

FROZEN_HYBRID_RESULT_SHA256 = (
    "2dcd3f85de7726dbe23ae33786b98d703aa278a2c69bf4074dda10d63f087ab9"
)


def test_calibration_manifest_uses_only_the_separate_calibration_fixture() -> None:
    assert hashlib.sha256(CALIBRATION_SCENARIOS.read_bytes()).hexdigest() == (
        FROZEN_CALIBRATION_SHA256
    )
    assert all(item["split"] == "calibration" for item in _load_scenarios(CALIBRATION_SCENARIOS))
    expected = json.loads(DEFAULT_MANIFEST.read_text())

    assert calibrate() == expected
    assert expected["test_split_read_during_calibration"] is False
    assert expected["selected_config"] == {
        "policy_version": "coverage-guarded-hybrid-v2",
        "maximum_mean_platform_coverage_for_exploration": "0.40",
        "minimum_exploration_budget": 5,
        "coarse_guard_observations": 3,
    }
    assert hashlib.sha256(DEFAULT_MANIFEST.read_bytes()).hexdigest() == (
        FROZEN_POLICY_MANIFEST_FILE_SHA256
    )


def test_sealed_holdout_is_policy_derived_reproducible_and_uses_four_to_seven_nights() -> None:
    generated = "\n".join(_canonical_json(item) for item in generate_holdout()) + "\n"
    scenarios = _load_scenarios(SEALED_HOLDOUT)
    old_seeds = {item["seed"] for item in _load_scenarios(SCENARIOS)}

    assert generated.encode() == SEALED_HOLDOUT.read_bytes()
    assert hashlib.sha256(generated.encode()).hexdigest() == FROZEN_SEALED_HOLDOUT_SHA256
    assert {item["seed"] for item in scenarios}.isdisjoint(old_seeds)
    assert all(item["split"] == "sealed_holdout" for item in scenarios)
    assert all(item["universe_contract"]["night_counts"] == [4, 5, 6, 7] for item in scenarios)
    assert all(
        item["universe_contract"]["source_request_day_range"] == [5, 8]
        for item in scenarios
    )


def test_guarded_hybrid_selection_does_not_read_unqueried_exact_values() -> None:
    scenario = _load_scenarios(CALIBRATION_SCENARIOS)[0]
    config = GuardedHybridConfig(
        policy_version="coverage-guarded-hybrid-v2",
        maximum_mean_platform_coverage_for_exploration="1.00",
        minimum_exploration_budget=5,
        coarse_guard_observations=3,
    )
    first, first_oracle = run_guarded_selection(
        records=scenario["records"],
        budget=8,
        config=config,
    )
    selected = set(first.selection.selected_pair_ids)
    mutated = [dict(item) for item in scenario["records"]]
    for item in mutated:
        if item["id"] not in selected and item["exact_total_cents"] is not None:
            item["exact_total_cents"] = 9_000_000 - item["exact_total_cents"]
    second, _second_oracle = run_guarded_selection(
        records=mutated,
        budget=8,
        config=config,
    )

    assert first.selection.selected_pair_ids == first.selection.query_read_pair_ids
    assert second.selection.selected_pair_ids == first.selection.selected_pair_ids
    assert len(first_oracle.evaluation_totals()) > len(first.selection.selected_pair_ids)


def test_one_time_sealed_result_fails_closed_and_does_not_authorize_live_default() -> None:
    expected = json.loads(HYBRID_RESULT.read_text())
    actual = evaluate()
    unsigned = {key: value for key, value in actual.items() if key != "result_sha256"}

    assert actual == expected
    assert hashlib.sha256(_canonical_bytes(unsigned)).hexdigest() == (
        FROZEN_HYBRID_RESULT_SHA256
    )
    assert actual["result_sha256"] == FROZEN_HYBRID_RESULT_SHA256
    assert actual["acceptance"]["accepted_as_planning_candidate"] is False
    assert actual["acceptance"]["live_default_change_allowed"] is False
    assert actual["acceptance"]["materially_improved_budgets"] == [8]
    assert "contaminated regression only" in actual["evidence_status"][
        "existing_v1_test_previously_inspected"
    ]
