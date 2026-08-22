import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_serializer, model_validator

from tripchord.agents.companion_control_tools import (
    BrowserCompanionBuildReconcileResponse,
)
from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexiblePackageConstraints,
)
from tripchord.agents.live_jobs import (
    NON_DURABLE_LIVE_PLANNING_BOUNDARY,
    LivePlanningJobSnapshot,
)
from tripchord.agents.live_monitor import LiveMonitorStatus
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LiveEventReplanRun,
    LivePackageAgentRun,
    LivePackageEvent,
)
from tripchord.agents.memory import MemoryRecord, normalize_confirmed_preference_value
from tripchord.agents.models import PreferenceConstitution
from tripchord.agents.package_request import (
    HybridPackageRequirementResult,
    PackageRequirementRequest,
)
from tripchord.agents.plan_modification import (
    LivePlanModificationReceipt,
)
from tripchord.agents.travel_runtime import TravelAgentRun
from tripchord.domain.common import Coordinates
from tripchord.domain.events import PlanEvent
from tripchord.domain.itinerary import ItineraryItem, PlanVersion, Violation
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import RouteMode
from tripchord.domain.trip import TripSpec
from tripchord.jobs import JobSnapshot
from tripchord.persistence.repository import WorkspaceSnapshot
from tripchord.planning import ChineseRequirementParser, ItineraryOptimizer, PlanVerifier
from tripchord.planning.adaptive import AdaptiveReplanResult
from tripchord.planning.flexible_dates import FlexibleTravelWindow, PlatformFareCalendar
from tripchord.planning.impact import PlanDependency
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageDecisionState,
    PackageIntent,
)
from tripchord.planning.policy import ReplanPreference
from tripchord.planning.problem import OptimizationResult, PlanningProblem
from tripchord.planning.replanner import LocalReplanner, LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.stay_plans import StayPlanCandidateSet
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowResult
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult, ProviderRegistry
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserSearchQuery,
    _is_allowed_provider_url,
)
from tripchord.providers.icom_transfer import IComCnyReferenceEstimate
from tripchord.providers.user_snapshot import UserQuoteInput, import_user_quote


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _sanitize_public_flight_totals(value: Any) -> Any:
    if isinstance(value, dict):
        result = {key: _sanitize_public_flight_totals(item) for key, item in value.items()}
        if result.get("party_total_known") is False and "total_for_party_cents" in result:
            if result.get("display_amount_cents") is None:
                result["display_amount_cents"] = result.get("total_for_party_cents")
            result["total_for_party_cents"] = None
            result["price_basis"] = "comparison_only"
        return result
    if isinstance(value, list):
        return [_sanitize_public_flight_totals(item) for item in value]
    return value


class VerifyRequest(ApiModel):
    spec: TripSpec
    plan: PlanVersion
    context: VerificationContext = VerificationContext()


class VerifyResponse(ApiModel):
    valid: bool
    violations: tuple[Violation, ...]


class GeocodeRequest(ApiModel):
    address: str
    city: str | None = None


class PlaceSearchRequest(ApiModel):
    keywords: str
    city: str
    types: tuple[str, ...] = ()
    limit: int = 20


class RouteRequest(ApiModel):
    origin: Coordinates
    destination: Coordinates
    mode: RouteMode
    city: str | None = None


class WeatherRequest(ApiModel):
    adcode: str
    coordinates: Coordinates


class ParseTripRequest(ApiModel):
    text: str
    default_year: int


class OptimizePlanRequest(ApiModel):
    problem: PlanningProblem
    trip_id: str
    plan_id: str
    version: int = 1


class OptimizePlanResponse(ApiModel):
    result: OptimizationResult
    plan: PlanVersion


class RepairPlanRequest(ApiModel):
    spec: TripSpec
    plan: PlanVersion
    context: VerificationContext = VerificationContext()
    max_iterations: int = 3


class ReplanRequest(ApiModel):
    spec: TripSpec
    plan: PlanVersion
    event: PlanEvent
    context: VerificationContext = VerificationContext()
    dependencies: tuple[PlanDependency, ...] | None = None
    replacements: dict[str, ItineraryItem] = Field(default_factory=dict)
    max_iterations: int = 3


class CreateWorkspaceRequest(ApiModel):
    spec: TripSpec
    title: str | None = None


class SavePlanRequest(ApiModel):
    plan: PlanVersion


class WorkspaceReplanRequest(ApiModel):
    event: PlanEvent
    context: VerificationContext = VerificationContext()
    dependencies: tuple[PlanDependency, ...] | None = None
    replacements: dict[str, ItineraryItem] = Field(default_factory=dict)
    max_iterations: int = 3
    preference: ReplanPreference = ReplanPreference.MINIMUM_CHANGE


class WorkspaceReplanResponse(ApiModel):
    result: AdaptiveReplanResult
    workspace: WorkspaceSnapshot


class CreatePlanningJobRequest(ApiModel):
    problem: PlanningProblem


class StartTripPlanningRequest(ApiModel):
    spec: TripSpec
    title: str | None = None


class StartTripPlanningResponse(ApiModel):
    workspace: WorkspaceSnapshot
    job: JobSnapshot
    data_mode: str
    candidate_count: int


class AgentPlanningRequest(ApiModel):
    spec: TripSpec
    preferences: PreferenceConstitution = PreferenceConstitution()


class AgentPlanningResponse(ApiModel):
    mode: str = "replay"
    claim_boundary: str = "回放模型与回放库存，仅验证多 Agent 决策闭环；不代表实时可订或全网最低价"
    run: TravelAgentRun


class AgentRuntimeProvenance(ApiModel):
    """Startup identity of the running API process (captured once at import)."""

    repo_toplevel: str | None = None
    commit_sha: str | None = None
    started_at: str | None = None
    pid: int | None = None
    python_version: str | None = None
    python_executable: str | None = None
    dependency_lock_sha256: str | None = None
    live_system_source_sha256: str | None = None


class AgentRuntimeStatusResponse(ApiModel):
    codex_runtime_dependency: bool = False
    chatgpt_runtime_dependency: bool = False
    model_enabled: bool
    model_required: bool
    model_provider: str | None = None
    primary_model: str | None = None
    fast_model: str | None = None
    worker_model_runtime: dict[str, JsonValue] | None = None
    model_trace_count: int = Field(default=0, ge=0)
    effective_flexible_timeout_seconds: int = Field(ge=60, le=3600)
    context_engine: str = "versioned evidence blackboard + role/budget scoped context packs"
    memory_backend: str = "process-local scoped MemoryStore"
    memory_persistence_enabled: bool = False
    sensitive_memory_persisted: bool = False
    live_run_cache_backend: str = "process-local fixed-TTL LRU"
    live_run_cache_persistence_enabled: bool = False
    live_run_cache_multi_worker_supported: bool = False
    browser_companion_control_enabled: bool = False
    browser_companion_auto_reload_enabled: bool = False
    browser_companion_supervisor_running: bool = False
    browser_companion_supervisor_outcome: str | None = None
    browser_companion_supervisor_attempt_count: int = Field(default=0, ge=0)
    browser_companion_last_reconcile: BrowserCompanionBuildReconcileResponse | None = None
    rag_enabled: bool = True
    runtime_provenance: AgentRuntimeProvenance | None = None
    formal_live_source: dict[str, JsonValue] | None = None
    rag_boundary: str = (
        "只检索用户偏好、历史决策、平台能力与非实时证据；实时价格、余票和库存禁止进入 RAG。"
    )
    agent_decision_roles: tuple[str, ...] = (
        "需求理解与冲突提案",
        "日期查询策略",
        "来源搜索调度",
        "证据仲裁",
        "候选策展",
        "软风险批判",
        "修复策略与修复后复审",
        "事件诊断",
        "主控建议、解释与记忆候选",
    )
    deterministic_authority: tuple[str, ...] = (
        "用户明示硬约束与工具授权",
        "域名白名单、验证码和只读边界",
        "报价解析、金额计算、身份、新鲜度与可比性",
        "候选数量上限与并发/速率预算",
        "Verifier、ReVerifier、Repair 执行器与最终 Safety Gate",
        "租户隔离、缓存分区和持久化完整性",
    )
    autonomy_boundary: str = (
        "模型 Agent 对语义不确定性、工具计划、候选取舍和返工策略作结构化提案并能改变执行；"
        "确定性代码保留事实计算、权限、硬约束和最终发布否决权。"
    )
    browser_runtime_requirements: tuple[str, ...] = (
        "local FastAPI/browser bridge",
        "Chrome companion extension",
        "user-granted provider host permissions",
        "valid OTA login state",
        "network and current provider DOM contracts",
    )


class ConfirmPreferenceMemoryRequest(ApiModel):
    key: str = Field(min_length=1, max_length=120)
    value: JsonValue
    source_evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_bounded_preference(self) -> "ConfirmPreferenceMemoryRequest":
        self.value = normalize_confirmed_preference_value(self.key, self.value)
        serialized = json.dumps(
            self.value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(serialized.encode("utf-8")) > 4_096:
            raise ValueError("confirmed preference value exceeds 4096 bytes")
        if len(self.source_evidence_refs) > 32 or any(
            not item.strip() or len(item) > 240 for item in self.source_evidence_refs
        ):
            raise ValueError("confirmed preference evidence references are invalid")
        return self


class ConfirmPreferenceMemoryResponse(ApiModel):
    record: MemoryRecord
    boundary: str = "只有用户显式调用确认接口才写入长期偏好；Agent 推断的长期偏好不会自动持久化。"


class AgentMemoryListResponse(ApiModel):
    records: tuple[MemoryRecord, ...]


class RevokeMemoryResponse(ApiModel):
    record_id: str
    revoked: bool
    boundary: str = "只能撤销当前已认证用户自己的记忆；撤销会同步写入本地持久化快照。"


class LiveAgentPlanningRequest(ApiModel):
    intent: PackageIntent
    search_query: BrowserSearchQuery
    coverage_mode: LiveCoverageMode = LiveCoverageMode.STRICT
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)


class LiveAgentPlanningResponse(ApiModel):
    run_id: str = Field(min_length=1)
    expires_at: datetime
    run: LivePackageAgentRun
    # FinalPlanProjection is declared below; rebuild this model after that
    # declaration so the public schema remains strongly typed.
    final_plan: "FinalPlanProjection | None" = None


class LiveFlexibleAgentPlanningRequest(ApiModel):
    window: FlexibleTravelWindow
    calendars: tuple[PlatformFareCalendar, ...] = ()
    coverage_mode: LiveCoverageMode = LiveCoverageMode.STRICT
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)
    total_timeout_seconds: int | None = Field(default=None, ge=60, le=600)
    max_pairs: int = Field(default=400, ge=1, le=400)
    publication_refresh_minimum_options: int = Field(default=1, ge=0, le=2)
    constraints: FlexiblePackageConstraints = FlexiblePackageConstraints()
    stay_plan_candidate_set: StayPlanCandidateSet | None = None


class LiveFlexiblePairRunHandle(ApiModel):
    date_pair_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    expires_at: datetime


class FinalFlightProjection(ApiModel):
    provider: str
    origin: str
    destination: str
    outbound_flight_numbers: tuple[str, ...] = ()
    outbound_depart_at: datetime
    outbound_arrive_at: datetime
    return_flight_numbers: tuple[str, ...] = ()
    return_depart_at: datetime
    return_arrive_at: datetime
    total_for_party_cents: int | None = None
    display_amount_cents: int | None = None
    party_total_known: bool = True
    price_basis: str = "total_party"
    official_view_url: str | None = None


class FinalLodgingProjection(ApiModel):
    provider: str
    property_name: str
    area: str
    check_in: date
    check_out: date
    rooms: int
    room_name: str | None = None
    breakfast_included: bool | None = None
    cancellation_policy: str | None = None
    location_convenience: str = "unknown"
    location_address: str | None = None
    nearby_location_evidence: tuple[str, ...] = ()
    location_evidence_summary: str | None = None
    display_total_cents: int | None = None
    official_view_url: str | None = None


class FinalTransferProjection(ApiModel):
    provider: str
    origin_area: str
    destination_area: str
    service_date: date
    schedule_mode: str
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    currency: str | None = None
    total_for_party_cents: int | None = None
    taxes_and_fees_included: bool | None = None
    price_guarantee: str | None = None


class FinalPlanProjection(ApiModel):
    """The sole user-facing flexible result; ranked options remain diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    option_id: str
    date_pair_id: str
    departure_date: date
    return_date: date
    total_budget_cents: int | None = None
    confirmed_cny_subtotal_cents: int | None = None
    estimated_icom_transfer_cny_cents: int | None = None
    estimated_total_cny_cents: int | None = None
    icom_cny_reference_estimate: IComCnyReferenceEstimate | None = None
    optimality_status: str
    claim_boundary: str
    flight_component_id: str | None = None
    lodging_component_ids: tuple[str, ...] = ()
    transfer_component_ids: tuple[str, ...] = ()
    flight: FinalFlightProjection | None = None
    lodgings: tuple[FinalLodgingProjection, ...] = ()
    transfers: tuple[FinalTransferProjection, ...] = ()
    party: dict[str, int] = Field(default_factory=dict)
    covered_source_ids: tuple[str, ...] = ()
    failed_source_ids: tuple[str, ...] = ()
    price_comparability: str = "total_not_comparable"
    unresolved_items: tuple[str, ...] = ()


class BestAvailablePlanProjection(FinalPlanProjection):
    """A useful current recommendation when a confirmed party total is absent."""

    advisory_note: str = Field(min_length=1)


LiveAgentPlanningResponse.model_rebuild(
    _types_namespace={"FinalPlanProjection": FinalPlanProjection}
)


class LiveFlexibleAgentPlanningResponse(ApiModel):
    run: FlexibleLiveAgentRun
    final_plan: FinalPlanProjection | None = None
    cached_pair_runs: tuple[LiveFlexiblePairRunHandle, ...] = ()


class LiveFlexibleFromTextPlanningRequest(ApiModel):
    requirement: PackageRequirementRequest
    calendars: tuple[PlatformFareCalendar, ...] = ()
    coverage_mode: LiveCoverageMode = LiveCoverageMode.STRICT
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)
    total_timeout_seconds: int | None = Field(default=None, ge=60, le=600)
    max_pairs: int = Field(default=400, ge=1, le=400)
    publication_refresh_minimum_options: int = Field(default=1, ge=0, le=2)
    stay_plan_candidate_set: StayPlanCandidateSet | None = None


LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY = (
    "默认使用确定性优先的本地需求解析；模型增强未启用。仅当关键字段完整时才会"
    "启动实时浏览器搜索，报价排序只对本轮抽样日期与可见证据有效。"
)


def _lodging_location_evidence_summary(
    lodging: NormalizedLodgingQuote,
) -> str | None:
    if lodging.location_address is None or not lodging.nearby_location_evidence:
        return None
    nearby = "；".join(lodging.nearby_location_evidence)
    return f"来源页面显示地址：{lodging.location_address}；页面显示邻近：{nearby}"


class LiveFlexibleFromTextPlanningResponse(ApiModel):
    interpretation: HybridPackageRequirementResult
    run: FlexibleLiveAgentRun | None = None
    final_plan: FinalPlanProjection | None = None
    best_available_plan: BestAvailablePlanProjection | None = None
    cached_pair_runs: tuple[LiveFlexiblePairRunHandle, ...] = ()
    model_enhancement_enabled: bool = False
    model_trace_scope_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    model_trace_count: int = Field(ge=0)
    model_trace_success_count: int = Field(ge=0)
    model_trace_failure_count: int = Field(ge=0)
    model_trace_boundary: str = (
        "仅返回本次规范化 API 请求的 SHA-256 与该执行实例内模型调用条数；"
        "不保存或返回 prompt、模型响应正文、推理内容、Cookie、令牌或 API Key。"
    )
    execution_boundary: str = LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY

    @model_serializer(mode="wrap")
    def serialize_public(self, handler: Callable[[Any], Any]) -> Any:
        """Hide unproven flight party totals at the public API boundary."""

        return _sanitize_public_flight_totals(handler(self))

    @model_validator(mode="after")
    def validate_model_trace_counts(self) -> "LiveFlexibleFromTextPlanningResponse":
        if self.model_trace_count != (
            self.model_trace_success_count + self.model_trace_failure_count
        ):
            raise ValueError("model trace success and failure counts must add up")
        return self


def build_live_final_plan_projection(run: LivePackageAgentRun) -> FinalPlanProjection | None:
    if run.decision.state != PackageDecisionState.ACCEPT or run.package is None:
        return None
    package = run.package
    candidate = package.final_candidate
    comparable = (
        package.budget.is_all_in_total
        and package.budget.currency == "CNY"
        and candidate.currency == "CNY"
    )
    confirmed_cny_subtotal_cents = (
        package.budget.confirmed_subtotal_cents
        if package.budget.currency == "CNY" and candidate.currency == "CNY"
        else None
    )
    reference_estimate = run.icom_cny_reference_estimate
    estimated_icom_transfer_cny_cents = (
        reference_estimate.estimated_cny_cents
        if reference_estimate is not None
        and not package.budget.foreign_currency_subtotals
        else None
    )
    estimated_total_cny_cents = (
        confirmed_cny_subtotal_cents + estimated_icom_transfer_cny_cents
        if confirmed_cny_subtotal_cents is not None
        and estimated_icom_transfer_cny_cents is not None
        else None
    )
    covered = tuple(source_id for item in run.coverage for source_id in item.successful_source_ids)
    failed = tuple(source_id for item in run.coverage for source_id in item.failed_source_ids)
    unresolved: list[str] = []
    if not comparable:
        if reference_estimate is not None:
            unresolved.append(
                "iCom 接驳人民币金额按欧洲央行参考汇率估算；税费可能浮动，"
                "预计总价不是结算锁价。"
            )
        else:
            unresolved.append(
                "尚未取得 iCom 美元基础价的当日人民币参考估算；接驳税费仍未知。"
            )
    if failed:
        unresolved.append("部分已连接来源未返回可用结果：" + "、".join(failed))
    for lodging in candidate.lodgings:
        if lodging.cancellation_policy is None:
            unresolved.append(f"{lodging.property_name}的取消规则待确认")
    return FinalPlanProjection(
        option_id=candidate.id,
        date_pair_id=f"{run.intent.start_date}:{run.intent.end_date}",
        departure_date=run.intent.start_date,
        return_date=run.intent.end_date,
        total_budget_cents=package.budget.total_cents if comparable else None,
        confirmed_cny_subtotal_cents=confirmed_cny_subtotal_cents,
        estimated_icom_transfer_cny_cents=estimated_icom_transfer_cny_cents,
        estimated_total_cny_cents=estimated_total_cny_cents,
        icom_cny_reference_estimate=reference_estimate,
        optimality_status="best_verified",
        claim_boundary=run.claim_boundary,
        flight_component_id=candidate.flight.id if candidate.flight else None,
        lodging_component_ids=tuple(item.id for item in candidate.lodgings),
        transfer_component_ids=tuple(item.id for item in candidate.transfers),
        flight=(FinalFlightProjection(
            provider=candidate.flight.provider, origin=candidate.flight.origin,
            destination=candidate.flight.destination,
            outbound_flight_numbers=candidate.flight.outbound_flight_numbers,
            outbound_depart_at=candidate.flight.outbound_depart_at,
            outbound_arrive_at=candidate.flight.outbound_arrive_at,
            return_flight_numbers=candidate.flight.return_flight_numbers,
            return_depart_at=candidate.flight.return_depart_at,
            return_arrive_at=candidate.flight.return_arrive_at,
            total_for_party_cents=(
                candidate.flight.total_for_party_cents
                if candidate.flight.party_total_known
                else None
            ),
            display_amount_cents=candidate.flight.display_amount_cents,
            party_total_known=candidate.flight.party_total_known,
            price_basis=candidate.flight.price_basis,
            official_view_url=_trusted_quote_view_url(candidate.flight),
        ) if candidate.flight else None),
        lodgings=tuple(FinalLodgingProjection(
            provider=item.provider, property_name=item.property_name, area=item.area.value,
            check_in=item.check_in, check_out=item.check_out, rooms=item.rooms,
            room_name=item.room_name, breakfast_included=item.breakfast_included,
            cancellation_policy=item.cancellation_policy,
            location_convenience=item.location_convenience.value,
            location_address=item.location_address,
            nearby_location_evidence=item.nearby_location_evidence,
            location_evidence_summary=_lodging_location_evidence_summary(item),
            display_total_cents=item.total_for_party_cents,
            official_view_url=_trusted_quote_view_url(item),
        ) for item in candidate.lodgings),
        transfers=tuple(FinalTransferProjection(
            provider=item.provider, origin_area=item.origin_area.value,
            destination_area=item.destination_area.value, service_date=item.service_date,
            schedule_mode=item.schedule_mode.value,
            depart_at=item.depart_at,
            arrive_at=item.arrive_at,
            currency=item.currency,
            total_for_party_cents=item.total_for_party_cents,
            taxes_and_fees_included=item.taxes_and_fees_included,
            price_guarantee=item.price_guarantee.value,
        ) for item in candidate.transfers),
        party={"adults": run.intent.adults, "children": run.intent.children,
               "infants": run.intent.infants, "rooms": run.intent.rooms},
        covered_source_ids=covered, failed_source_ids=failed,
        price_comparability=(
            "complete_cny"
            if comparable
            else (
                "confirmed_cny_subtotal_plus_icom_estimate"
                if estimated_total_cny_cents is not None
                else "confirmed_cny_subtotal"
            )
        ),
        unresolved_items=tuple(dict.fromkeys(unresolved)),
    )


def build_final_plan_projection(
    run: FlexibleLiveAgentRun | LivePackageAgentRun,
) -> FinalPlanProjection | None:
    if isinstance(run, LivePackageAgentRun):
        return build_live_final_plan_projection(run)
    option_id = next(iter(run.recommended_option_ids), None)
    if option_id is None:
        return None
    option = next((item for item in run.ranked_options if item.option_id == option_id), None)
    if option is None:
        raise ValueError("recommended option is missing from ranked diagnostics")
    execution = next(
        (item for item in run.pair_runs if item.date_pair.id == option.date_pair_id),
        None,
    )
    package_run = execution.run.package if execution and execution.run else None
    candidate = package_run.final_candidate if package_run else None
    covered_source_ids = tuple(
        source_id
        for coverage in (execution.run.coverage if execution and execution.run else ())
        for source_id in coverage.successful_source_ids
    )
    failed_source_ids = tuple(
        source_id
        for coverage in (execution.run.coverage if execution and execution.run else ())
        for source_id in coverage.failed_source_ids
    )
    price_comparability = (
        "complete_cny"
        if package_run is not None
        and package_run.budget.is_all_in_total
        and candidate is not None
        and candidate.currency == "CNY"
        else "total_not_comparable"
    )
    unresolved_items: list[str] = []
    if price_comparability != "complete_cny":
        unresolved_items.append("尚未获得全部人数、税费和币种一致的可比总价")
    if failed_source_ids:
        unresolved_items.append(
            "部分已连接来源未返回可用结果：" + "、".join(failed_source_ids)
        )
    if run.optimality_status.value != "optimality_proven":
        unresolved_items.append("当前是已成功覆盖范围内的最优结果，尚未证明全局最低总价")
    if candidate is not None:
        for lodging in candidate.lodgings:
            if lodging.cancellation_policy is None:
                unresolved_items.append(f"{lodging.property_name}的取消规则待确认")
    return FinalPlanProjection(
        option_id=option.option_id,
        date_pair_id=option.date_pair_id,
        departure_date=option.departure_date,
        return_date=option.return_date,
        total_budget_cents=(
            option.total_budget_cents if price_comparability == "complete_cny" else None
        ),
        optimality_status=run.optimality_status.value,
        claim_boundary=run.claim_boundary,
        flight_component_id=candidate.flight.id if candidate else None,
        lodging_component_ids=tuple(item.id for item in candidate.lodgings) if candidate else (),
        transfer_component_ids=tuple(item.id for item in candidate.transfers) if candidate else (),
        flight=(
            FinalFlightProjection(
                provider=candidate.flight.provider,
                origin=candidate.flight.origin,
                destination=candidate.flight.destination,
                outbound_flight_numbers=candidate.flight.outbound_flight_numbers,
                outbound_depart_at=candidate.flight.outbound_depart_at,
                outbound_arrive_at=candidate.flight.outbound_arrive_at,
                return_flight_numbers=candidate.flight.return_flight_numbers,
                return_depart_at=candidate.flight.return_depart_at,
                return_arrive_at=candidate.flight.return_arrive_at,
                display_amount_cents=candidate.flight.display_amount_cents,
                party_total_known=candidate.flight.party_total_known,
                price_basis=candidate.flight.price_basis,
                official_view_url=_trusted_quote_view_url(candidate.flight),
            )
            if candidate
            else None
        ),
        lodgings=(
            tuple(
                FinalLodgingProjection(
                    provider=item.provider,
                    property_name=item.property_name,
                    area=item.area.value,
                    check_in=item.check_in,
                    check_out=item.check_out,
                    rooms=item.rooms,
                    room_name=item.room_name,
                    breakfast_included=item.breakfast_included,
                    cancellation_policy=item.cancellation_policy,
                    location_convenience=item.location_convenience.value,
                    location_address=item.location_address,
                    nearby_location_evidence=item.nearby_location_evidence,
                    location_evidence_summary=_lodging_location_evidence_summary(item),
                    display_total_cents=item.total_for_party_cents,
                    official_view_url=_trusted_quote_view_url(item),
                )
                for item in candidate.lodgings
            )
            if candidate
            else ()
        ),
        transfers=(
            tuple(
                FinalTransferProjection(
                    provider=item.provider,
                    origin_area=item.origin_area.value,
                    destination_area=item.destination_area.value,
                    service_date=item.service_date,
                    schedule_mode=item.schedule_mode.value,
                    depart_at=item.depart_at,
                    arrive_at=item.arrive_at,
                )
                for item in candidate.transfers
            )
            if candidate
            else ()
        ),
        party=(
            {
                "adults": execution.run.intent.adults,
                "children": execution.run.intent.children,
                "infants": execution.run.intent.infants,
                "rooms": execution.run.intent.rooms,
            }
            if execution and execution.run
            else {}
        ),
        covered_source_ids=covered_source_ids,
        failed_source_ids=failed_source_ids,
        price_comparability=price_comparability,
        unresolved_items=tuple(dict.fromkeys(unresolved_items)),
    )


def _trusted_quote_view_url(quote: Any) -> str | None:
    """Expose only provider URLs already carried by the captured evidence."""

    try:
        provider = BrowserProvider(quote.provider)
    except ValueError:
        return None
    for reference in quote.evidence_refs:
        if (
            isinstance(reference, str)
            and reference.startswith("https://")
            and _is_allowed_provider_url(provider, reference)
        ):
            return reference
    return None


def build_best_available_plan_projection(
    run: FlexibleLiveAgentRun,
) -> BestAvailablePlanProjection | None:
    """Return one useful itinerary without promoting display prices to party totals."""

    choices: list[
        tuple[
            tuple[int, int, int, str],
            Any,
            LivePackageAgentRun,
            NormalizedFlightQuote,
            tuple[NormalizedLodgingQuote, ...],
        ]
    ] = []
    for execution in run.pair_runs:
        live_run = execution.run or execution.exploration_run
        if live_run is None:
            continue
        flights: list[NormalizedFlightQuote] = []
        lodgings: list[NormalizedLodgingQuote] = []
        for result in live_run.normalization_results:
            quote = result.quote
            if not result.usable or quote is None:
                continue
            if isinstance(quote, NormalizedFlightQuote):
                amount = (
                    quote.total_for_party_cents
                    if quote.party_total_known
                    else quote.display_amount_cents
                )
                if (
                    quote.currency == "CNY"
                    and quote.availability.value == "available"
                    and quote.outbound_depart_at.date() == execution.date_pair.departure_date
                    and quote.return_depart_at.date() == execution.date_pair.return_date
                    and amount is not None
                    and amount > 0
                ):
                    flights.append(quote)
            elif isinstance(quote, NormalizedLodgingQuote):
                searchable_text = f"{quote.property_name} {quote.room_name or ''}".lower()
                cancellation = quote.cancellation_policy or ""
                if (
                    quote.currency == "CNY"
                    and quote.availability.value == "available"
                    and quote.breakfast_included is True
                    and "免费取消" in cancellation
                    and "不可取消" not in cancellation
                    and "基础" not in searchable_text
                    and "b&b" not in searchable_text
                    and "无窗" not in searchable_text
                    and "标准房" not in searchable_text
                ):
                    lodgings.append(quote)
        if not flights or not lodgings:
            continue
        flight = min(
            flights,
            key=lambda item: (
                0 if item.party_total_known else 1,
                item.total_for_party_cents
                if item.party_total_known
                else item.display_amount_cents or 10**18,
                item.id,
            ),
        )
        def lodging_rank(item: NormalizedLodgingQuote) -> tuple[int, int, str]:
            text = f"{item.property_name} {item.room_name or ''}".lower()
            if "海景" in text or "sea view" in text:
                quality_tier = 0
            elif "阳台" in text or "balcony" in text:
                quality_tier = 1
            elif any(
                term in text
                for term in ("超级豪华", "豪华", "高级", "deluxe", "superior")
            ):
                quality_tier = 2
            else:
                quality_tier = 3
            return (
                quality_tier,
                item.total_for_party_cents,
                item.id,
            )

        available_lodgings = tuple(lodgings)
        lodging_end_date = execution.date_pair.return_date

        def best_lodging_cover(
            start: date,
            available: tuple[NormalizedLodgingQuote, ...] = available_lodgings,
            end: date = lodging_end_date,
        ) -> tuple[NormalizedLodgingQuote, ...] | None:
            by_start: dict[date, list[NormalizedLodgingQuote]] = {}
            for item in available:
                if start <= item.check_in < item.check_out <= end:
                    by_start.setdefault(item.check_in, []).append(item)

            covers: list[tuple[NormalizedLodgingQuote, ...]] = []

            def visit(
                cursor: date,
                selected: tuple[NormalizedLodgingQuote, ...],
            ) -> None:
                if cursor == end:
                    covers.append(selected)
                    return
                if len(selected) >= 4:
                    return
                for item in by_start.get(cursor, ()):
                    visit(item.check_out, (*selected, item))

            visit(start, ())
            if not covers:
                return None
            return min(
                covers,
                key=lambda cover: (
                    sum(lodging_rank(item)[0] for item in cover),
                    sum(item.total_for_party_cents for item in cover),
                    len(cover),
                    tuple(item.id for item in cover),
                ),
            )

        # An overnight outbound flight means the stay starts on the local
        # arrival date.  Never fall back to the pair's departure date here:
        # that would publish a lodging quote for a night before the traveler
        # reaches the destination.
        lodging_cover = best_lodging_cover(flight.outbound_arrive_at.date())
        if lodging_cover is None:
            continue
        lodging_total = sum(item.total_for_party_cents for item in lodging_cover)
        flight_amount = (
            flight.total_for_party_cents
            if flight.party_total_known
            else flight.display_amount_cents
        )
        assert flight_amount is not None
        choices.append(
            (
                (
                    0 if flight.party_total_known else 1,
                    (
                        flight_amount + lodging_total
                        if flight.party_total_known
                        else flight_amount
                    ),
                    0 if flight.party_total_known else lodging_total,
                    execution.date_pair.id,
                ),
                execution,
                live_run,
                flight,
                lodging_cover,
            )
        )
    if not choices:
        return None
    _, execution, live_run, flight, chosen_lodgings = min(
        choices, key=lambda item: item[0]
    )
    covered = tuple(
        dict.fromkeys(
            source_id
            for coverage in live_run.coverage
            for source_id in coverage.successful_source_ids
        )
    )
    failed = tuple(
        dict.fromkeys(
            source_id
            for coverage in live_run.coverage
            for source_id in coverage.failed_source_ids
        )
    )
    unresolved = []
    if not flight.party_total_known:
        unresolved.append(
            "航班页面展示价尚未确认为全部出行人的合计价，因此不计入行程总价"
        )
    lodging_areas = {item.area.value for item in chosen_lodgings}
    if lodging_areas == {"airport_island"}:
        unresolved.extend(
            (
                "当前选中住宿位于胡鲁马累机场岛，交通便利，但不是度假岛体验",
                "目的地岛住宿本轮未形成可直接与人民币价格同口径比较的方案",
            )
        )
    elif "destination_island" in lodging_areas and "airport_island" in lodging_areas:
        unresolved.append(
            "当前采用目的地岛住宿加返程前机场岛过渡住宿；岛间接驳仍需确认人民币总价和可衔接班次"
        )
    elif "destination_island" in lodging_areas:
        unresolved.append("目的地岛住宿已找到；机场往返接驳仍需确认人民币总价和班次")
    if flight.outbound_arrive_at.date() > chosen_lodgings[0].check_in:
        unresolved.append(
            "当前酒店报价从出发日起算，航班次日抵达；预订前应再查次日入住价格"
        )
    return BestAvailablePlanProjection(
        option_id=f"best-available:{execution.date_pair.id}",
        date_pair_id=execution.date_pair.id,
        departure_date=execution.date_pair.departure_date,
        return_date=execution.date_pair.return_date,
        total_budget_cents=None,
        optimality_status="best_available_not_final",
        claim_boundary=(
            "这是基于本轮已成功返回的实时结果生成的唯一最佳可用建议，"
            "不等同于已确认的全部人员总价或全平台最低价。"
        ),
        flight_component_id=flight.id,
        lodging_component_ids=tuple(item.id for item in chosen_lodgings),
        flight=FinalFlightProjection(
            provider=flight.provider,
            origin=flight.origin,
            destination=flight.destination,
            outbound_flight_numbers=flight.outbound_flight_numbers,
            outbound_depart_at=flight.outbound_depart_at,
            outbound_arrive_at=flight.outbound_arrive_at,
            return_flight_numbers=flight.return_flight_numbers,
            return_depart_at=flight.return_depart_at,
            return_arrive_at=flight.return_arrive_at,
            display_amount_cents=(
                None if flight.party_total_known else flight.display_amount_cents
            ),
            party_total_known=flight.party_total_known,
            price_basis=flight.price_basis,
            official_view_url=_trusted_quote_view_url(flight),
        ),
        lodgings=tuple(
            FinalLodgingProjection(
                provider=item.provider,
                property_name=item.property_name,
                area=item.area.value,
                check_in=item.check_in,
                check_out=item.check_out,
                rooms=item.rooms,
                room_name=item.room_name,
                breakfast_included=item.breakfast_included,
                cancellation_policy=item.cancellation_policy,
                location_convenience=item.location_convenience.value,
                location_address=item.location_address,
                nearby_location_evidence=item.nearby_location_evidence,
                location_evidence_summary=_lodging_location_evidence_summary(item),
                display_total_cents=item.total_for_party_cents,
                official_view_url=_trusted_quote_view_url(item),
            )
            for item in chosen_lodgings
        ),
        party={
            "adults": live_run.intent.adults,
            "children": live_run.intent.children,
            "infants": live_run.intent.infants,
            "rooms": live_run.intent.rooms,
        },
        covered_source_ids=covered,
        failed_source_ids=failed,
        price_comparability="flight_display_only_lodging_total_confirmed",
        unresolved_items=tuple(unresolved),
        advisory_note=(
            "航班只显示平台页面可见价；住宿为人民币已含税同行价。"
            "未经确认的航班价与住宿价不相加。"
        ),
    )

class StartLiveFlexibleFromTextJobResponse(ApiModel):
    job: LivePlanningJobSnapshot
    replayed: bool = False
    status_url: str = Field(min_length=1)
    events_url: str = Field(min_length=1)
    boundary: str = NON_DURABLE_LIVE_PLANNING_BOUNDARY


class LiveAgentEventReplanRequest(ApiModel):
    event: LivePackageEvent
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)


class LiveAgentEventReplanResponse(ApiModel):
    run_id: str = Field(min_length=1)
    expires_at: datetime
    run: LiveEventReplanRun
    final_plan: FinalPlanProjection | None = None

    @model_serializer(mode="wrap")
    def serialize_public(self, handler: Callable[[Any], Any]) -> Any:
        return _sanitize_public_flight_totals(handler(self))


class LivePlanModificationRequest(ApiModel):
    instruction: str = Field(min_length=1, max_length=2000)
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)


class LivePlanModificationResponse(ApiModel):
    run_id: str = Field(min_length=1)
    expires_at: datetime
    modification: LivePlanModificationReceipt
    run: LivePackageAgentRun
    final_plan: FinalPlanProjection | None = None

    @model_serializer(mode="wrap")
    def serialize_public(self, handler: Callable[[Any], Any]) -> Any:
        return _sanitize_public_flight_totals(handler(self))


class StartLiveMonitorRequest(ApiModel):
    interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_checks: int = Field(default=24, ge=1, le=288)
    timeout_seconds: int = Field(default=120, ge=15, le=300)


class LiveMonitorResponse(ApiModel):
    monitor: LiveMonitorStatus


def verify_plan(request: VerifyRequest) -> VerifyResponse:
    violations = PlanVerifier().verify(request.spec, request.plan, request.context)
    valid = not any(item.severity == "error" for item in violations)
    return VerifyResponse(valid=valid, violations=violations)


async def search_offers(
    query: OfferSearchQuery,
    registry: ProviderRegistry,
) -> OfferSearchResult:
    return await registry.search(query)


async def revalidate_offer(
    offer: TravelOffer,
    registry: ProviderRegistry,
) -> TravelOffer:
    return await registry.revalidate(offer)


def create_user_quote(quote: UserQuoteInput) -> TravelOffer:
    return import_user_quote(quote)


def parse_trip_request(request: ParseTripRequest) -> RequirementParseResult:
    return ChineseRequirementParser().parse(request.text, default_year=request.default_year)


def optimize_plan(request: OptimizePlanRequest) -> OptimizePlanResponse:
    optimizer = ItineraryOptimizer()
    result = optimizer.solve(request.problem)
    plan = optimizer.to_plan(
        result,
        request.problem,
        trip_id=request.trip_id,
        plan_id=request.plan_id,
        version=request.version,
    )
    return OptimizePlanResponse(result=result, plan=plan)


def repair_plan(request: RepairPlanRequest) -> WorkflowResult:
    workflow = PlanningWorkflow(max_repair_iterations=request.max_iterations)
    return workflow.run(request.spec, request.plan, request.context)


def replan_after_event(request: ReplanRequest) -> LocalReplanResult:
    return LocalReplanner(max_repair_iterations=request.max_iterations).replan(
        request.spec,
        request.plan,
        request.event,
        request.context,
        request.dependencies,
        request.replacements,
    )
