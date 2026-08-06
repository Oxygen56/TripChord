"""API contract tests for the v0.5/v0.6/v0.7 production wiring endpoints."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.main import app
from tripchord.persistence.booking_ledger import BookingLedgerStore
from tripchord.persistence.handoff_store import HandoffStore

PLAN = "plan-wiring-test"


@pytest.fixture(autouse=True)
def _isolate_stores(tmp_path):
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
