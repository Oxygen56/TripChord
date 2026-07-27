from benchmarks.evaluate_events import evaluate


def test_frozen_event_scenarios() -> None:
    result = evaluate()

    assert result["scenario_count"] == 5
    assert result["passed"] == 5
    assert result["pass_rate"] == 1.0
