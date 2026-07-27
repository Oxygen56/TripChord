import pytest

from benchmarks.evaluate_faults import evaluate_async


@pytest.mark.asyncio
async def test_provider_faults_are_isolated_and_timeouts_are_classified() -> None:
    result = await evaluate_async(20)

    assert result["partial_success_rate"] == 1.0
    assert result["failure_isolation_rate"] == 1.0
    assert result["timeout_classification_rate"] == 1.0
