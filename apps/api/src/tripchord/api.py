import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from tripchord.agents.companion_control_tools import (
    BrowserCompanionBuildReconcileResponse,
)
from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexiblePackageConstraints,
)
from tripchord.agents.live_jobs import LivePlanningJobSnapshot
from tripchord.agents.live_monitor import LiveMonitorStatus
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LiveEventReplanRun,
    LivePackageAgentRun,
    LivePackageEvent,
)
from tripchord.agents.memory import MemoryRecord
from tripchord.agents.models import PreferenceConstitution
from tripchord.agents.package_request import (
    HybridPackageRequirementResult,
    PackageRequirementRequest,
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
from tripchord.planning.package import PackageIntent
from tripchord.planning.policy import ReplanPreference
from tripchord.planning.problem import OptimizationResult, PlanningProblem
from tripchord.planning.replanner import LocalReplanner, LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.stay_plans import StayPlanCandidateSet
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowResult
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult, ProviderRegistry
from tripchord.providers.browser_bridge import BrowserSearchQuery
from tripchord.providers.user_snapshot import UserQuoteInput, import_user_quote


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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


class LiveFlexibleAgentPlanningRequest(ApiModel):
    window: FlexibleTravelWindow
    calendars: tuple[PlatformFareCalendar, ...] = ()
    coverage_mode: LiveCoverageMode = LiveCoverageMode.STRICT
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)
    total_timeout_seconds: int | None = Field(default=None, ge=60, le=3600)
    max_pairs: int = Field(default=3, ge=1, le=8)
    publication_refresh_minimum_options: int = Field(default=2, ge=0, le=2)
    constraints: FlexiblePackageConstraints = FlexiblePackageConstraints()
    stay_plan_candidate_set: StayPlanCandidateSet | None = None


class LiveFlexiblePairRunHandle(ApiModel):
    date_pair_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    expires_at: datetime


class LiveFlexibleAgentPlanningResponse(ApiModel):
    run: FlexibleLiveAgentRun
    cached_pair_runs: tuple[LiveFlexiblePairRunHandle, ...] = ()


class LiveFlexibleFromTextPlanningRequest(ApiModel):
    requirement: PackageRequirementRequest
    calendars: tuple[PlatformFareCalendar, ...] = ()
    coverage_mode: LiveCoverageMode = LiveCoverageMode.STRICT
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)
    total_timeout_seconds: int | None = Field(default=None, ge=60, le=3600)
    max_pairs: int = Field(default=3, ge=1, le=8)
    publication_refresh_minimum_options: int = Field(default=2, ge=0, le=2)
    stay_plan_candidate_set: StayPlanCandidateSet | None = None


LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY = (
    "默认使用确定性优先的本地需求解析；模型增强未启用。仅当关键字段完整时才会"
    "启动实时浏览器搜索，报价排序只对本轮抽样日期与可见证据有效。"
)


class LiveFlexibleFromTextPlanningResponse(ApiModel):
    interpretation: HybridPackageRequirementResult
    run: FlexibleLiveAgentRun | None = None
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

    @model_validator(mode="after")
    def validate_model_trace_counts(self) -> "LiveFlexibleFromTextPlanningResponse":
        if self.model_trace_count != (
            self.model_trace_success_count + self.model_trace_failure_count
        ):
            raise ValueError("model trace success and failure counts must add up")
        return self


class StartLiveFlexibleFromTextJobResponse(ApiModel):
    job: LivePlanningJobSnapshot
    replayed: bool = False
    status_url: str = Field(min_length=1)
    events_url: str = Field(min_length=1)
    boundary: str = (
        "POST 只创建本机进程内任务并快速返回；GET/SSE 读取状态，DELETE 请求取消。"
        "任务不会跨进程重启恢复，也不是持久化生产队列。"
    )


class LiveAgentEventReplanRequest(ApiModel):
    event: LivePackageEvent
    timeout_seconds: int | None = Field(default=None, ge=15, le=300)


class LiveAgentEventReplanResponse(ApiModel):
    run_id: str = Field(min_length=1)
    expires_at: datetime
    run: LiveEventReplanRun


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
