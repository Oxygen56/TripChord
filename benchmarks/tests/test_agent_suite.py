import asyncio

from benchmarks.evaluate_agents import evaluate
from benchmarks.generate_agent_scenarios import CATEGORIES, generate


def test_frozen_agent_suite_has_240_tasks_across_12_categories() -> None:
    scenarios = generate()
    assert len(scenarios) == 240
    assert {item["category"] for item in scenarios} == set(CATEGORIES)
    assert all(
        sum(item["category"] == category for item in scenarios) == 20 for category in CATEGORIES
    )


def test_agent_evaluator_small_smoke() -> None:
    result = asyncio.run(evaluate(limit=12, serial_stride=4, delay_seconds=0))
    assert result["scenario_count"] == 12
    assert result["category_count"] == 12
    assert result["quality"]["silent_hard_violation_count"] == 0
    assert result["reliability"]["unauthorised_l3_execution_count"] == 0


def test_concurrency_gate_uses_declared_nonzero_external_wait() -> None:
    # Keep the injected I/O wait above local event-loop noise so the benchmark
    # measures concurrency rather than host load from neighboring test suites.
    result = asyncio.run(evaluate(limit=24, serial_stride=2, delay_seconds=0.02))
    assert result["concurrency"]["simulated_model_delay_seconds"] == 0.02
    assert result["concurrency"]["same_quality"] is True
    assert result["concurrency"]["p50_speedup"] >= 0.35
