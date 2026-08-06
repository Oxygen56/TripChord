"""v0.5 OfficialHandoff / URL policy / revalidation contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.handoff import (
    HandoffURLPolicy,
    LocatorKind,
    OfficialDetailLocator,
    OfficialHandoffState,
    RevalidationOutcome,
    RevalidationReceipt,
    build_component_checklist,
    issue_official_handoff,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _locator() -> OfficialDetailLocator:
    return OfficialDetailLocator(
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        kind=LocatorKind.DETAIL_PAGE,
        official_hosts=("flights.ctrip.com",),
        allowed_path_prefixes=("/international/search/",),
    )


def _receipt(**overrides: object) -> RevalidationReceipt:
    values = {
        "receipt_id": "receipt-1",
        "plan_version": "plan-v1",
        "component_id": "comp-1",
        "scope": ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        "quote_id": "quote-1",
        "revalidated_at": NOW - timedelta(seconds=30),
        "expires_at": NOW + timedelta(minutes=4),
        "outcome": RevalidationOutcome.UNCHANGED,
        "total_for_party_cents": 120000,
    }
    values.update(overrides)
    return RevalidationReceipt(**values)


def test_url_policy_allows_official_detail_page() -> None:
    policy = HandoffURLPolicy(locator=_locator())
    allowed, reason = policy.validate_url(
        "https://flights.ctrip.com/international/search/round-HGH-MLE?dep=2026-08-21"
    )
    assert allowed, reason


@pytest.mark.parametrize(
    "url",
    [
        "http://flights.ctrip.com/international/search/round-HGH-MLE",
        "https://evil.example.com/international/search/round-HGH-MLE",
        "https://flights.ctrip.com/",
        "https://flights.ctrip.com/login?next=/international",
        "https://flights.ctrip.com/account/orders",
        "https://flights.ctrip.com/checkout/payment",
        "https://flights.ctrip.com/coupon/redeem",
        "https://flights.ctrip.com/international/search/round-HGH-MLE?coupon=SAVE50",
        "https://user:pass@flights.ctrip.com/international/search/round-HGH-MLE",
        "https://bit.ly/abc123",
    ],
)
def test_url_policy_rejects_dangerous_mutations(url: str) -> None:
    policy = HandoffURLPolicy(locator=_locator())
    allowed, reason = policy.validate_url(url)
    assert allowed is False, f"{url} should be rejected but got: {reason}"


def test_revalidation_receipt_freshness_and_unchanged_requires_price() -> None:
    receipt = _receipt()
    assert receipt.is_fresh(NOW)
    assert not receipt.is_fresh(NOW + timedelta(minutes=5))
    with pytest.raises(ValueError):
        _receipt(
            outcome=RevalidationOutcome.UNCHANGED,
            total_for_party_cents=None,
        )


def test_handoff_issued_only_after_unchanged_reprice() -> None:
    receipt = _receipt()
    handoff = issue_official_handoff(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        locator=_locator(),
        url="https://flights.ctrip.com/international/search/round-HGH-MLE?dep=2026-08-21",
        query_fingerprint_sha256="a" * 64,
        revalidation_receipt=receipt,
        created_at=NOW,
    )
    assert handoff.is_usable(NOW)
    assert handoff.handoff_sha256() == handoff.handoff_sha256()
    assert handoff.revalidation_receipt_sha256 == receipt.receipt_sha256()


def test_handoff_refuses_receipt_component_or_scope_mismatch() -> None:
    kwargs = {
        "plan_version": "plan-v1",
        "component_id": "comp-1",
        "scope": ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        "locator": _locator(),
        "url": "https://flights.ctrip.com/international/search/round-HGH-MLE",
        "query_fingerprint_sha256": "a" * 64,
        "created_at": NOW,
    }
    with pytest.raises(ValueError, match="same component"):
        issue_official_handoff(
            revalidation_receipt=_receipt(component_id="comp-2"),
            **kwargs,
        )
    with pytest.raises(ValueError, match="same scope"):
        issue_official_handoff(
            revalidation_receipt=_receipt(
                scope=ProviderScopeKey(provider="qunar", vertical=ProviderVertical.FLIGHT),
            ),
            **kwargs,
        )
    with pytest.raises(ValueError, match="unchanged"):
        issue_official_handoff(
            revalidation_receipt=_receipt(outcome=RevalidationOutcome.CHANGED),
            **kwargs,
        )


def test_handoff_expires_with_receipt_and_state_gating() -> None:
    receipt = _receipt()
    handoff = issue_official_handoff(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        locator=_locator(),
        url="https://flights.ctrip.com/international/search/round-HGH-MLE",
        query_fingerprint_sha256="a" * 64,
        revalidation_receipt=receipt,
        created_at=NOW,
    )
    used = handoff.model_copy(update={"state": OfficialHandoffState.USED})
    assert not used.is_usable(NOW)
    expired = handoff.model_copy(
        update={"expires_at": NOW - timedelta(seconds=1)}
    )
    assert not expired.is_usable(NOW)


def test_component_checklist_two_step_flow() -> None:
    receipt = _receipt()
    checklist = build_component_checklist(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        locator=_locator(),
        official_url="https://flights.ctrip.com/international/search/round-HGH-MLE",
        query_fingerprint_sha256="a" * 64,
        reprice_url="https://flights.ctrip.com/international/search/round-HGH-MLE?dep=2026-08-21",
        revalidation_receipt=receipt,
        now=NOW,
    )
    assert checklist.suggested_next_step == "go_to_official"
    assert checklist.can_go_to_official(NOW)
    assert checklist.official_handoff is not None
    assert checklist.reprice_url is not None


def test_component_checklist_requires_fresh_unchanged_receipt() -> None:
    stale_receipt = _receipt(expires_at=NOW - timedelta(seconds=1))
    checklist = build_component_checklist(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        locator=_locator(),
        official_url="https://flights.ctrip.com/international/search/round-HGH-MLE",
        query_fingerprint_sha256="a" * 64,
        reprice_url="https://flights.ctrip.com/international/search/round-HGH-MLE?dep=2026-08-21",
        revalidation_receipt=stale_receipt,
        now=NOW,
    )
    assert checklist.official_handoff is None
    assert checklist.suggested_next_step == "reprice"
    assert not checklist.can_go_to_official(NOW)

    changed_receipt = _receipt(outcome=RevalidationOutcome.CHANGED, total_for_party_cents=130000)
    checklist_changed = build_component_checklist(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        locator=_locator(),
        official_url="https://flights.ctrip.com/international/search/round-HGH-MLE",
        query_fingerprint_sha256="a" * 64,
        reprice_url="https://flights.ctrip.com/international/search/round-HGH-MLE?dep=2026-08-21",
        revalidation_receipt=changed_receipt,
        now=NOW,
    )
    assert checklist_changed.suggested_next_step == "reprice"
    assert not checklist_changed.can_go_to_official(NOW)
