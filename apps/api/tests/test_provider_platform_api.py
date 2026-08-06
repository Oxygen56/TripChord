"""API contract tests for the v0.2 provider capability matrix."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.main import app


@pytest.mark.asyncio
async def test_provider_capabilities_matrix() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/providers/capabilities")
    assert response.status_code == 200
    body = response.json()
    assert body["profile_version"].startswith("tripchord-provider-profile")
    assert len(body["registry_sha256"]) == 64
    scopes = {item["key"] for item in body["scopes"]}
    assert "ctrip:flight" in scopes
    assert "tongcheng:lodging" in scopes
    # Tongcheng overseas lodging stays disabled (user scope decision).
    tc_lodging = next(item for item in body["scopes"] if item["key"] == "tongcheng:lodging")
    assert tc_lodging["certification_stage"] == "disabled"
    assert tc_lodging["eligible"] is False
    # Flight matrix exposes the audited three scopes.
    flight = [item for item in body["scopes"] if item["vertical"] == "flight"]
    assert {item["provider"] for item in flight} == {"ctrip", "qunar", "tongcheng"}


@pytest.mark.asyncio
async def test_provider_runtime_health() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/providers/runtime-health")
    assert response.status_code == 200
    body = response.json()
    assert "ctrip:flight" in body["authorized_scope_keys"]
    assert isinstance(body["model_endpoint_healthy"], bool)


@pytest.mark.asyncio
async def test_provider_selection_toggle_reflects_in_matrix() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        put_response = await client.put(
            "/api/v1/preferences/provider-selection",
            json={"scope": "ctrip:flight", "enabled": False},
        )
    assert put_response.status_code == 200
    body = put_response.json()
    assert body["snapshot_sha256"] and len(body["snapshot_sha256"]) == 64
    ctrip_flight = next(
        item for item in body["updated"] if item["key"] == "ctrip:flight"
    )
    assert ctrip_flight["user_enabled"] is False
    assert ctrip_flight["eligible"] is False
