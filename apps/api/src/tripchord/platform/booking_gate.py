"""Booking protection consumption gate (v0.6 wiring).

v0.6 landed the deterministic core (:mod:`tripchord.platform.booking`):
``BookingChecklist``, ``BookingFact``, ``ProtectedComponentConstraint``,
``ConstraintOverrideRequest`` and ``BookingImpact``.  This module wires that
core into the *consumption path*: candidate generation, Optimizer, Planner,
Verifier, Repair, ReVerifier, Safety Gate and every replan must consult the
same protected-constraint set.  :class:`BookingProtectionGate` is the single
deterministic gate every consumer calls.

The gate:

- refuses silent modification (remove / re-identify / re-date / re-count /
  re-price) of any protected component unless an explicit override has been
  *applied* for it;
- returns a typed :class:`BookingImpact` describing which protected components
  are affected, which are preserved, and (never) a silent modification;
- treats an unresolved change as *blocked*: the event enters a user-handling
  state and is never auto-replaced.

Consumers: planning replan, Verifier/ReVerifier, Safety Gate, live event
replan and the five anti-surface E2E acceptance all call :meth:`evaluate_diff`
with a component-level before/after diff.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.platform.booking import (
    BookingFact,
    BookingImpact,
    BookingLedger,
    ConstraintOverrideRequest,
    ConstraintOverrideState,
)

BOOKING_GATE_SCHEMA_VERSION = "tripchord-booking-gate-v1"


class ComponentChangeSet(DomainModel):
    """A before/after component diff the gate can audit.

    Consumers derive this from a replan or repair round: which components were
    removed, added, changed (identity/date/traveller/room/price) and which were
    preserved unchanged.
    """

    plan_version: str = Field(min_length=1)
    removed_component_ids: tuple[str, ...] = ()
    added_component_ids: tuple[str, ...] = ()
    changed_component_ids: tuple[str, ...] = ()
    preserved_component_ids: tuple[str, ...] = ()
    event_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (
            self.removed_component_ids
            or self.added_component_ids
            or self.changed_component_ids
        )


class BookingProtectionGate:
    """Deterministic consumption gate for protected (booked) components.

    The gate is constructed once per plan with its append-only booking ledger.
    Every planning/replan/verifier/safety-gate entry point calls
    :meth:`evaluate_diff` before applying any component-level change.
    """

    def __init__(self, ledger: BookingLedger, *, now: datetime | None = None) -> None:
        self._ledger = ledger
        self._now = now or datetime.now(UTC)

    @property
    def ledger(self) -> BookingLedger:
        return self._ledger

    def is_protected(self, component_id: str) -> bool:
        return self._ledger.is_protected(component_id)

    def protected_component_ids(self) -> tuple[str, ...]:
        return tuple(
            fact.component_id for fact in self._ledger.facts
        )

    def has_applied_override(self, component_id: str) -> bool:
        return any(
            override.component_id == component_id
            and override.state is ConstraintOverrideState.APPLIED
            for override in self._ledger.overrides
        )

    def _blocking_reason(
        self,
        component_id: str,
        change_kind: str,
    ) -> str:
        constraint = self._ledger.constraint_for(component_id)
        fact_sha = constraint.booking_fact_sha256 if constraint is not None else "unknown"
        return (
            f"component {component_id!r} is protected by a booking fact "
            f"({fact_sha[:12]}…) and {change_kind}; an explicit override must be "
            "applied before any change, or the event enters the user-handling state"
        )

    def evaluate_diff(
        self,
        change_set: ComponentChangeSet,
    ) -> BookingImpact:
        """Audit one component diff against the protected set.

        Returns a :class:`BookingImpact`.  Any protected component that would be
        removed or changed is reported as affected and blocked; nothing is
        silently modified.  Protected components preserved unchanged are
        reported as unaffected.  Protected components that would be modified
        despite an open (un-applied) override request are still blocked.
        """
        affected: list[str] = []
        unaffected: list[str] = []
        for component_id in change_set.removed_component_ids:
            if self.is_protected(component_id) and not self.has_applied_override(component_id):
                affected.append(component_id)
        for component_id in change_set.changed_component_ids:
            if self.is_protected(component_id) and not self.has_applied_override(component_id):
                affected.append(component_id)
        for component_id in change_set.preserved_component_ids:
            if self.is_protected(component_id):
                unaffected.append(component_id)

        affected = sorted(set(affected))
        unaffected = sorted(set(unaffected))
        silently_modified = False
        blocked_reason: str | None = None
        if affected:
            # A protected change is *not* silent — it is blocked and reported.
            silently_modified = False
            blocked_reason = "; ".join(
                self._blocking_reason(
                    component_id,
                    "removed" if component_id in change_set.removed_component_ids else "changed",
                )
                for component_id in affected
            )
        return BookingImpact(
            event_id=change_set.event_id,
            affected_protected_component_ids=tuple(affected),
            unaffected_protected_component_ids=tuple(unaffected),
            protected_component_silently_modified=silently_modified,
            blocked_reason=blocked_reason,
        )

    def assert_no_silent_modification(self, change_set: ComponentChangeSet) -> BookingImpact:
        """Raise when a protected component would be modified by a change set.

        Convenience for deterministic consumers (Safety Gate, Verifier) that
        must fail closed rather than return a plan.  The returned impact is the
        blocking impact.
        """
        impact = self.evaluate_diff(change_set)
        if impact.affected_protected_component_ids:
            raise ProtectedComponentModificationError(impact)
        return impact


class ProtectedComponentModificationError(ValueError):
    """A protected component would be modified without an applied override."""

    def __init__(self, impact: BookingImpact) -> None:
        self.impact = impact
        super().__init__(impact.blocked_reason or "protected component modification blocked")


class BookingService:
    """Deterministic service creating facts and override requests.

    The service enforces the v0.6 contract: only an explicit user
    acknowledgement may create a Booking Fact; override requests are audited
    and never auto-apply.
    """

    def __init__(
        self,
        ledger: BookingLedger,
        *,
        now: datetime | None = None,
    ) -> None:
        self._ledger = ledger
        self._now = now or datetime.now(UTC)

    @property
    def ledger(self) -> BookingLedger:
        return self._ledger

    def acknowledge_component(
        self,
        *,
        plan_version: str,
        component_id: str,
        checklist_id: str,
        acknowledgement_id: str,
        user_token_sha256: str,
    ) -> tuple[BookingLedger, BookingFact]:
        """Create a Booking Fact from an explicit user acknowledgement."""
        from tripchord.platform.booking import (
            BookingFact,
            BookingFactSource,
            UserBookingAcknowledgement,
        )

        acknowledgement = UserBookingAcknowledgement(
            acknowledgement_id=acknowledgement_id,
            plan_version=plan_version,
            component_id=component_id,
            checklist_id=checklist_id,
            acknowledged_at=self._now,
            user_token_sha256=user_token_sha256,
        )
        fact = BookingFact(
            fact_id=f"fact-{component_id}",
            plan_version=plan_version,
            component_id=component_id,
            source=BookingFactSource.USER_ACKNOWLEDGEMENT,
            acknowledgement=acknowledgement,
            created_at=self._now,
        )
        updated = self._ledger.add_fact(fact, acknowledgement)
        self._ledger = updated
        return updated, fact

    def request_override(
        self,
        *,
        plan_version: str,
        component_id: str,
        requested_by_token_sha256: str,
        reason: str,
        request_id: str | None = None,
    ) -> tuple[BookingLedger, ConstraintOverrideRequest]:
        """Record an explicit, audited request to un-protect a component."""
        if not self._ledger.is_protected(component_id):
            raise ValueError(f"component {component_id!r} is not protected")
        request = ConstraintOverrideRequest(
            request_id=request_id or f"override-{component_id}",
            constraint_id=f"constraint-{component_id}",
            component_id=component_id,
            plan_version=plan_version,
            requested_by_token_sha256=requested_by_token_sha256,
            reason=reason,
            created_at=self._now,
        )
        updated = self._ledger.model_copy(
            update={"overrides": (*self._ledger.overrides, request)}
        )
        self._ledger = updated
        return updated, request

    def resolve_override(
        self,
        request_id: str,
        *,
        apply: bool,
        resolved_at: datetime | None = None,
    ) -> tuple[BookingLedger, ConstraintOverrideRequest]:
        """Resolve an override request (applied or rejected), with audit trail."""
        target: ConstraintOverrideRequest | None = None
        for override in self._ledger.overrides:
            if override.request_id == request_id:
                target = override
                break
        if target is None:
            raise ValueError(f"unknown override request: {request_id}")
        if target.state is not ConstraintOverrideState.REQUESTED:
            raise ValueError(f"override {request_id} is already resolved")
        resolved = target.model_copy(
            update={
                "state": (
                    ConstraintOverrideState.APPLIED
                    if apply
                    else ConstraintOverrideState.REJECTED
                ),
                "resolved_at": resolved_at or self._now,
            }
        )
        replaced = tuple(
            resolved if override.request_id == request_id else override
            for override in self._ledger.overrides
        )
        updated = self._ledger.model_copy(update={"overrides": replaced})
        self._ledger = updated
        return updated, resolved
