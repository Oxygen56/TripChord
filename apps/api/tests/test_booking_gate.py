"""v0.6 wiring tests: booking protection consumption gate + booking service."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tripchord.platform.booking import BookingLedger
from tripchord.platform.booking_gate import (
    BookingProtectionGate,
    BookingService,
    ComponentChangeSet,
    ProtectedComponentModificationError,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _ledger_with_fact(component_id: str = "comp-1") -> BookingLedger:
    service = BookingService(BookingLedger(plan_version="plan-v1"), now=NOW)
    ledger, _fact = service.acknowledge_component(
        plan_version="plan-v1",
        component_id=component_id,
        checklist_id=f"checklist-{component_id}",
        acknowledgement_id=f"ack-{component_id}",
        user_token_sha256="a" * 64,
    )
    return ledger


def test_protected_removal_blocked() -> None:
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            removed_component_ids=("comp-1",),
            preserved_component_ids=("comp-2",),
        )
    )
    assert impact.affected_protected_component_ids == ("comp-1",)
    assert impact.blocked_reason is not None
    assert not impact.protected_component_silently_modified


def test_protected_change_blocked() -> None:
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            changed_component_ids=("comp-1",),
        )
    )
    assert impact.affected_protected_component_ids == ("comp-1",)
    assert impact.blocked_reason is not None


def test_protected_component_preserved_is_unaffected() -> None:
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            preserved_component_ids=("comp-1", "comp-2"),
            added_component_ids=("comp-3",),
        )
    )
    assert impact.affected_protected_component_ids == ()
    assert impact.unaffected_protected_component_ids == ("comp-1",)
    assert impact.blocked_reason is None


def test_unprotected_component_change_allowed() -> None:
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            removed_component_ids=("comp-2",),
            added_component_ids=("comp-9",),
        )
    )
    assert impact.affected_protected_component_ids == ()
    assert impact.blocked_reason is None


def test_applied_override_allows_change() -> None:
    ledger = _ledger_with_fact("comp-1")
    service = BookingService(ledger, now=NOW)
    ledger, request = service.request_override(
        plan_version="plan-v1",
        component_id="comp-1",
        requested_by_token_sha256="b" * 64,
        reason="price changed significantly, user wants to switch",
    )
    ledger, _resolved = service.resolve_override(request.request_id, apply=True)
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            changed_component_ids=("comp-1",),
        )
    )
    assert impact.affected_protected_component_ids == ()


def test_open_override_still_blocks() -> None:
    ledger = _ledger_with_fact("comp-1")
    service = BookingService(ledger, now=NOW)
    ledger, _request = service.request_override(
        plan_version="plan-v1",
        component_id="comp-1",
        requested_by_token_sha256="b" * 64,
        reason="user is thinking about switching",
    )
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            removed_component_ids=("comp-1",),
        )
    )
    assert impact.affected_protected_component_ids == ("comp-1",)


def test_assert_no_silent_modification_raises() -> None:
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    with pytest.raises(ProtectedComponentModificationError) as exc_info:
        gate.assert_no_silent_modification(
            ComponentChangeSet(
                plan_version="plan-v1",
                changed_component_ids=("comp-1",),
            )
        )
    assert exc_info.value.impact.affected_protected_component_ids == ("comp-1",)


def test_booking_fact_append_only() -> None:
    service = BookingService(BookingLedger(plan_version="plan-v1"), now=NOW)
    ledger, fact = service.acknowledge_component(
        plan_version="plan-v1",
        component_id="comp-1",
        checklist_id="checklist-1",
        acknowledgement_id="ack-1",
        user_token_sha256="a" * 64,
    )
    assert len(ledger.facts) == 1
    assert ledger.is_protected("comp-1")
    assert fact.fact_sha256() == ledger.facts[0].fact_sha256()
    # A second fact for the same component must be rejected (append-only invariant).
    with pytest.raises(ValueError):
        service.acknowledge_component(
            plan_version="plan-v1",
            component_id="comp-1",
            checklist_id="checklist-1",
            acknowledgement_id="ack-2",
            user_token_sha256="a" * 64,
        )


def test_override_requires_protected_component() -> None:
    service = BookingService(BookingLedger(plan_version="plan-v1"), now=NOW)
    with pytest.raises(ValueError):
        service.request_override(
            plan_version="plan-v1",
            component_id="comp-zz",
            requested_by_token_sha256="b" * 64,
            reason="not protected",
        )


def test_override_rejected_keeps_blocked() -> None:
    ledger = _ledger_with_fact("comp-1")
    service = BookingService(ledger, now=NOW)
    ledger, request = service.request_override(
        plan_version="plan-v1",
        component_id="comp-1",
        requested_by_token_sha256="b" * 64,
        reason="not needed anymore",
    )
    ledger, resolved = service.resolve_override(request.request_id, apply=False)
    assert resolved.state.value == "rejected"
    gate = BookingProtectionGate(ledger, now=NOW)
    impact = gate.evaluate_diff(
        ComponentChangeSet(
            plan_version="plan-v1",
            changed_component_ids=("comp-1",),
        )
    )
    assert impact.affected_protected_component_ids == ("comp-1",)


def test_any_event_sequence_never_silently_modifies_booked() -> None:
    """Five anti-surface acceptance: booked-component modification rate is 0."""
    ledger = _ledger_with_fact("comp-1")
    gate = BookingProtectionGate(ledger, now=NOW)
    sequences = [
        ("removed", ("comp-1",)),
        ("changed", ("comp-1",)),
        ("removed", ("comp-1", "comp-2")),
        ("changed", ("comp-2",), ("comp-1",)),
    ]
    for seq in sequences:
        change_set = ComponentChangeSet(
            plan_version="plan-v1",
            removed_component_ids=seq[1] if seq[0] == "removed" else (),
            changed_component_ids=seq[1] if seq[0] == "changed" else (),
            preserved_component_ids=seq[2] if len(seq) > 2 else (),
        )
        impact = gate.evaluate_diff(change_set)
        # A protected component may only be modified if explicitly overridden,
        # and never silently: the gate always reports the block.
        assert not impact.protected_component_silently_modified
        if "comp-1" in (change_set.removed_component_ids + change_set.changed_component_ids):
            assert "comp-1" in impact.affected_protected_component_ids
