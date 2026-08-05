from __future__ import annotations

import pytest
from pydantic import ValidationError
from tripchord.agents.adaptive_control import (
    AdaptiveControlInput,
    ProviderHealth,
    derive_scale_directive,
)
from tripchord.agents.agent_templates import (
    AGENT_TEMPLATE_WHITELIST,
    AgentTemplate,
    AgentTemplateAllocation,
    AgentTemplateId,
    build_agent_template_plan,
    get_agent_template,
    validate_template_selection,
)
from tripchord.agents.models import AgentRole


def _directive(
    *,
    D: int,
    C: int,
    G: int = 0,
    R: bool = False,
    E: bool = False,
):
    providers = tuple(
        ProviderHealth(provider=name, vertical="lodging", status="healthy")
        for name in ("ctrip", "qunar")
    )
    return derive_scale_directive(
        AdaptiveControlInput(
            D=D,
            C=C,
            G=G,
            R=R,
            E=E,
            provider_health=providers,
        )
    )


def test_template_whitelist_is_complete_and_contains_no_external_action_tool() -> None:
    assert set(AGENT_TEMPLATE_WHITELIST) == set(AgentTemplateId)
    for template in AGENT_TEMPLATE_WHITELIST.values():
        assert "browser_bridge_search" not in template.allowed_tools
        assert "read_browser_cookies" not in template.allowed_tools
        assert "book" not in " ".join(template.allowed_tools)
        assert template.model_config["frozen"] is True


def test_unknown_duplicate_and_non_whitelisted_templates_are_rejected() -> None:
    with pytest.raises(ValueError, match="unknown agent template"):
        get_agent_template("arbitrary_browser_operator")
    with pytest.raises(ValueError, match="duplicates"):
        validate_template_selection((AgentTemplateId.QUERY_STRATEGIST,) * 2)
    with pytest.raises(ValidationError, match="non-whitelisted"):
        AgentTemplate(
            id=AgentTemplateId.QUERY_STRATEGIST,
            role=AgentRole.QUERY_STRATEGIST,
            goal="unsafe",
            allowed_tools=("read_browser_cookies",),
        )


def test_template_allocations_are_bound_to_per_template_limits() -> None:
    with pytest.raises(ValidationError, match="instance limit"):
        AgentTemplateAllocation(
            template_id=AgentTemplateId.CANDIDATE_SHARD,
            instances=63,
        )


@pytest.mark.parametrize(
    ("D", "C", "G", "R", "E", "logical"),
    (
        (1, 32, 0, False, False, 8),
        (1, 32, 0, True, False, 10),
        (8, 256, 4, False, False, 20),
        (32, 1_000, 9, True, False, 54),
        (124, 2_000, 0, True, True, 85),
        (124, 2_000, 11, True, True, 96),
    ),
)
def test_template_plan_exactly_reconciles_unsaturated_directive(
    D: int,
    C: int,
    G: int,
    R: bool,
    E: bool,
    logical: int,
) -> None:
    directive = _directive(D=D, C=C, G=G, R=R, E=E)
    plan = build_agent_template_plan(directive)
    assert plan.state_fingerprint == directive.state_fingerprint
    assert plan.logical_agent_count == logical
    assert plan.deferred_instance_count == 0
    assert sum(item.instances for item in plan.allocations) == logical


def test_saturated_template_plan_defers_work_instead_of_dropping_it() -> None:
    directive = _directive(D=124, C=2_000, G=12, R=True, E=True)
    plan = build_agent_template_plan(directive)
    assert directive.raw_logical_agents == 97
    assert plan.logical_agent_count == 96
    assert plan.deferred_instance_count == 1
    assert plan.logical_agent_count + plan.deferred_instance_count == 97
    assert plan.deferred_allocations == (
        AgentTemplateAllocation(template_id=AgentTemplateId.EVIDENCE_GAP, instances=1),
    )


def test_saturated_shard_package_never_runs_without_its_merger() -> None:
    directive = _directive(D=400, C=2_000)
    plan = build_agent_template_plan(directive)
    scheduled = {item.template_id: item.instances for item in plan.allocations}
    deferred = {item.template_id: item.instances for item in plan.deferred_allocations}

    assert directive.raw_logical_agents == 108
    assert directive.logical_saturated is True
    assert scheduled[AgentTemplateId.QUERY_STRATEGIST] == 1
    assert scheduled[AgentTemplateId.DATE_SHARD] == 33
    assert scheduled[AgentTemplateId.DATE_MERGER] == 4
    assert AgentTemplateId.CANDIDATE_SHARD not in scheduled
    assert AgentTemplateId.CANDIDATE_MERGER not in scheduled
    assert deferred[AgentTemplateId.CANDIDATE_SHARD] == 62
    assert deferred[AgentTemplateId.CANDIDATE_MERGER] == 1


def test_publication_fallback_template_expansion_keeps_the_core_group_atomic() -> None:
    providers = tuple(
        ProviderHealth(provider=name, vertical="lodging", status="healthy")
        for name in ("ctrip", "qunar")
    )
    directive = derive_scale_directive(
        AdaptiveControlInput(
            D=1,
            C=0,
            G=0,
            R=False,
            E=False,
            exploration_pair_count=6,
            publication_pair_count=6,
            provider_health=providers,
        )
    )
    plan = build_agent_template_plan(directive)
    allocations = {item.template_id: item.instances for item in plan.allocations}

    assert directive.raw_logical_agents == 91
    assert plan.logical_agent_count == 91
    assert plan.deferred_allocations == ()
    assert allocations[AgentTemplateId.EVIDENCE_ARBITER] == 12
    assert allocations[AgentTemplateId.CANDIDATE_CURATOR] == 12
    assert allocations[AgentTemplateId.REPAIR_STRATEGIST] == 12
