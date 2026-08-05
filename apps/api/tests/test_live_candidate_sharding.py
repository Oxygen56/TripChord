from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import cast

import pytest
from pydantic import ValidationError
from tripchord.agents.adaptive_control import AdaptiveControlInput, derive_scale_directive
from tripchord.agents.agent_budget import AgentBudgetLedger, bind_agent_budget
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.live_advisory import (
    AgenticRunSummary,
    EvidenceArbitrationProposal,
)
from tripchord.agents.live_system import (
    CandidateShardMergeAudit,
    LiveCoverageMode,
    LivePackageAgentSystem,
    _RunState,
)
from tripchord.agents.model_gateway import (
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelToolCall,
)
from tripchord.agents.models import AgentRole, AgentTask
from tripchord.planning.package import (
    PackageIntent,
    PackageInventory,
    PackagePlannerHandoff,
    TravelPackageCandidate,
)
from tripchord.providers.browser_bridge import BrowserTaskBridge

_LIVE_TEST_FIXTURES = importlib.import_module("apps.api.tests.test_live_agent_system")
NOW = cast(datetime, _LIVE_TEST_FIXTURES.NOW)
intent = cast(Callable[[], PackageIntent], _LIVE_TEST_FIXTURES.intent)
_two_visible_hard_valid_candidates = cast(
    Callable[
        [],
        Awaitable[tuple[TravelPackageCandidate, TravelPackageCandidate]],
    ],
    _LIVE_TEST_FIXTURES._two_visible_hard_valid_candidates,
)


@pytest.fixture(scope="module")
def valid_candidates() -> tuple[TravelPackageCandidate, TravelPackageCandidate]:
    async def load() -> tuple[TravelPackageCandidate, TravelPackageCandidate]:
        return await _two_visible_hard_valid_candidates()

    return asyncio.run(load())


class _CandidateShardModel:
    provider = "candidate-shard-test"
    model = "candidate-shard-test"

    def __init__(
        self,
        *,
        merger_selection_id: str,
        sabotage_task_id: str | None = None,
        forged_candidate_id: str | None = None,
    ) -> None:
        self.merger_selection_id = merger_selection_id
        self.sabotage_task_id = sabotage_task_id
        self.forged_candidate_id = forged_candidate_id
        self.requests: list[ModelRequest] = []
        self.active = 0
        self.max_active = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.005)
            initial = json.loads(request.messages[0].content)
            task_id = str(initial["task"]["id"])
            tool_messages = tuple(message for message in request.messages if message.tool_results)
            if not tool_messages:
                return ModelResponse(
                    provider=self.provider,
                    model=self.model,
                    tool_calls=(
                        ModelToolCall(
                            id=f"inspect-{task_id}",
                            name="inspect_package_candidates",
                        ),
                    ),
                )

            envelope = json.loads(tool_messages[-1].tool_results[0].content)
            output = envelope["tool_observation"]["tool_receipt"]["output"]
            table = output["candidate_table"]
            id_index = table["columns"].index("id")
            visible_ids = tuple(str(row[id_index]) for row in table["rows"])
            if task_id == self.sabotage_task_id:
                selected_id = self.forged_candidate_id
            elif task_id == "curate-travel-candidates":
                selected_id = self.merger_selection_id
            else:
                selected_id = visible_ids[-1]
            return ModelResponse(
                provider=self.provider,
                model=self.model,
                text=json.dumps(
                    {
                        "summary": "只在服务端绑定候选范围内完成分片提名或最终合并",
                        "selected_candidate_id": selected_id,
                        "alternative_candidate_ids": [],
                        "tradeoffs": ["保留真实报价与硬约束的确定性裁决"],
                        "confidence": 0.8,
                    }
                ),
            )
        finally:
            self.active -= 1


def _deduplicated_inventory(
    candidates: tuple[TravelPackageCandidate, ...],
) -> PackageInventory:
    flights = {candidate.flight.id: candidate.flight for candidate in candidates}
    lodgings = {lodging.id: lodging for candidate in candidates for lodging in candidate.lodgings}
    transfers = {
        transfer.id: transfer for candidate in candidates for transfer in candidate.transfers
    }
    return PackageInventory(
        flights=tuple(flights.values()),
        lodgings=tuple(lodgings.values()),
        transfers=tuple(transfers.values()),
    )


def _candidate_pool(
    base: TravelPackageCandidate,
    count: int,
) -> tuple[TravelPackageCandidate, ...]:
    return tuple(
        base.model_copy(update={"id": f"shard-candidate-{index:03d}"}) for index in range(count)
    )


def _system_and_state(
    base: TravelPackageCandidate,
    *,
    count: int,
    sabotage_task_id: str | None = None,
) -> tuple[LivePackageAgentSystem, _RunState, _CandidateShardModel]:
    candidates = _candidate_pool(base, count)
    model = _CandidateShardModel(
        merger_selection_id=candidates[-1].id,
        sabotage_task_id=sabotage_task_id,
        forged_candidate_id=candidates[0].id,
    )
    router = ModelRouter(
        {AgentRole.CANDIDATE_CURATOR: model},
        high_risk_client=model,
    )
    system = LivePackageAgentSystem(
        BrowserTaskBridge(now=lambda: NOW),
        now=lambda: NOW,
        model_router=router,
        model_agents_required=True,
    )
    shortlist, proof = system._candidate_agent_shortlist(
        candidates,
        deterministic_selected_candidate_id=candidates[0].id,
    )
    state = _RunState(
        source_task_ids=(),
        mode=LiveCoverageMode.DEGRADED,
        inventory=_deduplicated_inventory(candidates),
        candidates=candidates,
        candidate_shortlist=shortlist,
        candidate_shortlist_proof=proof,
        planner_handoff=PackagePlannerHandoff(
            candidates=candidates,
            selected_candidate_id=candidates[0].id,
        ),
        initial_candidate=candidates[0],
    )
    return system, state, model


def _prep_task() -> AgentTask:
    return AgentTask(
        id="prepare-candidate-decision-frontier",
        role=AgentRole.CONTEXT,
        goal="prepare a bounded candidate decision frontier",
    )


def _merger_task() -> AgentTask:
    return AgentTask(
        id="curate-travel-candidates",
        role=AgentRole.CANDIDATE_CURATOR,
        goal="merge server-bound Candidate Scout nominations",
        context_topics=("package_plan", "normalized_inventory"),
        allowed_tools=("inspect_package_candidates",),
        input={"risk_level": 1},
    )


@pytest.mark.asyncio
async def test_sixty_five_candidates_use_three_read_only_scouts_and_one_merger(
    valid_candidates: tuple[TravelPackageCandidate, TravelPackageCandidate],
) -> None:
    system, state, model = _system_and_state(
        valid_candidates[0],
        count=65,
        sabotage_task_id="candidate-scout-01",
    )
    context = ContextEngine(EvidenceBlackboard())
    tools = system._tool_registry(state, source_task_count=1)
    initial_id = state.initial_candidate.id if state.initial_candidate is not None else None
    ledger = AgentBudgetLedger()

    with bind_agent_budget(ledger):
        await system._candidate_frontier_executor(state, intent())(
            _prep_task(),
            context,
            tools,
        )

        directive = state.candidate_scale_directive
        audit = state.candidate_shard_merge_audit
        assert directive is not None
        assert directive.control_input.C == 65
        assert directive.candidate_shards == 3
        assert audit is not None
        assert tuple(len(item.candidate_ids) for item in audit.shards) == (32, 32, 1)
        assert (
            len({candidate_id for item in audit.shards for candidate_id in item.candidate_ids})
            == 65
        )
        assert audit.model_concurrency_audit.admitted_count == 3
        assert audit.model_concurrency_audit.success_count == 2
        assert audit.model_concurrency_audit.failure_count == 1
        assert audit.model_concurrency_audit.peak_in_flight <= audit.max_model_concurrency
        assert model.max_active >= 2
        assert state.initial_candidate is not None
        assert state.initial_candidate.id == initial_id
        assert state.candidate_proposal is None
        assert ledger.audit().admitted_count == 3

        sabotaged = audit.shards[1]
        assert sabotaged.fallback_used
        assert sabotaged.nominated_candidate_ids == (sabotaged.candidate_ids[0],)
        assert state.candidates[0].id not in sabotaged.nominated_candidate_ids

        comparable_quote_ids = tuple(
            dict.fromkeys(
                component_id
                for candidate in state.candidate_decision_frontier
                for component_id in candidate.component_ids
            )
        )
        state.evidence_proposal = EvidenceArbitrationProposal(
            summary="decision frontier evidence classified",
            comparable_quote_ids=comparable_quote_ids,
        )
        merger_result = await system._agentic_executor(
            state,
            intent(),
            AgentRole.CANDIDATE_CURATOR,
        )(_merger_task(), context, tools)

        assert merger_result.output["agent_template_id"] == "candidate_merger"
        assert merger_result.output["agent_template_admitted"] is True
        assert state.candidate_shard_merge_audit is not None
        assert state.candidate_shard_merge_audit.merger_agent_admitted
        assert state.initial_candidate is not None
        assert state.initial_candidate.id == "shard-candidate-064"
        assert ledger.audit().admitted_count == 4

    summary = AgenticRunSummary.from_results(
        tuple(state.agentic_results.values()),
        enabled=True,
        required=True,
    )
    assert summary.stage_count == 4
    assert {stage.task_id for stage in summary.stages} == {
        "candidate-scout-00",
        "candidate-scout-01",
        "candidate-scout-02",
        "curate-travel-candidates",
    }

    audit_payload = state.candidate_shard_merge_audit.model_dump(mode="json")
    forged_scope = json.loads(json.dumps(audit_payload))
    forged_scope["shards"][0]["scope_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="scope hash"):
        CandidateShardMergeAudit.model_validate(forged_scope)
    forged_pool = json.loads(json.dumps(audit_payload))
    forged_pool["pool_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="pool hash"):
        CandidateShardMergeAudit.model_validate(forged_pool)
    forged_frontier = json.loads(json.dumps(audit_payload))
    forged_frontier["frontier_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="frontier hash"):
        CandidateShardMergeAudit.model_validate(forged_frontier)


@pytest.mark.asyncio
async def test_candidate_scout_budget_and_saturation_fail_before_any_model_call(
    valid_candidates: tuple[TravelPackageCandidate, TravelPackageCandidate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, state, model = _system_and_state(valid_candidates[0], count=65)
    context = ContextEngine(EvidenceBlackboard())
    tools = system._tool_registry(state, source_task_count=1)
    limited_ledger = AgentBudgetLedger(limit=10)
    with (
        bind_agent_budget(limited_ledger),
        pytest.raises(RuntimeError, match="only 10/96 request-wide admissions remain"),
    ):
        await system._candidate_frontier_executor(state, intent())(
            _prep_task(),
            context,
            tools,
        )
    assert limited_ledger.audit().admitted_count == 0
    assert model.requests == []

    saturated = derive_scale_directive(
        AdaptiveControlInput(
            D=400,
            C=2_000,
            G=32,
            R=True,
            E=True,
            exploration_pair_count=8,
            publication_pair_count=2,
        )
    )
    monkeypatch.setattr(system, "_candidate_stage_scale_directive", lambda _: saturated)
    with (
        bind_agent_budget(AgentBudgetLedger()),
        pytest.raises(RuntimeError, match="exceeds the 96 logical-Agent cap"),
    ):
        await system._candidate_frontier_executor(state, intent())(
            _prep_task(),
            context,
            tools,
        )
    assert model.requests == []


@pytest.mark.asyncio
async def test_at_most_thirty_two_candidates_keep_the_single_curator_path(
    valid_candidates: tuple[TravelPackageCandidate, TravelPackageCandidate],
) -> None:
    system, state, model = _system_and_state(valid_candidates[0], count=32)
    ledger = AgentBudgetLedger()
    with bind_agent_budget(ledger):
        result = await system._candidate_frontier_executor(state, intent())(
            _prep_task(),
            ContextEngine(EvidenceBlackboard()),
            system._tool_registry(state, source_task_count=1),
        )

    assert result.output["mode"] == "single_candidate_curator"
    assert state.candidate_scale_directive is not None
    assert state.candidate_scale_directive.control_input.C == 32
    assert state.candidate_shard_merge_audit is None
    assert state.candidate_decision_frontier == state.candidate_shortlist
    assert ledger.audit().admitted_count == 0
    assert model.requests == []


def test_evidence_arbiter_reads_the_collected_frontier_not_the_old_shortlist(
    valid_candidates: tuple[TravelPackageCandidate, TravelPackageCandidate],
) -> None:
    shortlist_candidate, frontier_candidate = valid_candidates
    assert set(frontier_candidate.component_ids) - set(shortlist_candidate.component_ids)
    state = _RunState(
        source_task_ids=(),
        inventory=_deduplicated_inventory(valid_candidates),
        candidates=valid_candidates,
        candidate_shortlist=(shortlist_candidate,),
        candidate_decision_frontier=(frontier_candidate,),
    )
    system = LivePackageAgentSystem(BrowserTaskBridge(now=lambda: NOW), now=lambda: NOW)

    quote_ids = {item.id for item in system._evidence_frontier_quotes(state)}

    assert quote_ids == set(frontier_candidate.component_ids)
    assert quote_ids != set(shortlist_candidate.component_ids)
