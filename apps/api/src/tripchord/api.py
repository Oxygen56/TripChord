from pydantic import BaseModel, ConfigDict

from tripchord.domain.itinerary import PlanVersion, Violation
from tripchord.domain.trip import TripSpec
from tripchord.planning import PlanVerifier


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VerifyRequest(ApiModel):
    spec: TripSpec
    plan: PlanVersion


class VerifyResponse(ApiModel):
    valid: bool
    violations: tuple[Violation, ...]


def verify_plan(request: VerifyRequest) -> VerifyResponse:
    violations = PlanVerifier().verify(request.spec, request.plan)
    valid = not any(item.severity == "error" for item in violations)
    return VerifyResponse(valid=valid, violations=violations)
