"""Booking protection, protected constraints and event-loop core (v0.6).

TripChord never reads platform orders to confirm a booking: a booking exists
only because the user explicitly says so.  This module is the deterministic
core of the v0.6 contract:

- :class:`BookingChecklist` / :class:`BookingItem` — what the user must confirm
  before a Booking Fact can be created.
- :class:`UserBookingAcknowledgement` — the explicit user action that may create
  a Booking Fact.  Opening a link, an Agent output or platform page text can
  never create one.
- :class:`BookingFact` — an append-only fact binding a protected component to
  the explicit acknowledgement that produced it.
- :class:`ProtectedComponentConstraint` — a persistent invariant that survives
  candidate generation, optimizer, Planner, Verifier, Repair, ReVerifier,
  Safety Gate and every replan until it is explicitly overridden.
- :class:`ConstraintOverrideRequest` — an explicit, audited request to un-protect
  a component; it never auto-applies.
- :class:`BookingImpact` — the Impact Analyzer result: which protected
  components are affected by an event and whether any protected component was
  silently modified.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel

BOOKING_SCHEMA_VERSION = "tripchord-booking-v1"


class BookingFactSource(StrEnum):
    """Only explicit user acknowledgement may create a Booking Fact."""

    USER_ACKNOWLEDGEMENT = "user_acknowledgement"


class BookingItemState(StrEnum):
    PENDING = "pending"
    ACKNOWLEDGED = "acknowledged"


class ConstraintOverrideState(StrEnum):
    REQUESTED = "requested"
    APPLIED = "applied"
    REJECTED = "rejected"


class BookingItem(DomainModel):
    """One explicit thing the user must confirm for a component."""

    item_id: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    checklist_id: str = Field(min_length=1)
    state: BookingItemState = BookingItemState.PENDING
    acknowledged_at: datetime | None = None

    @model_validator(mode="after")
    def validate_ack(self) -> Self:
        if self.state is BookingItemState.ACKNOWLEDGED:
            if self.acknowledged_at is None:
                raise ValueError("acknowledged booking items require a timestamp")
        elif self.acknowledged_at is not None:
            raise ValueError("pending booking items cannot carry an acknowledgement time")
        return self


class BookingChecklist(DomainModel):
    """The set of confirmations required before a component may be booked."""

    checklist_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    items: tuple[BookingItem, ...] = ()

    def is_complete(self) -> bool:
        return bool(self.items) and all(
            item.state is BookingItemState.ACKNOWLEDGED for item in self.items
        )


class UserBookingAcknowledgement(DomainModel):
    """The explicit, audited user action that can create a Booking Fact."""

    acknowledgement_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    checklist_id: str = Field(min_length=1)
    acknowledged_at: datetime
    user_token_sha256: str = Field(min_length=64, max_length=64)

    def acknowledgement_sha256(self) -> str:
        canonical = {
            "schema": "tripchord-booking-acknowledgement-v1",
            "acknowledgement_id": self.acknowledgement_id,
            "plan_version": self.plan_version,
            "component_id": self.component_id,
            "checklist_id": self.checklist_id,
            "acknowledged_at": self.acknowledged_at.isoformat(),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class BookingFact(DomainModel):
    """Append-only fact that one component is protected by a user booking."""

    fact_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    source: BookingFactSource = BookingFactSource.USER_ACKNOWLEDGEMENT
    acknowledgement: UserBookingAcknowledgement
    created_at: datetime

    def fact_sha256(self) -> str:
        canonical = {
            "schema": "tripchord-booking-fact-v1",
            "fact_id": self.fact_id,
            "plan_version": self.plan_version,
            "component_id": self.component_id,
            "source": self.source.value,
            "acknowledgement_sha256": self.acknowledgement.acknowledgement_sha256(),
            "created_at": self.created_at.isoformat(),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ProtectedComponentConstraint(DomainModel):
    """A persistent invariant protecting one booked component."""

    constraint_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    booking_fact_sha256: str = Field(min_length=64, max_length=64)
    protected_attributes: tuple[str, ...] = (
        "identity",
        "provider",
        "dates",
        "travelers",
        "room_count",
        "price",
    )

    def protects(self, component_id: str) -> bool:
        return component_id == self.component_id


class ConstraintOverrideRequest(DomainModel):
    """An explicit, audited request to un-protect one component."""

    request_id: str = Field(min_length=1)
    constraint_id: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    requested_by_token_sha256: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=400)
    state: ConstraintOverrideState = ConstraintOverrideState.REQUESTED
    created_at: datetime
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        if self.state is ConstraintOverrideState.REQUESTED:
            if self.resolved_at is not None:
                raise ValueError("a requested override cannot carry a resolution time")
        elif self.resolved_at is None:
            raise ValueError("applied or rejected overrides require a resolution time")
        return self


class BookingImpact(DomainModel):
    """Impact Analyzer result for one event or replan round."""

    event_id: str | None = None
    affected_protected_component_ids: tuple[str, ...] = ()
    unaffected_protected_component_ids: tuple[str, ...] = ()
    protected_component_silently_modified: bool = False
    blocked_reason: str | None = None

    @model_validator(mode="after")
    def validate_impact(self) -> Self:
        if self.protected_component_silently_modified and self.blocked_reason is None:
            raise ValueError("silent protected modification requires a blocked reason")
        return self


class BookingLedger(DomainModel):
    """Append-only in-memory ledger of booking facts and overrides for one plan."""

    plan_version: str = Field(min_length=1)
    facts: tuple[BookingFact, ...] = ()
    constraints: tuple[ProtectedComponentConstraint, ...] = ()
    overrides: tuple[ConstraintOverrideRequest, ...] = ()
    checklists: tuple[BookingChecklist, ...] = ()

    def add_fact(
        self,
        fact: BookingFact,
        acknowledgement: UserBookingAcknowledgement,
    ) -> BookingLedger:
        if fact.acknowledgement.acknowledgement_id != acknowledgement.acknowledgement_id:
            raise ValueError(
                "booking fact must reference the exact acknowledgement that created it"
            )
        if any(existing.component_id == fact.component_id for existing in self.facts):
            raise ValueError(
                f"component {fact.component_id} is already protected by a booking fact"
            )
        constraint = ProtectedComponentConstraint(
            constraint_id=f"constraint-{fact.component_id}",
            plan_version=self.plan_version,
            component_id=fact.component_id,
            booking_fact_sha256=fact.fact_sha256(),
        )
        return self.model_copy(
            update={
                "facts": (*self.facts, fact),
                "constraints": (*self.constraints, constraint),
            }
        )

    def constraint_for(self, component_id: str) -> ProtectedComponentConstraint | None:
        matches = [
            constraint
            for constraint in self.constraints
            if constraint.protects(component_id)
        ]
        if not matches:
            return None
        return matches[-1]

    def is_protected(self, component_id: str) -> bool:
        return self.constraint_for(component_id) is not None

    def override_requests_open(self, component_id: str) -> tuple[ConstraintOverrideRequest, ...]:
        return tuple(
            override
            for override in self.overrides
            if override.component_id == component_id
            and override.state is ConstraintOverrideState.REQUESTED
        )
