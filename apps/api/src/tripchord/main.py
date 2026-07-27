import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord import __version__
from tripchord.api import (
    CreatePlanningJobRequest,
    CreateWorkspaceRequest,
    GeocodeRequest,
    OptimizePlanRequest,
    OptimizePlanResponse,
    ParseTripRequest,
    PlaceSearchRequest,
    RepairPlanRequest,
    ReplanRequest,
    RouteRequest,
    SavePlanRequest,
    StartTripPlanningRequest,
    StartTripPlanningResponse,
    VerifyRequest,
    VerifyResponse,
    WeatherRequest,
    WorkspaceReplanRequest,
    WorkspaceReplanResponse,
    create_user_quote,
    optimize_plan,
    parse_trip_request,
    repair_plan,
    replan_after_event,
    revalidate_offer,
    search_offers,
    verify_plan,
)
from tripchord.auth import Principal, get_principal
from tripchord.config import get_settings
from tripchord.domain.common import Coordinates
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import Place, RouteLeg, WeatherWindow
from tripchord.jobs import (
    JobConflictError,
    JobNotFoundError,
    JobRepository,
    JobSnapshot,
    JobStatus,
    PlanningJobRunner,
)
from tripchord.observability import configure_logging, metrics, observe_request
from tripchord.persistence import Database, WorkspaceRepository
from tripchord.persistence.repository import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceSnapshot,
)
from tripchord.planning.adaptive import AdaptiveReplanner
from tripchord.planning.assembler import PlanningProblemAssembler, ReplayPlaceCatalog
from tripchord.planning.policy import ReplanPolicySelector
from tripchord.planning.problem import PlanningInfeasible
from tripchord.planning.repair import PlanDiff, diff_plans
from tripchord.planning.replanner import LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.workflow import WorkflowResult
from tripchord.providers.amap import AmapTravelDataProvider
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult
from tripchord.providers.factory import build_amap_provider, build_provider_registry
from tripchord.providers.user_snapshot import UserQuoteInput
from tripchord.rate_limit import RateLimiter

settings = get_settings()
configure_logging(settings.log_level)
root = Path(__file__).resolve().parents[4]
providers = build_provider_registry(settings, root)
amap = build_amap_provider(settings)
database = Database(settings.database_url)
job_runner = PlanningJobRunner(database)
rate_limiter = RateLimiter(
    limit=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    redis_url=settings.redis_url,
)
planning_assembler = PlanningProblemAssembler(
    ReplayPlaceCatalog(root / "data" / "replay" / "places.json")
)
replan_policy = ReplanPolicySelector.from_path(
    root / "training" / "artifacts" / "replan-policy.json"
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await database.create_schema()
    await job_runner.recover()
    yield
    await rate_limiter.close()
    await database.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    async for session in database.session():
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_job_runner() -> PlanningJobRunner:
    return job_runner


RunnerDep = Annotated[PlanningJobRunner, Depends(get_job_runner)]

app = FastAPI(
    title="TripChord API",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(observe_request)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "tripchord", "version": __version__}


@app.get("/ready")
async def ready() -> dict[str, str]:
    try:
        async with database.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is not ready") from exc
    return {
        "status": "ready",
        "database": "ok",
        "rate_limit_backend": rate_limiter.backend,
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    return metrics.render()


@app.post("/api/v1/plans/verify", response_model=VerifyResponse)
async def verify_endpoint(request: VerifyRequest) -> VerifyResponse:
    return verify_plan(request)


@app.post("/api/v1/offers/search", response_model=OfferSearchResult)
async def offer_search_endpoint(
    query: OfferSearchQuery,
    principal: PrincipalDep,
) -> OfferSearchResult:
    await rate_limiter.check(principal.tenant_id, "offer-search")
    return await search_offers(query, providers)


@app.post("/api/v1/offers/revalidate", response_model=TravelOffer)
async def offer_revalidate_endpoint(
    offer: TravelOffer,
    principal: PrincipalDep,
) -> TravelOffer:
    await rate_limiter.check(principal.tenant_id, "offer-revalidate")
    return await revalidate_offer(offer, providers)


@app.post("/api/v1/offers/user-snapshot", response_model=TravelOffer)
async def user_quote_endpoint(
    quote: UserQuoteInput,
    principal: PrincipalDep,
) -> TravelOffer:
    await rate_limiter.check(principal.tenant_id, "user-quote")
    return create_user_quote(quote)


@app.post("/api/v1/trips/parse", response_model=RequirementParseResult)
async def parse_trip_endpoint(request: ParseTripRequest) -> RequirementParseResult:
    return parse_trip_request(request)


@app.post("/api/v1/plans/optimize", response_model=OptimizePlanResponse)
async def optimize_plan_endpoint(request: OptimizePlanRequest) -> OptimizePlanResponse:
    try:
        return optimize_plan(request)
    except PlanningInfeasible as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/plans/repair", response_model=WorkflowResult)
async def repair_plan_endpoint(request: RepairPlanRequest) -> WorkflowResult:
    return repair_plan(request)


@app.post("/api/v1/plans/replan", response_model=LocalReplanResult)
async def replan_endpoint(request: ReplanRequest) -> LocalReplanResult:
    return replan_after_event(request)


@app.post(
    "/api/v1/workspaces",
    response_model=WorkspaceSnapshot,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_endpoint(
    request: CreateWorkspaceRequest,
    session: SessionDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).create(
            request.spec, request.title, idempotency_key
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceSnapshot)
async def get_workspace_endpoint(
    workspace_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/api/v1/workspaces/{workspace_id}/plans", response_model=WorkspaceSnapshot)
async def save_workspace_plan_endpoint(
    workspace_id: str,
    request: SavePlanRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).save_plan(
            workspace_id, request.plan
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/v1/workspaces/{workspace_id}/plans/{from_version}/diff/{to_version}",
    response_model=PlanDiff,
)
async def compare_workspace_plans_endpoint(
    workspace_id: str,
    from_version: int,
    to_version: int,
    session: SessionDep,
    principal: PrincipalDep,
) -> PlanDiff:
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    plans = {plan.version: plan for plan in workspace.plans}
    if from_version not in plans or to_version not in plans:
        raise HTTPException(status_code=404, detail="plan version not found")
    return diff_plans(plans[from_version], plans[to_version])


@app.post(
    "/api/v1/workspaces/{workspace_id}/events/replan",
    response_model=WorkspaceReplanResponse,
)
async def persisted_replan_endpoint(
    workspace_id: str,
    request: WorkspaceReplanRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceReplanResponse:
    await rate_limiter.check(principal.tenant_id, "replan")
    repository = WorkspaceRepository(session, principal.tenant_id)
    try:
        workspace = await repository.get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if not workspace.plans:
        raise HTTPException(status_code=409, detail="workspace has no plan to replan")
    problem = await JobRepository(session, principal.tenant_id).latest_problem(workspace_id)
    result = AdaptiveReplanner(
        replan_policy,
        max_repair_iterations=request.max_iterations,
    ).replan(
        workspace.spec,
        workspace.plans[-1],
        request.event,
        request.preference,
        problem,
        request.context,
        request.dependencies,
        request.replacements,
    )
    plan_to_store = result.final_plan if result.status == "ready" and result.diff.changed else None
    try:
        updated = await repository.record_replan(
            workspace_id,
            request.event,
            result,
            plan_to_store,
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkspaceReplanResponse(result=result, workspace=updated)


@app.post(
    "/api/v1/workspaces/{workspace_id}/jobs/planning",
    response_model=JobSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_planning_job_endpoint(
    workspace_id: str,
    request: CreatePlanningJobRequest,
    session: SessionDep,
    runner: RunnerDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobSnapshot:
    await rate_limiter.check(principal.tenant_id, "planning-job")
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if request.problem.trip != workspace.spec:
        raise HTTPException(status_code=409, detail="planning problem trip differs from workspace")
    try:
        job = await JobRepository(session, principal.tenant_id).create(
            workspace_id, request.problem, idempotency_key
        )
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner.enqueue(job.id, workspace_id, request.problem, principal.tenant_id)
    return job


@app.post(
    "/api/v1/trips/plan",
    response_model=StartTripPlanningResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_trip_planning_endpoint(
    request: StartTripPlanningRequest,
    session: SessionDep,
    runner: RunnerDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StartTripPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "trip-plan")
    try:
        problem = planning_assembler.assemble(request.spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).create(
            request.spec, request.title, idempotency_key
        )
        job = await JobRepository(session, principal.tenant_id).create(
            workspace.id, problem, idempotency_key
        )
    except (WorkspaceConflictError, JobConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner.enqueue(job.id, workspace.id, problem, principal.tenant_id)
    return StartTripPlanningResponse(
        workspace=workspace,
        job=job,
        data_mode="replay",
        candidate_count=len(problem.activities),
    )


@app.get(
    "/api/v1/workspaces/{workspace_id}/jobs/{job_id}",
    response_model=JobSnapshot,
)
async def get_planning_job_endpoint(
    workspace_id: str,
    job_id: str,
    runner: RunnerDep,
    principal: PrincipalDep,
) -> JobSnapshot:
    try:
        job = await runner.get(job_id, principal.tenant_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if job.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/v1/workspaces/{workspace_id}/jobs/{job_id}/events")
async def stream_planning_job_endpoint(
    workspace_id: str,
    job_id: str,
    runner: RunnerDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        last_payload = ""
        while True:
            try:
                job = await runner.get(job_id, principal.tenant_id)
            except JobNotFoundError:
                yield 'event: error\ndata: {"detail":"job not found"}\n\n'
                return
            if job.workspace_id != workspace_id:
                yield 'event: error\ndata: {"detail":"job not found"}\n\n'
                return
            payload = json.dumps(job.model_dump(mode="json"), ensure_ascii=False)
            if payload != last_payload:
                yield f"event: job\ndata: {payload}\n\n"
                last_payload = payload
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def require_amap() -> AmapTravelDataProvider:
    if amap is None:
        raise HTTPException(
            status_code=503,
            detail="AMap provider is not configured; set AMAP_API_KEY",
        )
    return amap


@app.post("/api/v1/places/geocode", response_model=Coordinates)
async def geocode_endpoint(request: GeocodeRequest) -> Coordinates:
    provider = require_amap()
    return await provider.geocode(request.address, request.city)


@app.post("/api/v1/places/search", response_model=tuple[Place, ...])
async def place_search_endpoint(request: PlaceSearchRequest) -> tuple[Place, ...]:
    provider = require_amap()
    return await provider.search_places(
        request.keywords,
        request.city,
        types=request.types,
        limit=request.limit,
    )


@app.post("/api/v1/routes", response_model=RouteLeg)
async def route_endpoint(request: RouteRequest) -> RouteLeg:
    provider = require_amap()
    return await provider.route(
        request.origin,
        request.destination,
        request.mode,
        city=request.city,
    )


@app.post("/api/v1/weather", response_model=tuple[WeatherWindow, ...])
async def weather_endpoint(request: WeatherRequest) -> tuple[WeatherWindow, ...]:
    provider = require_amap()
    return await provider.weather(
        request.adcode,
        request.coordinates,
    )
