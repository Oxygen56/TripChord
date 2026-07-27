from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.domain.trip import TripSpec


class ActivityAvailability(DomainModel):
    date: date
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)

    @model_validator(mode="after")
    def validate_window(self) -> ActivityAvailability:
        if self.end_minute <= self.start_minute:
            raise ValueError("availability end must be after start")
        return self


class ActivityCandidate(DomainModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    duration_minutes: int = Field(gt=0, le=720)
    cost_cents: int = Field(default=0, ge=0)
    utility: int = Field(default=100, ge=0, le=10000)
    must_visit: bool = False
    availability: tuple[ActivityAvailability, ...]
    source_refs: tuple[str, ...] = ()
    location_name: str | None = None


class TravelTime(DomainModel):
    origin_id: str
    destination_id: str
    minutes: int = Field(ge=0, le=1440)
    source_ref: str | None = None
    estimated: bool = False


class PlanningProblem(DomainModel):
    trip: TripSpec
    activities: tuple[ActivityCandidate, ...]
    travel_times: tuple[TravelTime, ...] = ()
    timezone: str = "Asia/Shanghai"
    solver_time_limit_seconds: float = Field(default=10, gt=0, le=120)

    @model_validator(mode="after")
    def validate_unique_activities(self) -> PlanningProblem:
        ids = [activity.id for activity in self.activities]
        if len(ids) != len(set(ids)):
            raise ValueError("activity ids must be unique")
        return self


class ScheduledActivity(DomainModel):
    activity_id: str
    title: str
    date: date
    start_minute: int
    end_minute: int
    cost_cents: int
    utility: int
    source_refs: tuple[str, ...] = ()
    location_name: str | None = None


class OptimizationResult(DomainModel):
    status: str
    objective_value: float
    scheduled: tuple[ScheduledActivity, ...]
    skipped_activity_ids: tuple[str, ...]
    total_cost_cents: int
    total_utility: int
    solver_wall_time_seconds: float


class PlanningInfeasible(RuntimeError):
    pass
