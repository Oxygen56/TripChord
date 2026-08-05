from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Self

from pydantic import Field, model_validator

from tripchord.agents.adaptive_control import LOGICAL_AGENT_HARD_CAP, ScaleDirective
from tripchord.agents.models import AgentRole
from tripchord.domain.common import DomainModel

ALLOWED_TEMPLATE_TOOLS = frozenset(
    {
        "inspect_date_search_space",
        "inspect_search_capabilities",
        "inspect_normalized_inventory",
        "inspect_package_candidates",
        "inspect_package_verification",
        "inspect_planning_handoffs",
    }
)


class AgentTemplateId(StrEnum):
    QUERY_STRATEGIST = "query_strategist"
    SEARCH_SUPERVISOR = "search_supervisor"
    EVIDENCE_ARBITER = "evidence_arbiter"
    CANDIDATE_CURATOR = "candidate_curator"
    RISK_CRITIC = "risk_critic"
    ORCHESTRATOR = "orchestrator"
    EXPLANATION = "explanation"
    MEMORY_CURATOR = "memory_curator"
    REPAIR_STRATEGIST = "repair_strategist"
    RECRITIC = "recritic"
    EVENT_DIAGNOSER = "event_diagnoser"
    DATE_SHARD = "date_shard"
    DATE_MERGER = "date_merger"
    CANDIDATE_SHARD = "candidate_shard"
    CANDIDATE_MERGER = "candidate_merger"
    EVIDENCE_GAP = "evidence_gap"


class AgentTemplate(DomainModel):
    id: AgentTemplateId
    role: AgentRole
    goal: str = Field(min_length=1, max_length=240)
    allowed_tools: tuple[str, ...] = ()
    max_instances: int = Field(default=1, ge=1, le=LOGICAL_AGENT_HARD_CAP)
    scalable: bool = False

    @model_validator(mode="after")
    def validate_tool_whitelist_and_scale(self) -> Self:
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise ValueError("agent template tools must be unique")
        unknown = set(self.allowed_tools) - ALLOWED_TEMPLATE_TOOLS
        if unknown:
            raise ValueError(f"agent template contains non-whitelisted tools: {sorted(unknown)}")
        if not self.scalable and self.max_instances != 1:
            raise ValueError("non-scalable templates must have max_instances=1")
        return self


def _template(
    template_id: AgentTemplateId,
    role: AgentRole,
    goal: str,
    *tools: str,
    max_instances: int = 1,
) -> AgentTemplate:
    return AgentTemplate(
        id=template_id,
        role=role,
        goal=goal,
        allowed_tools=tools,
        max_instances=max_instances,
        scalable=max_instances > 1,
    )


_TEMPLATES = {
    item.id: item
    for item in (
        _template(
            AgentTemplateId.QUERY_STRATEGIST,
            AgentRole.QUERY_STRATEGIST,
            "Select an auditable bounded date frontier.",
            "inspect_date_search_space",
        ),
        _template(
            AgentTemplateId.SEARCH_SUPERVISOR,
            AgentRole.SEARCH_SUPERVISOR,
            "Schedule only admitted read-only source tasks.",
            "inspect_search_capabilities",
            max_instances=8,
        ),
        _template(
            AgentTemplateId.EVIDENCE_ARBITER,
            AgentRole.EVIDENCE_ARBITER,
            "Audit normalized evidence without changing price truth.",
            "inspect_normalized_inventory",
            max_instances=16,
        ),
        _template(
            AgentTemplateId.CANDIDATE_CURATOR,
            AgentRole.CANDIDATE_CURATOR,
            "Choose only from the deterministic candidate frontier.",
            "inspect_package_candidates",
            max_instances=16,
        ),
        _template(
            AgentTemplateId.RISK_CRITIC,
            AgentRole.RISK_CRITIC,
            "Identify soft risk outside deterministic hard verification.",
            "inspect_package_candidates",
            "inspect_package_verification",
            max_instances=16,
        ),
        _template(
            AgentTemplateId.ORCHESTRATOR,
            AgentRole.ORCHESTRATOR,
            "Recommend a bounded final decision without overriding hard gates.",
            "inspect_planning_handoffs",
            max_instances=16,
        ),
        _template(
            AgentTemplateId.EXPLANATION,
            AgentRole.EXPLANATION,
            "Explain the final evidence-bound decision.",
            "inspect_planning_handoffs",
            max_instances=10,
        ),
        _template(
            AgentTemplateId.MEMORY_CURATOR,
            AgentRole.MEMORY_CURATOR,
            "Propose scoped memory candidates without writing them.",
            "inspect_planning_handoffs",
            max_instances=10,
        ),
        _template(
            AgentTemplateId.REPAIR_STRATEGIST,
            AgentRole.REPAIR_STRATEGIST,
            "Select a repair action from frozen candidates and verifier evidence.",
            "inspect_package_candidates",
            "inspect_package_verification",
            max_instances=17,
        ),
        _template(
            AgentTemplateId.RECRITIC,
            AgentRole.RECRITIC,
            "Independently critique the repaired candidate.",
            "inspect_package_candidates",
            "inspect_package_verification",
            max_instances=17,
        ),
        _template(
            AgentTemplateId.EVENT_DIAGNOSER,
            AgentRole.EVENT_DIAGNOSER,
            "Diagnose a bounded event before deterministic replanning.",
            "inspect_planning_handoffs",
        ),
        _template(
            AgentTemplateId.DATE_SHARD,
            AgentRole.QUERY_STRATEGIST,
            "Audit one deterministic date-frontier shard.",
            "inspect_date_search_space",
            max_instances=33,
        ),
        _template(
            AgentTemplateId.DATE_MERGER,
            AgentRole.QUERY_STRATEGIST,
            "Merge date-shard proposals within the frozen date universe.",
            "inspect_date_search_space",
            max_instances=4,
        ),
        _template(
            AgentTemplateId.CANDIDATE_SHARD,
            AgentRole.CANDIDATE_CURATOR,
            "Audit one deterministic candidate shard.",
            "inspect_package_candidates",
            max_instances=62,
        ),
        _template(
            AgentTemplateId.CANDIDATE_MERGER,
            AgentRole.CANDIDATE_CURATOR,
            "Merge candidate-shard proposals without inventing candidate IDs.",
            "inspect_package_candidates",
        ),
        _template(
            AgentTemplateId.EVIDENCE_GAP,
            AgentRole.EVIDENCE_ARBITER,
            "Audit one deduplicated evidence-gap scope.",
            "inspect_normalized_inventory",
            max_instances=32,
        ),
    )
}

AGENT_TEMPLATE_WHITELIST: Mapping[AgentTemplateId, AgentTemplate] = MappingProxyType(_TEMPLATES)


class AgentTemplateAllocation(DomainModel):
    template_id: AgentTemplateId
    instances: int = Field(ge=1, le=LOGICAL_AGENT_HARD_CAP)

    @model_validator(mode="after")
    def validate_against_whitelist(self) -> Self:
        template = AGENT_TEMPLATE_WHITELIST.get(self.template_id)
        if template is None:
            raise ValueError("agent allocation references an unknown template")
        if self.instances > template.max_instances:
            raise ValueError("agent allocation exceeds the template instance limit")
        return self


class AgentTemplatePlan(DomainModel):
    policy_version: str = "agent-template-whitelist-v1"
    state_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    allocations: tuple[AgentTemplateAllocation, ...]
    deferred_allocations: tuple[AgentTemplateAllocation, ...] = ()
    logical_agent_count: int = Field(ge=0, le=LOGICAL_AGENT_HARD_CAP)
    deferred_instance_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_plan_totals(self) -> Self:
        scheduled = sum(item.instances for item in self.allocations)
        deferred = sum(item.instances for item in self.deferred_allocations)
        if scheduled != self.logical_agent_count:
            raise ValueError("template plan logical count does not match allocations")
        if deferred != self.deferred_instance_count:
            raise ValueError("template plan deferred count does not match allocations")
        for group in (self.allocations, self.deferred_allocations):
            ids = tuple(item.template_id for item in group)
            if len(ids) != len(set(ids)):
                raise ValueError("template allocations must be unique within each group")
        return self


def get_agent_template(template_id: AgentTemplateId | str) -> AgentTemplate:
    try:
        normalized = AgentTemplateId(template_id)
    except ValueError as exc:
        raise ValueError(f"unknown agent template: {template_id}") from exc
    return AGENT_TEMPLATE_WHITELIST[normalized]


def validate_template_selection(
    template_ids: Iterable[AgentTemplateId | str],
) -> tuple[AgentTemplate, ...]:
    normalized = tuple(AgentTemplateId(item) for item in template_ids)
    if len(normalized) != len(set(normalized)):
        raise ValueError("agent template selection cannot contain duplicates")
    return tuple(get_agent_template(item) for item in normalized)


def _requested_allocation_groups(
    directive: ScaleDirective,
) -> tuple[tuple[AgentTemplateAllocation, ...], ...]:
    control = directive.control_input
    groups: list[tuple[AgentTemplateAllocation, ...]] = []

    date_group: list[AgentTemplateAllocation] = []
    if directive.date_shards:
        date_group.append(
            AgentTemplateAllocation(
                template_id=AgentTemplateId.QUERY_STRATEGIST,
                instances=1,
            )
        )
    if directive.date_shards > 1:
        date_group.extend(
            (
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.DATE_SHARD,
                    instances=directive.date_shards - 1,
                ),
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.DATE_MERGER,
                    instances=directive.date_mergers,
                ),
            )
        )
    if date_group:
        groups.append(tuple(date_group))

    exploration = control.exploration_pair_count
    publication = control.publication_pair_count
    direct = control.direct_final_pair_count
    all_pairs = exploration + publication + direct
    core_counts = (
        (AgentTemplateId.SEARCH_SUPERVISOR, exploration + direct),
        (AgentTemplateId.EVIDENCE_ARBITER, all_pairs),
        (AgentTemplateId.CANDIDATE_CURATOR, all_pairs),
        (AgentTemplateId.RISK_CRITIC, all_pairs),
        (AgentTemplateId.REPAIR_STRATEGIST, all_pairs + int(control.R)),
        (AgentTemplateId.RECRITIC, all_pairs + int(control.R)),
        (AgentTemplateId.ORCHESTRATOR, all_pairs),
        (AgentTemplateId.EXPLANATION, publication + direct),
        (AgentTemplateId.MEMORY_CURATOR, publication + direct),
    )
    core_group = tuple(
        AgentTemplateAllocation(template_id=template_id, instances=instances)
        for template_id, instances in core_counts
        if instances
    )
    if core_group:
        groups.append(core_group)

    if directive.candidate_shards > 1:
        groups.append(
            (
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.CANDIDATE_SHARD,
                    instances=directive.candidate_shards - 1,
                ),
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.CANDIDATE_MERGER,
                    instances=1,
                ),
            )
        )
    if control.E:
        groups.append(
            (
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.EVENT_DIAGNOSER,
                    instances=1,
                ),
            )
        )
    if control.G:
        groups.append(
            (
                AgentTemplateAllocation(
                    template_id=AgentTemplateId.EVIDENCE_GAP,
                    instances=control.G,
                ),
            )
        )
    return tuple(groups)


def build_agent_template_plan(directive: ScaleDirective) -> AgentTemplatePlan:
    remaining = directive.logical_agent_cap
    scheduled: list[AgentTemplateAllocation] = []
    deferred: list[AgentTemplateAllocation] = []
    groups = _requested_allocation_groups(directive)
    for group_index, group in enumerate(groups):
        group_size = sum(item.instances for item in group)
        is_last_evidence_gap_group = (
            group_index == len(groups) - 1
            and len(group) == 1
            and group[0].template_id == AgentTemplateId.EVIDENCE_GAP
        )
        if group_size <= remaining:
            scheduled.extend(group)
            remaining -= group_size
            continue
        if is_last_evidence_gap_group and remaining:
            allocation = group[0]
            scheduled.append(allocation.model_copy(update={"instances": remaining}))
            deferred.append(
                allocation.model_copy(update={"instances": allocation.instances - remaining})
            )
            remaining = 0
            continue
        deferred.extend(group)
    logical_count = sum(item.instances for item in scheduled)
    deferred_count = sum(item.instances for item in deferred)
    if logical_count + deferred_count != directive.raw_logical_agents:
        raise RuntimeError("template allocation does not reconcile with the scale directive")
    return AgentTemplatePlan(
        state_fingerprint=directive.state_fingerprint,
        allocations=tuple(scheduled),
        deferred_allocations=tuple(deferred),
        logical_agent_count=logical_count,
        deferred_instance_count=deferred_count,
    )
