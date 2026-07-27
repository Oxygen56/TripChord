from pydantic import BaseModel, ConfigDict, Field

from tripchord.domain.common import Coordinates
from tripchord.domain.events import PlanEvent
from tripchord.domain.itinerary import ItineraryItem, PlanVersion, Violation
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import RouteMode
from tripchord.domain.trip import TripSpec
from tripchord.jobs import JobSnapshot
from tripchord.persistence.repository import WorkspaceSnapshot
from tripchord.planning import ChineseRequirementParser, ItineraryOptimizer, PlanVerifier
from tripchord.planning.impact import PlanDependency
from tripchord.planning.problem import OptimizationResult, PlanningProblem
from tripchord.planning.replanner import LocalReplanner, LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.verifier import VerificationContext
from tripchord.planning.workflow import PlanningWorkflow, WorkflowResult
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult, ProviderRegistry
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


class WorkspaceReplanResponse(ApiModel):
    result: LocalReplanResult
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
