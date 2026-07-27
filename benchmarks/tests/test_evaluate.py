from benchmarks.evaluate import evaluate


def test_frozen_verifier_scenarios_pass() -> None:
    result = evaluate()

    assert result["scenario_count"] >= 2
    assert result["pass_rate"] == 1

