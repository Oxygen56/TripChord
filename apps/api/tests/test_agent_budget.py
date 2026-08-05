from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel
from tripchord.agents.agent_budget import (
    AgentBudgetExceeded,
    AgentBudgetLedger,
    bind_agent_budget,
    current_agent_budget,
    request_agent_budgeted,
)
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.live_advisory import StructuredLiveModelAgent
from tripchord.agents.models import AgentRole, AgentTask
from tripchord.agents.tools import ToolRegistry


class _EmptyOutput(BaseModel):
    pass


@pytest.mark.asyncio
async def test_request_ledger_enforces_the_logical_agent_cap_under_concurrency() -> None:
    ledger = AgentBudgetLedger(limit=3)

    async def admit(index: int) -> bool:
        try:
            await ledger.admit(f"task-{index}", AgentRole.QUERY_STRATEGIST)
        except AgentBudgetExceeded:
            return False
        return True

    outcomes = await asyncio.gather(*(admit(index) for index in range(8)))
    audit = ledger.audit()

    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 5
    assert audit.admitted_count == 3
    assert audit.rejected_count == 5
    assert audit.remaining_count == 0
    assert [item.sequence for item in audit.admissions] == [1, 2, 3]


@pytest.mark.asyncio
async def test_nested_budgeted_runs_reuse_one_request_ledger() -> None:
    seen: list[AgentBudgetLedger] = []

    @request_agent_budgeted
    async def inner() -> None:
        ledger = current_agent_budget()
        assert ledger is not None
        seen.append(ledger)
        await ledger.admit("inner", AgentRole.CANDIDATE_CURATOR)

    @request_agent_budgeted
    async def outer() -> int:
        ledger = current_agent_budget()
        assert ledger is not None
        seen.append(ledger)
        await ledger.admit("outer", AgentRole.QUERY_STRATEGIST)
        await inner()
        return ledger.audit().admitted_count

    assert await outer() == 2
    assert seen[0] is seen[1]
    assert current_agent_budget() is None


def test_explicit_budget_binding_restores_the_previous_context() -> None:
    ledger = AgentBudgetLedger(limit=1)
    assert current_agent_budget() is None
    with bind_agent_budget(ledger):
        assert current_agent_budget() is ledger
    assert current_agent_budget() is None


@pytest.mark.asyncio
async def test_structured_agent_is_rejected_before_router_after_ninety_six_admissions() -> None:
    ledger = AgentBudgetLedger()
    agent = StructuredLiveModelAgent(
        AgentRole.QUERY_STRATEGIST,
        None,
        system_prompt="unused",
        output_model=_EmptyOutput,
        required=True,
    )
    context = ContextEngine(EvidenceBlackboard())
    tools = ToolRegistry()

    with bind_agent_budget(ledger):
        results = await asyncio.gather(
            *(
                agent.execute(
                    AgentTask(
                        id=f"bounded-agent-{index:03d}",
                        role=AgentRole.QUERY_STRATEGIST,
                        goal="prove runtime admission",
                    ),
                    context,
                    tools,
                )
                for index in range(97)
            )
        )

    audit = ledger.audit()
    assert audit.admitted_count == 96
    assert audit.rejected_count == 1
    assert audit.rejected_task_ids == ("bounded-agent-096",)
    rejected_trace = results[-1].output["agentic_trace"]
    assert isinstance(rejected_trace, dict)
    assert "agent_budget_exhausted" in str(rejected_trace["failure"])
