from benchmarks.evaluate_replanning_scale import evaluate as evaluate_replanning
from benchmarks.evaluate_scale import evaluate as evaluate_planning


def test_scaled_planning_and_ablation_benchmark() -> None:
    result = evaluate_planning()

    assert result["scenario_count"] == 120
    assert result["cp_valid_rate"] == 1.0
    assert result["cp_mean_utility"] >= result["greedy_mean_utility"]
    assert result["no_travel_valid_rate"] < 0.2
    assert result["no_budget_valid_rate"] < 0.5


def test_scaled_replanning_preserves_unaffected_items() -> None:
    result = evaluate_replanning()

    assert result["scenario_count"] == 120
    assert result["local_recovery_rate"] == 1.0
    assert result["local_unaffected_preservation"] == 1.0
    assert result["local_mean_preservation"] > result["global_mean_preservation"]
