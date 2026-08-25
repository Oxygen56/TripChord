"""Deterministic Pareto selection and bounded, on-demand Agent proposals.

The provider catalog is queried before this module is called.  Every plan in
the returned set therefore shares one immutable source snapshot; Agents may
select only an existing, independently validated candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import time, timedelta
from enum import StrEnum
from itertools import pairwise
from math import prod
from typing import Protocol

from pydantic import Field, JsonValue

from tripchord.agents.models import AgentRole, PreferenceConstitution, PreferenceMode
from tripchord.domain.common import DomainModel
from tripchord.planning.complex_trip import (
    OfferCatalog,
    PlanComponent,
    PlanDecisionMetrics,
    PlanGraph,
    PlanningProblem,
    PlanPreferenceMode,
    PlanPreferencePolicy,
    PlanPreferenceSource,
    PlanRepresentativeKind,
    PlanStatus,
    PriceContract,
    StayOffer,
    TransportOffer,
    TravelIntent,
    _anchor_leg_indexes,
    _datetime_after,
    _datetime_not_after,
    _datetime_not_before,
    _intent_travelers,
    _offer_component,
    _participant_scope,
    _stay_leg_indexes,
    _traveler_leg_indexes,
    solve_complex_catalog,
    validate_plan_graph,
)

_ELDER_SKILL_ID = "elder-comfort-travel"
_ELDER_SKILL_VERSION = "1.0.0"
_PARETO_ENUMERATION_LIMIT = 2_000_000


class PersonalizationSelectionMode(StrEnum):
    SINGLE = "single"
    REPRESENTATIVES = "representatives"
    NO_SOLUTION = "no_solution"


class AgentNeedReason(StrEnum):
    AMBIGUOUS_TRADEOFF = "ambiguous_tradeoff"
    ELDER_COMFORT = "elder_comfort"


class CandidateContextProjection(DomainModel):
    candidate_id: str
    component_ids: tuple[str, ...]
    component_labels: tuple[str, ...]
    metrics: PlanDecisionMetrics
    source_refs: tuple[str, ...]


class AgentContextManifest(DomainModel):
    graph_version: str = Field(pattern=r"^graph:[0-9a-f]{64}$")
    role: AgentRole
    trigger: AgentNeedReason
    related_route_leg_ids: tuple[str, ...]
    traveler_ids: tuple[str, ...]
    candidates: tuple[CandidateContextProjection, ...]
    program_checks: tuple[str, ...]
    applicable_preferences: dict[str, JsonValue]
    allowed_tools: tuple[str, ...]
    skill_id: str | None = None
    skill_version: str | None = None
    skill_rule_boundary: str | None = None
    source_refs: tuple[str, ...] = ()
    boundary: str = (
        "仅包含本职责相关的旅行子图、有限候选、程序检查、适用偏好、"
        "允许工具、Skill版本和当前来源引用；不包含全对话、全网页或其他Agent思考。"
    )


class AgentSelectionProposal(DomainModel):
    graph_version: str = Field(pattern=r"^graph:[0-9a-f]{64}$")
    role: AgentRole
    candidate_id: str
    reason: str = Field(min_length=1, max_length=600)
    source_refs: tuple[str, ...]
    skill_id: str | None = None
    skill_version: str | None = None


class AgentProposalResult(DomainModel):
    proposal: AgentSelectionProposal
    model: str
    token_usage: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)


class AgentDecisionTrace(DomainModel):
    role: AgentRole
    trigger: AgentNeedReason
    model_called: bool
    model: str | None = None
    token_usage: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    context_manifest: AgentContextManifest
    proposal: AgentSelectionProposal | None = None
    applied: bool = False
    rejected_reason: str | None = None


class SkillApplication(DomainModel):
    skill_id: str
    skill_version: str
    applicable: bool
    selected_candidate_id: str | None = None
    reason: str
    rule_boundary: str


class ParetoPlan(DomainModel):
    candidate_id: str
    graph: PlanGraph
    metrics: PlanDecisionMetrics
    source_refs: tuple[str, ...]


class PersonalizedPlan(DomainModel):
    candidate: ParetoPlan
    representative_kind: PlanRepresentativeKind
    selection_reason: str
    participating_agent_roles: tuple[str, ...] = ()
    applied_skill_ids: tuple[str, ...] = ()


class PersonalizationSummary(DomainModel):
    selection_mode: PersonalizationSelectionMode
    preference_policy: PlanPreferencePolicy
    graph_version: str = Field(pattern=r"^graph:[0-9a-f]{64}$")
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_query_count: int = Field(default=1, ge=0)
    theoretical_combination_count: int = Field(ge=0)
    enumerated_feasible_count: int = Field(ge=0)
    pareto_candidate_count: int = Field(ge=0)
    pareto_complete_in_catalog: bool
    agent_runs: tuple[AgentDecisionTrace, ...] = ()
    skill_applications: tuple[SkillApplication, ...] = ()
    model_call_count: int = Field(default=0, ge=0)
    total_token_usage: int = Field(default=0, ge=0)
    total_agent_latency_ms: int = Field(default=0, ge=0)
    boundary: str = (
        "多个方案复用同一次来源查询和同一OfferCatalog；"
        "价格、时间、人数与可行性由程序复算，Agent只能建议已存在的候选。"
    )


class PersonalizationResult(DomainModel):
    plans: tuple[PersonalizedPlan, ...]
    summary: PersonalizationSummary


class BoundedPersonalizationAgent(Protocol):
    def propose(self, manifest: AgentContextManifest) -> AgentProposalResult:
        """Return one bounded proposal; it has no authority until validated."""


class AgentNeedRouter:
    """Route semantic work; query volume never creates additional roles."""

    def route(
        self,
        policy: PlanPreferencePolicy,
        candidates: tuple[ParetoPlan, ...],
    ) -> tuple[tuple[AgentRole, AgentNeedReason], ...]:
        if len(candidates) <= 1:
            return ()
        if (
            policy.traveling_with_elders
            and policy.max_comfort_premium_cny_cents is not None
            and not (
                policy.current_request_override
                and policy.mode == PlanPreferenceMode.PRICE_FIRST
            )
        ):
            return ((AgentRole.EXPERIENCE_SPECIALIST, AgentNeedReason.ELDER_COMFORT),)
        if policy.mode == PlanPreferenceMode.AMBIGUOUS:
            return ((AgentRole.DECISION_AGENT, AgentNeedReason.AMBIGUOUS_TRADEOFF),)
        return ()


class ElderComfortSkill:
    id = _ELDER_SKILL_ID
    version = _ELDER_SKILL_VERSION
    applicability = "仅在长辈同行且用户给出可接受的舒适溢价时适用"
    input_contract = (
        "同一当前OfferCatalog的已验证Pareto候选、换乘次数、"
        "出发时刻不便程度、交通耗时和明确溢价上限"
    )
    output_contract = "返回一个已存在候选ID和可复算选择理由"
    rule_boundary = (
        "Skill不保存价格、班次或页面内容，不创建候选；"
        "只能在用户明确溢价上限内选择已验证方案。"
    )

    def apply(
        self,
        candidates: tuple[ParetoPlan, ...],
        policy: PlanPreferencePolicy,
    ) -> SkillApplication:
        if (
            not policy.traveling_with_elders
            or policy.max_comfort_premium_cny_cents is None
            or not candidates
        ):
            return SkillApplication(
                skill_id=self.id,
                skill_version=self.version,
                applicable=False,
                reason="本次未同时具备长辈同行和明确溢价容忍条件",
                rule_boundary=self.rule_boundary,
            )
        cheapest = min(item.metrics.total_cny_cents for item in candidates)
        ceiling = cheapest + policy.max_comfort_premium_cny_cents
        eligible = tuple(
            item for item in candidates if item.metrics.total_cny_cents <= ceiling
        )
        selected = min(
            eligible,
            key=lambda item: (
                item.metrics.transfer_count if policy.avoid_transfers else 0,
                item.metrics.schedule_inconvenience_minutes,
                item.metrics.transport_duration_minutes,
                item.metrics.total_cny_cents,
                item.candidate_id,
            ),
        )
        return SkillApplication(
            skill_id=self.id,
            skill_version=self.version,
            applicable=True,
            selected_candidate_id=selected.candidate_id,
            reason=(
                f"在最低价上浮不超过¥{policy.max_comfort_premium_cny_cents / 100:.2f}"
                "的候选中，先减少换乘，再减少过早出发/过晚到达和交通耗时"
            ),
            rule_boundary=self.rule_boundary,
        )


def apply_effective_preference_policy(
    intent: TravelIntent,
    text: str,
    durable_preferences: PreferenceConstitution,
) -> TravelIntent:
    """Compile current text over conditional durable preferences."""

    compact = "".join(text.split())
    with_elders = any(
        marker in compact
        for marker in ("带父母", "和父母", "与父母", "长辈同行", "一位是长辈")
    )
    asks_fewer_transfers = any(
        marker in compact for marker in ("少换乘", "尽量不换乘", "避免换乘")
    )
    asks_avoid_early = any(
        marker in compact
        for marker in (
            "避免早班",
            "避免过早出发",
            "不赶早",
            "不早于",
            "不要太早",
        )
    )
    current_mode = PlanPreferenceMode.UNSPECIFIED
    current_reason = "本次尚未表达价格与体验的取舍"
    if any(
        marker in compact
        for marker in (
            "这次价格优先",
            "价格优先",
            "最便宜",
            "最低价",
            "省钱优先",
            "总价尽量低",
        )
    ):
        current_mode = PlanPreferenceMode.PRICE_FIRST
        current_reason = "当前请求明确价格优先"
    elif asks_fewer_transfers or asks_avoid_early or any(
        marker in compact
        for marker in (
            "舒适优先",
            "体验优先",
            "不赶早优先",
            "宁可贵一点",
            "不考虑价格",
            "预算不限",
        )
    ):
        current_mode = PlanPreferenceMode.EXPERIENCE_FIRST
        current_reason = "当前请求明确舒适与出行便利优先"
    elif any(
        marker in compact
        for marker in (
            "均衡优先",
            "价格和舒适兼顾",
            "价格适中舒适度较好",
            "性价比",
        )
    ):
        current_mode = PlanPreferenceMode.BALANCED
        current_reason = "当前请求明确要价格、耗时和出行时刻的均衡方案"
    elif any(
        marker in compact
        for marker in ("价格和舒适都重要", "既想省钱又不想太早", "看情况选吧")
    ):
        current_mode = PlanPreferenceMode.AMBIGUOUS
        current_reason = "当前请求存在真实取舍，但未给出可直接计算的优先顺序"

    premium_match = re.search(r"(?:可接受)?(?:多花|贵)(\d{1,6})元", compact)
    current_premium = int(premium_match.group(1)) * 100 if premium_match else None
    departure_threshold_match = re.search(
        r"不早于([01]?\d|2[0-3]):([0-5]\d)",
        compact,
    )
    current_departure_threshold = (
        time(
            int(departure_threshold_match.group(1)),
            int(departure_threshold_match.group(2)),
        )
        if departure_threshold_match is not None
        else time(8, 0)
    )
    if current_mode != PlanPreferenceMode.UNSPECIFIED:
        return intent.model_copy(
            update={
                "preference_summary": current_reason,
                "preference_policy": PlanPreferencePolicy(
                    mode=current_mode,
                    source=PlanPreferenceSource.CURRENT_REQUEST,
                    traveling_with_elders=with_elders,
                    avoid_transfers=asks_fewer_transfers or with_elders,
                    avoid_departures_before=current_departure_threshold,
                    max_comfort_premium_cny_cents=current_premium,
                    current_request_override=True,
                    reason=current_reason,
                ),
            }
        )

    durable = durable_preferences.effective("elder_trip_comfort")
    if (
        with_elders
        and durable is not None
        and durable.mode != PreferenceMode.INDIFFERENT
        and isinstance(durable.expected, dict)
    ):
        before = durable.expected.get("avoid_departures_before")
        parsed_before = time.fromisoformat(str(before))
        premium = durable.expected.get("max_comfort_premium_cny_cents")
        assert isinstance(premium, int) and not isinstance(premium, bool)
        reason = "本次带长辈同行，应用用户已确认的条件偏好"
        return intent.model_copy(
            update={
                "preference_summary": reason,
                "preference_policy": PlanPreferencePolicy(
                    mode=PlanPreferenceMode.EXPERIENCE_FIRST,
                    source=PlanPreferenceSource.CONFIRMED_LONG_TERM,
                    traveling_with_elders=True,
                    avoid_transfers=bool(durable.expected.get("avoid_transfers")),
                    avoid_departures_before=parsed_before,
                    max_comfort_premium_cny_cents=premium,
                    reason=reason,
                ),
            }
        )
    return intent.model_copy(
        update={
            "preference_summary": current_reason,
            "preference_policy": PlanPreferencePolicy(
                traveling_with_elders=with_elders,
            )
        }
    )


def personalization_graph_version(problem: PlanningProblem) -> tuple[str, str]:
    catalog_canonical = json.dumps(
        {
            "catalog": problem.offer_catalog.model_dump(mode="json"),
            "contracts": [item.model_dump(mode="json") for item in problem.price_contracts],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    catalog_digest = hashlib.sha256(catalog_canonical.encode()).hexdigest()
    graph_canonical = json.dumps(
        {
            "intent": problem.intent.model_dump(mode="json"),
            "catalog_digest": catalog_digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    graph_digest = hashlib.sha256(graph_canonical.encode()).hexdigest()
    return f"graph:{graph_digest}", catalog_digest


def validate_agent_selection_proposal(
    manifest: AgentContextManifest,
    proposal: AgentSelectionProposal,
) -> str | None:
    if proposal.graph_version != manifest.graph_version:
        return "stale_graph_version"
    if proposal.role != manifest.role:
        return "role_mismatch"
    selected = next(
        (
            item
            for item in manifest.candidates
            if item.candidate_id == proposal.candidate_id
        ),
        None,
    )
    if selected is None:
        return "candidate_outside_manifest"
    if not proposal.source_refs:
        return "missing_source_references"
    if not set(proposal.source_refs).issubset(set(manifest.source_refs)):
        return "source_reference_outside_manifest"
    if len(set(proposal.source_refs)) != len(proposal.source_refs):
        return "candidate_source_reference_mismatch"
    if set(proposal.source_refs) != set(selected.source_refs):
        return "candidate_source_reference_mismatch"
    if manifest.skill_id is not None and (
        proposal.skill_id != manifest.skill_id
        or proposal.skill_version != manifest.skill_version
    ):
        return "skill_version_mismatch"
    if manifest.skill_id is None and (proposal.skill_id or proposal.skill_version):
        return "undeclared_skill"
    return None


def build_pareto_plans(
    problem: PlanningProblem,
    *,
    enumeration_limit: int = _PARETO_ENUMERATION_LIMIT,
) -> tuple[tuple[ParetoPlan, ...], int, int, bool]:
    """Enumerate this bounded catalog and keep only exact non-dominated plans."""

    intent = problem.intent
    catalog = problem.offer_catalog
    if intent.unresolved_critical:
        return (), 0, 0, True
    contracts = list(problem.price_contracts)
    activity_contract_ids: list[str] = []
    for anchor in intent.anchors:
        if anchor.provided_price_cny_cents is None:
            continue
        contract_id = f"user-activity:{anchor.id}"
        activity_contract_ids.append(contract_id)
        participant_ids = _participant_scope(anchor.participant_ids, intent)
        contracts.append(
            PriceContract(
                id=contract_id,
                total_for_party_cents=anchor.provided_price_cny_cents,
                component_ids=(anchor.id,),
                covered_traveler_ids=participant_ids,
                shared_between_travelers=len(participant_ids) > 1,
                source="user-provided",
            )
        )
    effective_contracts = tuple(contracts)
    by_contract = {item.id: item for item in effective_contracts}
    all_offer_by_id: dict[str, TransportOffer | StayOffer] = {
        item.id: item for item in catalog.transports
    }
    all_offer_by_id.update({item.id: item for item in catalog.stays})

    def valid_contract(offer: TransportOffer | StayOffer) -> bool:
        contract = by_contract.get(offer.price_contract_id)
        if (
            contract is None
            or offer.id not in contract.component_ids
            or contract.currency != "CNY"
            or not contract.taxes_and_fees_included
        ):
            return False
        if len(contract.component_ids) == 1 and contract.shared:
            return False
        if len(contract.component_ids) > 1 and not contract.shared:
            return False
        if intent.traveler_profiles:
            expected_covered = {
                traveler_id
                for component_id in contract.component_ids
                if component_id in all_offer_by_id
                for traveler_id in _participant_scope(
                    all_offer_by_id[component_id].participant_ids,
                    intent,
                )
            }
            if set(contract.covered_traveler_ids) != expected_covered:
                return False
        return True

    transport_slots: list[tuple[TransportOffer, ...]] = []
    travelers = {item.id: item for item in _intent_travelers(intent)}
    for leg_requirement in intent.route_legs:
        scope = set(_participant_scope(leg_requirement.participant_ids, intent))
        transport_options = tuple(
            offer
            for offer in catalog.transports
            if valid_contract(offer)
            and (offer.origin_place_id, offer.destination_place_id)
            == (
                leg_requirement.origin_place_id,
                leg_requirement.destination_place_id,
            )
            and set(_participant_scope(offer.participant_ids, intent)) == scope
            and (
                leg_requirement.departure_date is None
                or offer.departure.date() == leg_requirement.departure_date
            )
            and (
                leg_requirement.earliest_departure_date is None
                or offer.departure.date() >= leg_requirement.earliest_departure_date
            )
            and (
                leg_requirement.latest_departure_date is None
                or offer.departure.date() <= leg_requirement.latest_departure_date
            )
            and _datetime_not_before(offer.departure, intent.window.start)
            and not _datetime_after(offer.arrival, intent.window.end)
            and _datetime_after(offer.arrival, offer.departure)
            and all(
                _datetime_not_before(offer.departure, travelers[item].available_window.start)
                and not _datetime_after(offer.arrival, travelers[item].available_window.end)
                for item in scope
                if item in travelers
            )
        )
        if not transport_options:
            return (), 0, 0, True
        transport_slots.append(transport_options)

    stay_slots: list[tuple[StayOffer, ...]] = []
    if intent.stay_requirements:
        for stay_requirement in intent.stay_requirements:
            scope = set(stay_requirement.participant_ids)
            stay_options = tuple(
                offer
                for offer in catalog.stays
                if valid_contract(offer)
                and offer.place_id == stay_requirement.place_id
                and offer.check_in <= stay_requirement.check_in
                and offer.check_out >= stay_requirement.check_out
                and set(offer.participant_ids) == scope
                and (
                    offer.confirmed_traveler_count is None
                    or offer.confirmed_traveler_count >= len(scope)
                )
                and (
                    offer.confirmed_room_count is None
                    or offer.confirmed_room_count >= stay_requirement.room_count
                )
            )
            if not stay_options:
                return (), 0, 0, True
            stay_slots.append(stay_options)
    else:
        for place in intent.places:
            stay_options = tuple(
                offer
                for offer in catalog.stays
                if valid_contract(offer) and offer.place_id == place.id
            )
            if not stay_options:
                return (), 0, 0, True
            stay_slots.append(stay_options)

    slots: tuple[tuple[TransportOffer | StayOffer, ...], ...] = (
        *transport_slots,
        *stay_slots,
    )
    theoretical = prod(len(item) for item in slots)
    if theoretical > enumeration_limit:
        graph = solve_complex_catalog(problem)
        if graph.status == PlanStatus.NO_SOLUTION:
            return (), theoretical, 0, False
        metrics = _plan_metrics(graph, catalog, intent.preference_policy)
        return (
            (
                ParetoPlan(
                    candidate_id=_candidate_id(graph.components),
                    graph=graph,
                    metrics=metrics,
                    source_refs=_plan_source_refs(graph),
                ),
            ),
            theoretical,
            1,
            False,
        )

    traveler_indexes = _traveler_leg_indexes(intent)
    frontier: list[ParetoPlan] = []
    metrics_seen: set[tuple[int, int, int, int]] = set()
    feasible_count = 0

    def complete_selection(selected: tuple[TransportOffer | StayOffer, ...]) -> None:
        nonlocal feasible_count, frontier
        transports = tuple(
            item
            for item in selected[: len(transport_slots)]
            if isinstance(item, TransportOffer)
        )
        stays = tuple(
            item
            for item in selected[len(transport_slots) :]
            if isinstance(item, StayOffer)
        )
        if len(transports) != len(transport_slots) or len(stays) != len(stay_slots):
            return
        for traveler_leg_indexes in traveler_indexes.values():
            for left, right in pairwise(traveler_leg_indexes):
                if _datetime_after(transports[left].arrival, transports[right].departure):
                    return
        if intent.stay_requirements:
            for stay_requirement in intent.stay_requirements:
                for traveler_id in stay_requirement.participant_ids:
                    stay_leg_pair = _stay_leg_indexes(
                        intent,
                        stay_requirement,
                        traveler_id,
                    )
                    if stay_leg_pair is None:
                        return
                    if (
                        transports[stay_leg_pair[0]].arrival.date()
                        > stay_requirement.check_in
                        or transports[stay_leg_pair[1]].departure.date()
                        < stay_requirement.check_out
                    ):
                        return
        else:
            for index, stay in enumerate(stays):
                if (
                    stay.check_in > transports[index].arrival.date()
                    or stay.check_out < transports[index + 1].departure.date()
                ):
                    return
        for anchor in intent.anchors:
            if _datetime_not_after(anchor.end, anchor.start):
                return
            for traveler_id in _participant_scope(anchor.participant_ids, intent):
                anchor_leg_pair = _anchor_leg_indexes(intent, anchor, traveler_id)
                if anchor_leg_pair is None:
                    return
                if _datetime_after(
                    transports[anchor_leg_pair[0]].arrival
                    + timedelta(minutes=intent.minimum_anchor_buffer_minutes),
                    anchor.start,
                ) or _datetime_after(
                    anchor.end + timedelta(minutes=intent.minimum_anchor_buffer_minutes),
                    transports[anchor_leg_pair[1]].departure,
                ):
                    return
        counted_ids = {item.price_contract_id for item in selected}
        counted_ids.update(activity_contract_ids)
        for contract_id in counted_ids:
            contract = by_contract.get(contract_id)
            if contract is None:
                return
            expected_ids = set(contract.component_ids)
            actual_ids = {
                item.id for item in selected if item.price_contract_id == contract_id
            }
            actual_ids.update(
                anchor.id
                for anchor in intent.anchors
                if f"user-activity:{anchor.id}" == contract_id
            )
            if expected_ids != actual_ids:
                return
        components = tuple(
            _offer_component(item, by_contract[item.price_contract_id])
            for item in selected
        ) + tuple(
            PlanComponent(
                kind="anchor",
                offer_id=anchor.id,
                label=anchor.name,
                provider="user-provided",
                start=anchor.start,
                end=anchor.end,
                place_from=anchor.place_id,
                price_contract_id=(
                    f"user-activity:{anchor.id}"
                    if anchor.provided_price_cny_cents is not None
                    else "user-provided-not-priced"
                ),
                detail_url="",
                price_cny_cents=anchor.provided_price_cny_cents,
                participant_ids=_participant_scope(anchor.participant_ids, intent),
            )
            for anchor in intent.anchors
        )
        ordered_contract_ids = tuple(sorted(counted_ids))
        selected_contracts = tuple(by_contract[item] for item in ordered_contract_ids)
        total = sum(item.total_for_party_cents for item in selected_contracts)
        graph = PlanGraph(
            status=PlanStatus.PARETO_OPTIMAL_IN_CATALOG,
            components=components,
            total_cny_cents=total,
            counted_price_contract_ids=ordered_contract_ids,
            price_contracts=selected_contracts,
            checked_constraints=(
                "逐段地点、日期与人员范围",
                "每位同行者时间连续",
                "住宿覆盖与活动缓冲",
                "价格合同唯一计价",
                "当前有界目录精确Pareto枚举",
            ),
            claim_boundary=(
                "仅对当前有界来源目录中的全部可行组合完成"
                "价格、交通耗时与出行时刻便利性Pareto比较；"
                "不是实时全网最低价或锁价"
            ),
        )
        if validate_plan_graph(
            graph,
            selected_contracts,
            intent=intent,
            catalog=catalog,
        ):
            return
        feasible_count += 1
        metrics = _plan_metrics(graph, catalog, intent.preference_policy)
        vector = (
            metrics.total_cny_cents,
            metrics.transport_duration_minutes,
            metrics.schedule_inconvenience_minutes,
            metrics.transfer_count,
        )
        if vector in metrics_seen:
            return
        if any(_dominates(item.metrics, metrics) for item in frontier):
            return
        frontier = [
            item for item in frontier if not _dominates(metrics, item.metrics)
        ]
        metrics_seen.add(vector)
        frontier.append(
            ParetoPlan(
                candidate_id=_candidate_id(graph.components),
                graph=graph,
                metrics=metrics,
                source_refs=_plan_source_refs(graph),
            )
        )

    def visit(
        index: int,
        selected: tuple[TransportOffer | StayOffer, ...],
    ) -> None:
        if index == len(slots):
            complete_selection(selected)
            return
        for offer in slots[index]:
            visit(index + 1, (*selected, offer))

    visit(0, ())
    frontier.sort(
        key=lambda item: (
            item.metrics.total_cny_cents,
            item.metrics.transport_duration_minutes,
            item.metrics.schedule_inconvenience_minutes,
            item.candidate_id,
        )
    )
    return tuple(frontier), theoretical, feasible_count, True


def personalize_complex_problem(
    problem: PlanningProblem,
    *,
    agent: BoundedPersonalizationAgent | None = None,
    provider_query_count: int = 1,
) -> PersonalizationResult:
    frontier, theoretical, feasible_count, complete = build_pareto_plans(problem)
    graph_version, catalog_digest = personalization_graph_version(problem)
    policy = problem.intent.preference_policy
    if not frontier:
        return PersonalizationResult(
            plans=(),
            summary=PersonalizationSummary(
                selection_mode=PersonalizationSelectionMode.NO_SOLUTION,
                preference_policy=policy,
                graph_version=graph_version,
                catalog_digest=catalog_digest,
                provider_query_count=provider_query_count,
                theoretical_combination_count=theoretical,
                enumerated_feasible_count=feasible_count,
                pareto_candidate_count=0,
                pareto_complete_in_catalog=complete,
            ),
        )

    decision_frontier = _preference_eligible_candidates(frontier, policy)
    saver, balanced, experience = _representatives(decision_frontier, policy)
    skills: list[SkillApplication] = []
    traces: list[AgentDecisionTrace] = []
    selected: ParetoPlan | None = None
    reason = ""
    applied_roles: tuple[str, ...] = ()
    applied_skills: tuple[str, ...] = ()
    needs = AgentNeedRouter().route(policy, decision_frontier)
    if needs:
        role, trigger = needs[0]
        skill_application: SkillApplication | None = None
        if role == AgentRole.EXPERIENCE_SPECIALIST:
            skill_application = ElderComfortSkill().apply(decision_frontier, policy)
            skills.append(skill_application)
        manifest = _context_manifest(
            problem,
            graph_version,
            decision_frontier,
            role=role,
            trigger=trigger,
            skill=skill_application,
        )
        if agent is not None:
            result = agent.propose(manifest)
            rejection = validate_agent_selection_proposal(manifest, result.proposal)
            if (
                rejection is None
                and skill_application is not None
                and skill_application.selected_candidate_id is not None
                and result.proposal.candidate_id
                != skill_application.selected_candidate_id
            ):
                rejection = "proposal_conflicts_with_skill_contract"
            trace = AgentDecisionTrace(
                role=role,
                trigger=trigger,
                model_called=True,
                model=result.model,
                token_usage=result.token_usage,
                latency_ms=result.latency_ms,
                context_manifest=manifest,
                proposal=result.proposal,
                applied=rejection is None,
                rejected_reason=rejection,
            )
            traces.append(trace)
            if rejection is None:
                selected = next(
                    item
                    for item in decision_frontier
                    if item.candidate_id == result.proposal.candidate_id
                )
                reason = result.proposal.reason
                applied_roles = (role.value,)
                if skill_application is not None:
                    applied_skills = (skill_application.skill_id,)
        if (
            selected is None
            and skill_application is not None
            and skill_application.selected_candidate_id is not None
        ):
            selected = next(
                item
                for item in decision_frontier
                if item.candidate_id == skill_application.selected_candidate_id
            )
            reason = skill_application.reason
            applied_skills = (skill_application.skill_id,)

    if selected is None and policy.mode == PlanPreferenceMode.PRICE_FIRST:
        selected = saver
        reason = "当前请求价格优先：选择当前有界Pareto候选中整趟人民币总价最低者"
    elif selected is None and policy.mode == PlanPreferenceMode.EXPERIENCE_FIRST:
        selected = experience
        reason = "当前请求体验优先：先减少过早出发/过晚到达，再减少交通耗时"
    elif selected is None and policy.mode == PlanPreferenceMode.BALANCED:
        selected = balanced
        reason = (
            "价格、交通耗时和出行时刻三项在当前Pareto集内等权归一化，"
            "选择平均损失最小的可复算均衡点"
        )

    plans: tuple[PersonalizedPlan, ...]
    if selected is not None:
        plans = (
            PersonalizedPlan(
                candidate=selected,
                representative_kind=PlanRepresentativeKind.PERSONALIZED,
                selection_reason=reason,
                participating_agent_roles=applied_roles,
                applied_skill_ids=applied_skills,
            ),
        )
        selection_mode = PersonalizationSelectionMode.SINGLE
    else:
        plans_list: list[PersonalizedPlan] = []
        seen: set[str] = set()
        for candidate, kind, explanation in (
            (
                saver,
                PlanRepresentativeKind.SAVER,
                "省钱方案：当前Pareto候选中整趟人民币总价最低",
            ),
            (
                balanced,
                PlanRepresentativeKind.BALANCED,
                "均衡方案：价格、交通耗时和出行时刻三项等权归一化后平均损失最小",
            ),
            (
                experience,
                PlanRepresentativeKind.EXPERIENCE,
                "体验方案：先减少过早出发/过晚到达，再减少交通耗时",
            ),
        ):
            if candidate.candidate_id in seen:
                continue
            seen.add(candidate.candidate_id)
            plans_list.append(
                PersonalizedPlan(
                    candidate=candidate,
                    representative_kind=kind,
                    selection_reason=explanation,
                )
            )
        plans = tuple(plans_list)
        selection_mode = PersonalizationSelectionMode.REPRESENTATIVES

    model_runs = tuple(item for item in traces if item.model_called)
    return PersonalizationResult(
        plans=plans,
        summary=PersonalizationSummary(
            selection_mode=selection_mode,
            preference_policy=policy,
            graph_version=graph_version,
            catalog_digest=catalog_digest,
            provider_query_count=provider_query_count,
            theoretical_combination_count=theoretical,
            enumerated_feasible_count=feasible_count,
            pareto_candidate_count=len(frontier),
            pareto_complete_in_catalog=complete,
            agent_runs=tuple(traces),
            skill_applications=tuple(skills),
            model_call_count=len(model_runs),
            total_token_usage=sum(item.token_usage for item in model_runs),
            total_agent_latency_ms=sum(item.latency_ms for item in model_runs),
        ),
    )


def _representatives(
    frontier: tuple[ParetoPlan, ...],
    policy: PlanPreferencePolicy,
) -> tuple[ParetoPlan, ParetoPlan, ParetoPlan]:
    saver = min(
        frontier,
        key=lambda item: (
            item.metrics.total_cny_cents,
            item.metrics.transport_duration_minutes,
            item.metrics.schedule_inconvenience_minutes,
            item.candidate_id,
        ),
    )
    experience = min(
        frontier,
        key=lambda item: (
            item.metrics.transfer_count if policy.avoid_transfers else 0,
            item.metrics.schedule_inconvenience_minutes,
            item.metrics.transport_duration_minutes,
            item.metrics.total_cny_cents,
            item.candidate_id,
        ),
    )
    minima = {
        "cost": min(item.metrics.total_cny_cents for item in frontier),
        "duration": min(item.metrics.transport_duration_minutes for item in frontier),
        "schedule": min(item.metrics.schedule_inconvenience_minutes for item in frontier),
    }
    maxima = {
        "cost": max(item.metrics.total_cny_cents for item in frontier),
        "duration": max(item.metrics.transport_duration_minutes for item in frontier),
        "schedule": max(item.metrics.schedule_inconvenience_minutes for item in frontier),
    }

    def loss(value: int, name: str) -> float:
        span = maxima[name] - minima[name]
        return 0.0 if span == 0 else (value - minima[name]) / span

    balanced = min(
        frontier,
        key=lambda item: (
            (
                loss(item.metrics.total_cny_cents, "cost")
                + loss(item.metrics.transport_duration_minutes, "duration")
                + loss(item.metrics.schedule_inconvenience_minutes, "schedule")
            )
            / 3,
            item.metrics.total_cny_cents,
            item.candidate_id,
        ),
    )
    return saver, balanced, experience


def _preference_eligible_candidates(
    frontier: tuple[ParetoPlan, ...],
    policy: PlanPreferencePolicy,
) -> tuple[ParetoPlan, ...]:
    if policy.max_comfort_premium_cny_cents is None:
        return frontier
    cheapest = min(item.metrics.total_cny_cents for item in frontier)
    ceiling = cheapest + policy.max_comfort_premium_cny_cents
    return tuple(item for item in frontier if item.metrics.total_cny_cents <= ceiling)


def _plan_metrics(
    graph: PlanGraph,
    catalog: OfferCatalog,
    policy: PlanPreferencePolicy,
) -> PlanDecisionMetrics:
    by_id = {item.id: item for item in catalog.transports}
    transports = tuple(
        by_id[item.offer_id]
        for item in graph.components
        if item.kind == "transport" and item.offer_id in by_id
    )
    early = 0
    late = 0
    duration = 0
    transfers = 0
    threshold = policy.avoid_departures_before
    threshold_minutes = threshold.hour * 60 + threshold.minute
    for offer in transports:
        duration += int((offer.arrival - offer.departure).total_seconds() // 60)
        departure_minutes = offer.departure.hour * 60 + offer.departure.minute
        arrival_minutes = offer.arrival.hour * 60 + offer.arrival.minute
        early += max(0, threshold_minutes - departure_minutes)
        late += max(0, arrival_minutes - 22 * 60)
        transfers += offer.transfer_count
    return PlanDecisionMetrics(
        total_cny_cents=graph.total_cny_cents or 0,
        transport_duration_minutes=duration,
        early_departure_penalty_minutes=early,
        late_arrival_penalty_minutes=late,
        schedule_inconvenience_minutes=early + late,
        transfer_count=transfers,
    )


def _dominates(left: PlanDecisionMetrics, right: PlanDecisionMetrics) -> bool:
    left_values = (
        left.total_cny_cents,
        left.transport_duration_minutes,
        left.schedule_inconvenience_minutes,
        left.transfer_count,
    )
    right_values = (
        right.total_cny_cents,
        right.transport_duration_minutes,
        right.schedule_inconvenience_minutes,
        right.transfer_count,
    )
    return all(a <= b for a, b in zip(left_values, right_values, strict=True)) and any(
        a < b for a, b in zip(left_values, right_values, strict=True)
    )


def _candidate_id(components: tuple[PlanComponent, ...]) -> str:
    value = "|".join(item.offer_id for item in components if item.kind != "anchor")
    return "pareto:" + hashlib.sha256(value.encode()).hexdigest()[:24]


def _plan_source_refs(graph: PlanGraph) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *(item.source for item in graph.price_contracts),
                *(item.detail_url for item in graph.components if item.detail_url),
            )
        )
    )


def _context_manifest(
    problem: PlanningProblem,
    graph_version: str,
    frontier: tuple[ParetoPlan, ...],
    *,
    role: AgentRole,
    trigger: AgentNeedReason,
    skill: SkillApplication | None,
) -> AgentContextManifest:
    bounded = tuple(frontier[:24])
    source_refs = tuple(
        dict.fromkeys(item for candidate in bounded for item in candidate.source_refs)
    )
    policy = problem.intent.preference_policy
    preferences: dict[str, JsonValue] = {
        "mode": policy.mode.value,
        "source": policy.source.value,
        "traveling_with_elders": policy.traveling_with_elders,
        "avoid_transfers": policy.avoid_transfers,
        "avoid_departures_before": policy.avoid_departures_before.isoformat(timespec="minutes"),
        "max_comfort_premium_cny_cents": policy.max_comfort_premium_cny_cents,
    }
    return AgentContextManifest(
        graph_version=graph_version,
        role=role,
        trigger=trigger,
        related_route_leg_ids=tuple(item.id for item in problem.intent.route_legs),
        traveler_ids=tuple(item.id for item in _intent_travelers(problem.intent)),
        candidates=tuple(
            CandidateContextProjection(
                candidate_id=item.candidate_id,
                component_ids=tuple(
                    component.offer_id
                    for component in item.graph.components
                    if component.kind != "anchor"
                ),
                component_labels=tuple(
                    component.label
                    for component in item.graph.components
                    if component.kind != "anchor"
                ),
                metrics=item.metrics,
                source_refs=item.source_refs,
            )
            for item in bounded
        ),
        program_checks=(
            "已通过人数、地点、时间、住宿覆盖和价格合同复算",
            "候选均来自同一OfferCatalog的非支配集",
        ),
        applicable_preferences=preferences,
        allowed_tools=("inspect_personalization_candidates",),
        skill_id=skill.skill_id if skill is not None else None,
        skill_version=skill.skill_version if skill is not None else None,
        skill_rule_boundary=skill.rule_boundary if skill is not None else None,
        source_refs=source_refs,
    )


__all__ = [
    "AgentContextManifest",
    "AgentDecisionTrace",
    "AgentNeedRouter",
    "AgentProposalResult",
    "AgentSelectionProposal",
    "BoundedPersonalizationAgent",
    "ElderComfortSkill",
    "PersonalizationResult",
    "PersonalizationSelectionMode",
    "PersonalizationSummary",
    "SkillApplication",
    "apply_effective_preference_policy",
    "build_pareto_plans",
    "personalization_graph_version",
    "personalize_complex_problem",
    "validate_agent_selection_proposal",
]
