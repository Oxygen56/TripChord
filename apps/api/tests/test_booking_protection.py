"""v0.6 booking protection / protected constraint contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tripchord.platform.booking import (
    BookingChecklist,
    BookingFact,
    BookingFactSource,
    BookingItem,
    BookingItemState,
    BookingLedger,
    ConstraintOverrideRequest,
    ConstraintOverrideState,
    UserBookingAcknowledgement,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _ack(component_id: str = "comp-1") -> UserBookingAcknowledgement:
    return UserBookingAcknowledgement(
        acknowledgement_id=f"ack-{component_id}",
        plan_version="plan-v1",
        component_id=component_id,
        checklist_id=f"checklist-{component_id}",
        acknowledged_at=NOW,
        user_token_sha256="a" * 64,
    )


def _checklist(component_id: str = "comp-1") -> BookingChecklist:
    return BookingChecklist(
        checklist_id=f"checklist-{component_id}",
        plan_version="plan-v1",
        component_id=component_id,
        items=(
            BookingItem(
                item_id=f"item-1-{component_id}",
                component_id=component_id,
                checklist_id=f"checklist-{component_id}",
                state=BookingItemState.ACKNOWLEDGED,
                acknowledged_at=NOW,
            ),
        ),
    )


def test_checklist_complete_only_when_all_items_acknowledged() -> None:
    pending = BookingChecklist(
        checklist_id="checklist-1",
        plan_version="plan-v1",
        component_id="comp-1",
        items=(
            BookingItem(
                item_id="item-1",
                component_id="comp-1",
                checklist_id="checklist-1",
                state=BookingItemState.PENDING,
            ),
        ),
    )
    assert not pending.is_complete()
    assert _checklist().is_complete()


def test_booking_fact_requires_explicit_user_acknowledgement() -> None:
    ack = _ack()
    fact = BookingFact(
        fact_id="fact-1",
        plan_version="plan-v1",
        component_id="comp-1",
        source=BookingFactSource.USER_ACKNOWLEDGEMENT,
        acknowledgement=ack,
        created_at=NOW,
    )
    assert fact.fact_sha256() == fact.fact_sha256()
    assert len(fact.fact_sha256()) == 64


def test_booking_ledger_adds_fact_and_protects_component() -> None:
    ledger = BookingLedger(plan_version="plan-v1")
    ack = _ack()
    fact = BookingFact(
        fact_id="fact-1",
        plan_version="plan-v1",
        component_id="comp-1",
        source=BookingFactSource.USER_ACKNOWLEDGEMENT,
        acknowledgement=ack,
        created_at=NOW,
    )
    updated = ledger.add_fact(fact, ack)
    assert updated.is_protected("comp-1")
    assert updated.constraint_for("comp-1") is not None
    assert updated.constraint_for("comp-2") is None
    # A second fact for the same component is rejected.
    with pytest.raises(ValueError, match="already protected"):
        updated.add_fact(fact, ack)


def test_booking_ledger_rejects_mismatched_acknowledgement() -> None:
    ledger = BookingLedger(plan_version="plan-v1")
    ack = _ack()
    other_ack = _ack("comp-2")
    fact = BookingFact(
        fact_id="fact-1",
        plan_version="plan-v1",
        component_id="comp-1",
        source=BookingFactSource.USER_ACKNOWLEDGEMENT,
        acknowledgement=other_ack,
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="exact acknowledgement"):
        ledger.add_fact(fact, ack)


def test_override_request_requires_resolution_when_applied() -> None:
    with pytest.raises(ValueError):
        ConstraintOverrideRequest(
            request_id="req-1",
            constraint_id="constraint-comp-1",
            component_id="comp-1",
            plan_version="plan-v1",
            requested_by_token_sha256="b" * 64,
            reason="user wants to reprice",
            state=ConstraintOverrideState.APPLIED,
            created_at=NOW,
            resolved_at=None,
        )
    request = ConstraintOverrideRequest(
        request_id="req-1",
        constraint_id="constraint-comp-1",
        component_id="comp-1",
        plan_version="plan-v1",
        requested_by_token_sha256="b" * 64,
        reason="user wants to reprice",
        state=ConstraintOverrideState.REQUESTED,
        created_at=NOW,
        resolved_at=None,
    )
    assert request.state is ConstraintOverrideState.REQUESTED
    assert request.resolved_at is None
