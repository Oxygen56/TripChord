"""v0.5 wiring tests: component re-price service + official-handoff issuance."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.handoff import (
    LocatorKind,
    OfficialDetailLocator,
    RevalidationOutcome,
)
from tripchord.platform.reprice import (
    ComponentRepriceOutcome,
    ComponentRepriceRequest,
    ComponentRepriceService,
    DefaultRepriceURLBuilder,
    FreshComponentQuote,
    UnstableHandoffPath,
    build_official_url,
    build_reprice_query_url,
    compute_query_fingerprint_sha256,
)
from tripchord.providers.browser_bridge import BrowserSearchQuery

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)

FLIGHT_SCOPE = ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT)


def _locator() -> OfficialDetailLocator:
    return OfficialDetailLocator(
        scope=FLIGHT_SCOPE,
        kind=LocatorKind.DETAIL_PAGE,
        official_hosts=("flights.ctrip.com",),
        allowed_path_prefixes=("/international/search/",),
    )


def _request(**overrides: object) -> ComponentRepriceRequest:
    values = {
        "plan_version": "plan-v1",
        "component_id": "comp-1",
        "scope": FLIGHT_SCOPE,
        "query_fingerprint_sha256": "f" * 64,
        "current_total_for_party_cents": 120000,
        "reprice_url": "/api/v1/reprice?plan_version=plan-v1&component_id=comp-1",
    }
    values.update(overrides)
    return ComponentRepriceRequest(**values)


class _FixedSource:
    def __init__(self, quote: FreshComponentQuote | None) -> None:
        self._quote = quote
        self.requests: list[ComponentRepriceRequest] = []

    async def fetch_fresh_quote(
        self,
        request: ComponentRepriceRequest,
    ) -> FreshComponentQuote | None:
        self.requests.append(request)
        return self._quote


def _query(**overrides: object) -> BrowserSearchQuery:
    values = {
        "origin": "PVG",
        "destination": "MLE",
        "start_date": date(2026, 9, 1),
        "end_date": date(2026, 9, 8),
        "adults": 2,
        "rooms": 1,
    }
    values.update(overrides)
    return BrowserSearchQuery(**values)


def test_query_fingerprint_is_deterministic_sha256() -> None:
    fingerprint = compute_query_fingerprint_sha256(_query())
    assert len(fingerprint) == 64
    assert fingerprint == compute_query_fingerprint_sha256(_query())


def test_query_fingerprint_binds_every_query_parameter() -> None:
    fingerprint = compute_query_fingerprint_sha256(_query())
    for changed in (
        {"start_date": date(2026, 9, 2)},
        {"end_date": date(2026, 9, 9)},
        {"adults": 1},
        {"rooms": 2},
        {"destination": "CMB"},
        {"origin": None},
        {"currency": "USD"},
    ):
        assert compute_query_fingerprint_sha256(_query(**changed)) != fingerprint, changed


@pytest.mark.asyncio
async def test_reprice_unchanged_builds_two_step_checklist() -> None:
    source = _FixedSource(
        FreshComponentQuote(
            quote_id="quote-fresh-1",
            component_id="comp-1",
            scope=FLIGHT_SCOPE,
            total_for_party_cents=120000,
            fetched_at=NOW,
        )
    )
    service = ComponentRepriceService(quote_source=source, now=NOW)
    result = await service.reprice_component(_request(), _locator())
    assert result.outcome is ComponentRepriceOutcome.UNCHANGED
    assert result.revalidation_receipt is not None
    assert result.revalidation_receipt.outcome is RevalidationOutcome.UNCHANGED
    assert result.revalidation_receipt.total_for_party_cents == 120000
    checklist = result.checklist
    assert checklist is not None
    assert checklist.official_handoff is not None
    assert checklist.official_handoff.is_usable(NOW)
    # Suggested next step is "go to official" only after an unchanged, fresh reprice.
    assert checklist.suggested_next_step == "go_to_official"


@pytest.mark.asyncio
async def test_reprice_changed_never_issues_handoff() -> None:
    source = _FixedSource(
        FreshComponentQuote(
            quote_id="quote-fresh-2",
            component_id="comp-1",
            scope=FLIGHT_SCOPE,
            total_for_party_cents=150000,
            fetched_at=NOW,
        )
    )
    service = ComponentRepriceService(quote_source=source, now=NOW)
    result = await service.reprice_component(_request(), _locator())
    assert result.outcome is ComponentRepriceOutcome.CHANGED
    assert result.revalidation_receipt is not None
    assert result.revalidation_receipt.outcome is RevalidationOutcome.CHANGED
    assert result.revalidation_receipt.total_for_party_cents is None
    assert result.checklist is None


@pytest.mark.asyncio
async def test_reprice_not_found_never_issues_handoff() -> None:
    source = _FixedSource(
        FreshComponentQuote(
            quote_id="quote-fresh-3",
            component_id="comp-1",
            scope=FLIGHT_SCOPE,
            total_for_party_cents=None,
            fetched_at=NOW,
        )
    )
    service = ComponentRepriceService(quote_source=source, now=NOW)
    result = await service.reprice_component(_request(), _locator())
    assert result.outcome is ComponentRepriceOutcome.NOT_FOUND
    assert result.revalidation_receipt is not None
    assert result.revalidation_receipt.outcome is RevalidationOutcome.NOT_FOUND
    assert result.checklist is None


@pytest.mark.asyncio
async def test_reprice_scope_mismatch_blocked() -> None:
    source = _FixedSource(
        FreshComponentQuote(
            quote_id="quote-fresh-4",
            component_id="comp-1",
            scope=ProviderScopeKey(
                provider="qunar", vertical=ProviderVertical.FLIGHT
            ),
            total_for_party_cents=120000,
            fetched_at=NOW,
        )
    )
    service = ComponentRepriceService(quote_source=source, now=NOW)
    result = await service.reprice_component(_request(), _locator())
    assert result.outcome is ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE
    assert result.blocked_reason is not None


@pytest.mark.asyncio
async def test_reprice_no_current_quote_skips() -> None:
    service = ComponentRepriceService(
        quote_source=_FixedSource(None), now=NOW, url_builder=DefaultRepriceURLBuilder()
    )
    result = await service.reprice_component(
        _request(current_total_for_party_cents=None),
        _locator(),
    )
    assert result.outcome is ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE
    assert result.checklist is None


@pytest.mark.asyncio
async def test_reprice_no_quote_source_reports_live_unavailable() -> None:
    service = ComponentRepriceService(quote_source=None, now=NOW)
    result = await service.reprice_component(_request(), _locator())
    assert result.outcome is ComponentRepriceOutcome.LIVE_UNAVAILABLE
    assert result.blocked_reason is not None
    assert result.checklist is None


def test_default_url_builder_rejects_param_card_only() -> None:
    param_card_locator = OfficialDetailLocator(
        scope=FLIGHT_SCOPE,
        kind=LocatorKind.PARAM_CARD_ONLY,
        official_hosts=("flights.ctrip.com",),
        allowed_path_prefixes=(),
    )
    builder = DefaultRepriceURLBuilder()
    with pytest.raises(UnstableHandoffPath):
        builder.build(
            scope=FLIGHT_SCOPE,
            locator=param_card_locator,
            query_fingerprint_sha256="f" * 64,
            plan_version="plan-v1",
            component_id="comp-1",
        )


def test_build_official_url_roundtrip() -> None:
    url = build_official_url(
        locator=_locator(),
        path="/international/search/",
        query_params={"tripchord_component": "comp-1"},
    )
    assert url.startswith("https://flights.ctrip.com/international/search/?")
    assert "tripchord_component=comp-1" in url


def test_build_reprice_query_url() -> None:
    url = build_reprice_query_url(
        plan_version="plan-v1",
        component_id="comp-1",
        scope=FLIGHT_SCOPE,
        query_fingerprint_sha256="f" * 64,
        base_url="/api/v1/reprice",
    )
    assert url.startswith("/api/v1/reprice?")
    assert "plan_version=plan-v1" in url
    assert "component_id=comp-1" in url


@pytest.mark.asyncio
async def test_handoff_cannot_outlive_receipt() -> None:
    source = _FixedSource(
        FreshComponentQuote(
            quote_id="quote-fresh-5",
            component_id="comp-1",
            scope=FLIGHT_SCOPE,
            total_for_party_cents=120000,
            fetched_at=NOW,
        )
    )
    service = ComponentRepriceService(
        quote_source=source, now=NOW, receipt_ttl_seconds=300
    )
    result = await service.reprice_component(_request(), _locator())
    assert result.checklist is not None
    handoff = result.checklist.official_handoff
    receipt = result.revalidation_receipt
    assert handoff is not None and receipt is not None
    assert handoff.expires_at <= receipt.expires_at
    # After the receipt window the checklist must refuse the official hop.
    later = NOW + timedelta(minutes=6)
    assert not result.checklist.can_go_to_official(later)
