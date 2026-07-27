from pydantic import BaseModel, ConfigDict

from tripchord.domain.common import Coordinates
from tripchord.domain.itinerary import PlanVersion, Violation
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import RouteMode
from tripchord.domain.trip import TripSpec
from tripchord.planning import PlanVerifier
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult, ProviderRegistry
from tripchord.providers.user_snapshot import UserQuoteInput, import_user_quote


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyRequest(ApiModel):
    spec: TripSpec
    plan: PlanVersion


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


def verify_plan(request: VerifyRequest) -> VerifyResponse:
    violations = PlanVerifier().verify(request.spec, request.plan)
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
