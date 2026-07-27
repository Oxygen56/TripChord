from benchmarks.evaluate_repair import evaluate


def test_frozen_repair_scenarios() -> None:
    result = evaluate()

    assert result["scenario_count"] == 4
    assert result["passed"] == 4
    assert result["pass_rate"] == 1.0
