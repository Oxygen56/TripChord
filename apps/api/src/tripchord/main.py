from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from tripchord import __version__
from tripchord.api import (
    GeocodeRequest,
    OptimizePlanRequest,
    OptimizePlanResponse,
    ParseTripRequest,
    PlaceSearchRequest,
    RepairPlanRequest,
    RouteRequest,
    VerifyRequest,
    VerifyResponse,
    WeatherRequest,
    create_user_quote,
    optimize_plan,
    parse_trip_request,
    repair_plan,
    revalidate_offer,
    search_offers,
    verify_plan,
)
from tripchord.config import get_settings
from tripchord.domain.common import Coordinates
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import Place, RouteLeg, WeatherWindow
from tripchord.planning.problem import PlanningInfeasible
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.workflow import WorkflowResult
from tripchord.providers.amap import AmapTravelDataProvider
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult
from tripchord.providers.factory import build_amap_provider, build_provider_registry
from tripchord.providers.user_snapshot import UserQuoteInput

settings = get_settings()
root = Path(__file__).resolve().parents[4]
providers = build_provider_registry(settings, root)
amap = build_amap_provider(settings)

app = FastAPI(
    title="TripChord API",
    version=__version__,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "tripchord", "version": __version__}


@app.post("/api/v1/plans/verify", response_model=VerifyResponse)
async def verify_endpoint(request: VerifyRequest) -> VerifyResponse:
    return verify_plan(request)


@app.post("/api/v1/offers/search", response_model=OfferSearchResult)
async def offer_search_endpoint(query: OfferSearchQuery) -> OfferSearchResult:
    return await search_offers(query, providers)


@app.post("/api/v1/offers/revalidate", response_model=TravelOffer)
async def offer_revalidate_endpoint(offer: TravelOffer) -> TravelOffer:
    return await revalidate_offer(offer, providers)


@app.post("/api/v1/offers/user-snapshot", response_model=TravelOffer)
async def user_quote_endpoint(quote: UserQuoteInput) -> TravelOffer:
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
