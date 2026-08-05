from __future__ import annotations

import asyncio

from tripchord.agents.model_gateway import (
    ModelRequest,
    ModelResponse,
    ModelToolCall,
    ModelUsage,
)

from benchmarks.evaluate_agent_architectures import (
    AgentProposal,
    ArchitectureName,
    FairBudget,
    ScenarioToolbox,
    UsageLedger,
    evaluate_architectures,
    load_scenarios,
)


class _InvalidAcceptClient:
    provider = "scripted"
    model = "invalid-accept-fixture"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not any(message.tool_results for message in request.messages):
            return ModelResponse(
                tool_calls=(
                    ModelToolCall(id="inspect", name="inspect_candidates"),
                ),
                provider=self.provider,
                model=self.model,
                usage=ModelUsage(input_tokens=10, output_tokens=5),
            )
        proposal = AgentProposal(
            summary="故意接受违反最大中转约束的候选",
            selected_candidate_id="standard-c",
            decision="accept",
        )
        return ModelResponse(
            text=proposal.model_dump_json(),
            structured_output=proposal.model_dump(mode="json"),
            provider=self.provider,
            model=self.model,
            usage=ModelUsage(input_tokens=10, output_tokens=5),
        )


def test_frozen_architecture_suite_covers_constraints_and_repair_events() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 12
    assert len({item.category for item in scenarios}) == 12
    assert sum(item.expected_repair for item in scenarios) == 5
    assert all(len(item.candidates) >= 2 for item in scenarios)


def test_scripted_run_enforces_fairness_contract_without_claiming_a_winner() -> None:
    result = asyncio.run(evaluate_architectures())
    fairness = result["fairness_contract"]
    assert isinstance(fairness, dict)
    assert fairness["same_task_set"] is True
    assert fairness["same_model_identity"] is True
    assert fairness["same_tool_contract"] is True
    assert fairness["same_total_budget"] is True
    assert result["evidence_tier"] == "scripted_harness_validation"
    claim_boundary = result["claim_boundary"]
    assert isinstance(claim_boundary, dict)
    assert claim_boundary["winner_claim_allowed"] is False


def test_scripted_harness_measures_validity_repair_calls_tokens_and_cost() -> None:
    result = asyncio.run(evaluate_architectures())
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    for architecture in (ArchitectureName.SINGLE, ArchitectureName.MULTI):
        arm = metrics[architecture]
        assert isinstance(arm, dict)
        assert arm["valid_plan_rate"] == 1
        assert arm["repair_success_rate"] == 1
        assert arm["released_hard_constraint_violation_count"] == 0
        for metric in (
            "mean_model_calls",
            "mean_tool_calls",
            "mean_total_tokens",
            "total_estimated_cost_usd",
        ):
            value = arm[metric]
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
            assert value > 0


def test_shared_tight_call_budget_fails_both_arms_instead_of_hiding_overuse() -> None:
    result = asyncio.run(
        evaluate_architectures(
            limit=1,
            budget=FairBudget(
                max_model_calls=1,
                max_tool_calls=8,
                max_total_tokens=12_000,
            ),
        )
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    for architecture in (ArchitectureName.SINGLE, ArchitectureName.MULTI):
        arm = metrics[architecture]
        assert isinstance(arm, dict)
        assert arm["failure_count"] == 1
        assert arm["budget_breach_count"] == 1
        assert arm["valid_plan_rate"] == 0


def test_deterministic_gate_exposes_bad_model_proposals_but_never_releases_them() -> None:
    result = asyncio.run(
        evaluate_architectures(
            limit=1,
            client_factory=_InvalidAcceptClient,
        )
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    for architecture in (ArchitectureName.SINGLE, ArchitectureName.MULTI):
        arm = metrics[architecture]
        assert isinstance(arm, dict)
        proposed = arm["proposed_hard_constraint_violation_count"]
        assert isinstance(proposed, (int, float)) and proposed > 0
        assert arm["released_hard_constraint_violation_count"] == 0
        assert arm["valid_plan_rate"] == 0


def test_independent_release_audit_detects_an_injected_broken_gate() -> None:
    result = asyncio.run(
        evaluate_architectures(
            limit=1,
            client_factory=_InvalidAcceptClient,
            release_gate=lambda proposal, _: proposal.decision == "accept",
        )
    )
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    for architecture in (ArchitectureName.SINGLE, ArchitectureName.MULTI):
        arm = metrics[architecture]
        assert isinstance(arm, dict)
        assert arm["released_hard_constraint_violation_count"] == 1


def test_candidate_inventory_tool_does_not_leak_precomputed_violations() -> None:
    scenario = load_scenarios()[0]
    toolbox = ScenarioToolbox(scenario, UsageLedger(FairBudget()))
    receipt = toolbox.invoke(ModelToolCall(id="inspect", name="inspect_candidates"))
    candidates = receipt["candidates"]
    assert isinstance(candidates, list)
    assert candidates
    assert all(
        isinstance(candidate, dict) and "violations" not in candidate
        for candidate in candidates
    )
