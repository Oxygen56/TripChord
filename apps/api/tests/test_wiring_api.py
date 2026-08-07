"""API contract tests for the v0.5/v0.6/v0.7 production wiring endpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.live_system import LivePackageAgentRun
from tripchord.main import LiveRunCache, app
from tripchord.persistence.booking_ledger import BookingLedgerStore
from tripchord.persistence.handoff_store import HandoffStore
from tripchord.platform.reprice import (
    ComponentRepriceRequest,
    FreshComponentQuote,
    compute_query_fingerprint_sha256,
)
from tripchord.providers.browser_bridge import BrowserSearchQuery

PLAN = "plan-wiring-test"
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path: Path) -> Iterator[None]:
    """Point the app's local stores at a temp dir so tests never share state."""
    previous_booking = app.state.booking_ledger_store
    previous_handoff = app.state.handoff_store
    app.state.booking_ledger_store = BookingLedgerStore(root=tmp_path / "booking-ledgers")
    app.state.handoff_store = HandoffStore(path=tmp_path / "handoffs.json")
    yield
    app.state.booking_ledger_store = previous_booking
    app.state.handoff_store = previous_handoff


@pytest.mark.asyncio
async def test_booking_acknowledge_creates_protected_component() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/v1/plans/{PLAN}/components/comp-1/booking/acknowledge",
            json={
                "checklist_id": "checklist-comp-1",
                "acknowledgement_id": "ack-comp-1",
                "user_token_sha256": "a" * 64,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["protected"] is True
        assert body["fact"]["component_id"] == "comp-1"

        ledger_response = await client.get(f"/api/v1/plans/{PLAN}/booking")
        assert ledger_response.status_code == 200
        ledger = ledger_response.json()
        assert "comp-1" in ledger["protected_component_ids"]


@pytest.mark.asyncio
async def test_booking_acknowledge_is_append_only() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            f"/api/v1/plans/{PLAN}/components/comp-2/booking/acknowledge",
            json={
                "checklist_id": "checklist-comp-2",
                "acknowledgement_id": "ack-comp-2",
                "user_token_sha256": "a" * 64,
            },
        )
        assert first.status_code == 200
        # A second fact for the same component must be rejected (append-only).
        second = await client.post(
            f"/api/v1/plans/{PLAN}/components/comp-2/booking/acknowledge",
            json={
                "checklist_id": "checklist-comp-2",
                "acknowledgement_id": "ack-comp-2b",
                "user_token_sha256": "a" * 64,
            },
        )
        assert second.status_code == 409


@pytest.mark.asyncio
async def test_booking_override_request_is_audited_and_not_applied() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # A component must be protected before an override can be requested.
        await client.post(
            f"/api/v1/plans/{PLAN}/components/comp-1/booking/acknowledge",
            json={
                "checklist_id": "checklist-comp-1",
                "acknowledgement_id": "ack-comp-1",
                "user_token_sha256": "a" * 64,
            },
        )
        override = await client.post(
            f"/api/v1/plans/{PLAN}/components/comp-1/booking/override",
            json={
                "reason": "user wants to switch to a different hotel",
                "requested_by_token_sha256": "b" * 64,
            },
        )
        assert override.status_code == 200
        body = override.json()
        assert body["request_id"]
        assert body["state"] == "requested"
        request_id = body["request_id"]

        resolve = await client.post(
            f"/api/v1/plans/{PLAN}/booking/overrides/{request_id}/resolve",
            json={"apply": True},
        )
        assert resolve.status_code == 200
        assert resolve.json()["state"] == "applied"


@pytest.mark.asyncio
async def test_booking_override_requires_protected_component() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/v1/plans/{PLAN}/components/never-protected/booking/override",
            json={
                "reason": "this component is not protected",
                "requested_by_token_sha256": "b" * 64,
            },
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_provider_cooldown_endpoint() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/providers/qunar:flight/cooldown",
            json={"reason": "repeated drift on qunar flight selector"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["scope"] == "qunar:flight"
        assert body["from_stage"] == "certified_active"
        assert body["to_stage"] == "cooldown"


@pytest.mark.asyncio
async def test_provider_cooldown_unknown_scope_404() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/providers/nonexistent:flight/cooldown",
            json={"reason": "test"},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_provider_sdk_conformance_reports_shadow_and_certified() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/providers/sdk/conformance")
    assert response.status_code == 200
    views = response.json()
    by_scope = {view["scope"]: view for view in views}
    assert "ctrip:flight" in by_scope
    assert by_scope["ctrip:flight"]["certification_stage"] == "certified_active"
    assert "tongcheng:lodging" in by_scope
    assert by_scope["tongcheng:lodging"]["certification_stage"] == "disabled"


def _fixture_query() -> BrowserSearchQuery:
    return BrowserSearchQuery(
        origin="PVG",
        destination="MLE",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 8),
        adults=2,
        rooms=1,
    )


def _fixture_run(
    *,
    flight_provider: str = "ctrip",
    lodging_provider: str = "ctrip",
) -> LivePackageAgentRun:
    return cast(
        LivePackageAgentRun,
        SimpleNamespace(
            package=SimpleNamespace(
                final_candidate=SimpleNamespace(
                    flight=SimpleNamespace(
                        id="flight-1",
                        provider=flight_provider,
                        total_for_party_cents=120000,
                    ),
                    lodgings=(
                        SimpleNamespace(
                            id="lodging-1",
                            provider=lodging_provider,
                            total_for_party_cents=80000,
                        ),
                    ),
                    transfers=(),
                )
            ),
            search_query=_fixture_query(),
        ),
    )


class _FixtureQuoteSource:
    """Returns an unchanged fresh quote for the exact requested scope."""

    def __init__(self, totals: dict[str, int], now: datetime) -> None:
        self._totals = totals
        self._now = now

    async def fetch_fresh_quote(
        self,
        request: ComponentRepriceRequest,
    ) -> FreshComponentQuote:
        return FreshComponentQuote(
            quote_id=f"quote-{request.component_id}",
            component_id=request.component_id,
            scope=request.scope,
            total_for_party_cents=self._totals.get(request.component_id),
            fetched_at=self._now,
        )


def _fixture_quote_source_factory() -> Callable[..., _FixtureQuoteSource]:
    totals = {"flight-1": 120000, "lodging-1": 80000}

    def factory(
        *,
        run: object,
        component_id: str,
        provider: str,
        timeout_seconds: int,
    ) -> _FixtureQuoteSource:
        return _FixtureQuoteSource(totals, NOW)

    return factory


@pytest.mark.asyncio
async def test_reprice_derives_vertical_scope_and_issues_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lodging component re-prices under ``ctrip:lodging``, not ``ctrip:flight``,
    and a fresh unchanged reprice issues a query-bound handoff (fixture success path)."""
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5))
    run_id, _ = await cache.put("anonymous", _fixture_run())
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(
        app.state,
        "reprice_quote_source_factory",
        _fixture_quote_source_factory(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        # Flight component re-prices under ctrip:flight.
        flight_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/components/flight-1/reprice",
            json={},
        )
        assert flight_response.status_code == 200
        flight_body = flight_response.json()
        assert flight_body["scope_key"] == "ctrip:flight"
        assert flight_body["live_mode"] == "fixture"
        assert flight_body["outcome"] == "unchanged"

        # Lodging component re-prices under ctrip:lodging — never ctrip:flight.
        lodging_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/components/lodging-1/reprice",
            json={},
        )
        assert lodging_response.status_code == 200
        lodging_body = lodging_response.json()
        assert lodging_body["scope_key"] == "ctrip:lodging"
        assert lodging_body["live_mode"] == "fixture"
        assert lodging_body["outcome"] == "unchanged"

        checklist = lodging_body["checklist"]
        assert checklist is not None
        handoff = checklist["official_handoff"]
        assert handoff is not None
        # The handoff is bound to the real query, not the all-zero placeholder.
        expected_fingerprint = compute_query_fingerprint_sha256(_fixture_query())
        assert handoff["query_fingerprint_sha256"] == expected_fingerprint
        assert handoff["query_fingerprint_sha256"] != "0" * 64

        # Consume the lodging handoff -> single use, no booked state.
        consume_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/components/lodging-1/handoff/consume",
            json={"handoff_id": handoff["handoff_id"]},
        )
        assert consume_response.status_code == 200
        consume_body = consume_response.json()
        assert consume_body["consumed"] is True
        assert consume_body["state"] == "used"
        assert consume_body["booked"] is False


@pytest.mark.asyncio
async def test_consume_rejects_when_query_binding_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the run's query changes, the old handoff is invalidated and the
    consume endpoint refuses it until the component is re-priced."""
    cache = LiveRunCache(capacity=4, ttl=timedelta(minutes=5))
    run_id, _ = await cache.put("anonymous", _fixture_run())
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(
        app.state,
        "reprice_quote_source_factory",
        _fixture_quote_source_factory(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        reprice_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/components/lodging-1/reprice",
            json={},
        )
        assert reprice_response.status_code == 200
        handoff = reprice_response.json()["checklist"]["official_handoff"]

        # The trip query changes after the re-price: replace the cached run
        # with a copy whose query differs.
        entry = await cache.get(run_id, "anonymous")
        assert entry is not None
        changed_run = cast(
            LivePackageAgentRun,
            SimpleNamespace(
                package=entry.run.package,
                search_query=entry.run.search_query.model_copy(
                    update={"start_date": date(2026, 9, 2)}
                ),
            ),
        )
        assert await cache.replace(run_id, "anonymous", entry, changed_run) is not None

        consume_response = await client.post(
            f"/api/v1/agents/live-plans/{run_id}/components/lodging-1/handoff/consume",
            json={"handoff_id": handoff["handoff_id"]},
        )
        assert consume_response.status_code == 422


@pytest.mark.asyncio
async def test_reprice_missing_run_returns_404() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-plans/does-not-exist/components/comp-1/reprice",
            json={},
        )
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_consume_handoff_missing_returns_404() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/live-plans/run-x/components/comp-1/handoff/consume",
            json={"handoff_id": "no-such-handoff"},
        )
        assert response.status_code == 404
