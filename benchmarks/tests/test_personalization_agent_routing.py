from benchmarks.evaluate_personalization_agents import evaluate


def test_personalization_architecture_comparison_is_reproducible() -> None:
    first = evaluate()
    second = evaluate()
    assert first == second
    assert first["passed"] is True
    assert first["metrics"]["conditional_multi_agent"] == {
        "scenario_count": 4,
        "final_feasible_rate": 1,
        "preference_match_rate": 1,
        "source_fact_error_count": 0,
        "model_call_count": 2,
        "token_usage": 192,
        "model_wait_ms": 16,
    }
    assert first["metrics"]["fixed_full_team"]["model_call_count"] == 12
    assert first["metrics"]["single_generic_agent"]["model_call_count"] == 4
    assert first["metrics"]["no_decision_model"]["preference_match_rate"] == 0.25
    assert "不能外推" in first["claim_boundary"]
