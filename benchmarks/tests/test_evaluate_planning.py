from benchmarks.evaluate_planning import evaluate


def test_frozen_optimizer_scenarios_pass() -> None:
    result = evaluate()

    assert result["scenario_count"] >= 3
    assert result["pass_rate"] == 1

