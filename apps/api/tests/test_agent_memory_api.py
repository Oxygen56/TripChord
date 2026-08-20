from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.memory import (
    MemoryAccessContext,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    MemoryVolatility,
    PrivacyBoundary,
    confirmed_preference_constitution,
)
from tripchord.agents.models import (
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
)
from tripchord.agents.persistent_memory import PersistentMemoryStore
from tripchord.auth import Principal, get_principal
from tripchord.main import app


@pytest.mark.asyncio
async def test_runtime_endpoint_reports_real_model_memory_and_rag_boundaries() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1/agents/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["codex_runtime_dependency"] is False
    assert body["chatgpt_runtime_dependency"] is False
    assert body["memory_persistence_enabled"] is True
    assert body["sensitive_memory_persisted"] is False
    assert body["live_run_cache_persistence_enabled"] is True
    assert body["live_run_cache_multi_worker_supported"] is False
    assert "single-process" in body["live_run_cache_backend"]
    assert body["rag_enabled"] is True
    assert "实时价格" in body["rag_boundary"]
    assert "来源搜索调度" in body["agent_decision_roles"]
    assert "Verifier、ReVerifier" in "".join(body["deterministic_authority"])
    assert "能改变执行" in body["autonomy_boundary"]
    assert "Chrome companion extension" in body["browser_runtime_requirements"]


@pytest.mark.asyncio
async def test_long_term_preference_requires_explicit_confirmation_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PersistentMemoryStore(tmp_path / "memory.json")
    monkeypatch.setattr(
        "tripchord.main.memory_store",
        store,
    )
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="authenticated-user-a",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            confirmed = await client.post(
                "/api/v1/agents/memory/preferences/confirm",
                json={
                    "key": "hotel_breakfast",
                    "value": {"mode": "required", "weight": 1},
                    "source_evidence_refs": ["user-explicit-confirmation"],
                },
            )
            listed = await client.get("/api/v1/agents/memory")

            record_id = confirmed.json()["record"]["id"]
            revoked = await client.delete(f"/api/v1/agents/memory/{record_id}")
            listed_after_revoke = await client.get("/api/v1/agents/memory")
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert confirmed.status_code == 200
    record = confirmed.json()["record"]
    assert record["kind"] == "user_preference"
    assert record["source"] == "user:explicit_memory_confirmation"
    assert record["rag_eligible"] is True
    assert confirmed.json()["boundary"].startswith("只有用户显式调用确认接口")
    assert listed.status_code == 200
    assert any(item["subject"] == "hotel_breakfast" for item in listed.json()["records"])
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert listed_after_revoke.json()["records"] == []
    restarted = PersistentMemoryStore(tmp_path / "memory.json")
    assert restarted.query(
        query=MemoryQuery(),
        access=MemoryAccessContext(
            tenant_id="authenticated-user-a",
            user_id="authenticated-user-a",
        ),
    ) == ()


@pytest.mark.asyncio
async def test_confirmed_preference_is_normalized_and_rejects_live_facts(
) -> None:
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="authenticated-user-normalize",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            confirmed = await client.post(
                "/api/v1/agents/memory/preferences/confirm",
                json={
                    "key": "lodging_price",
                    "value": {"mode": "weighted", "expected": "low", "weight": 0.8},
                },
            )
            rejected = await client.post(
                "/api/v1/agents/memory/preferences/confirm",
                json={
                    "key": "trip_cost",
                    "value": {"mode": "weighted", "expected": "low", "weight": 0.8,
                              "total_cents": 123456},
                },
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert confirmed.status_code == 200
    assert confirmed.json()["record"]["payload"]["value"] == {
        "mode": "weighted",
        "expected": "low",
        "weight": 0.8,
    }
    assert rejected.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("hotel_style", {"mode": "weighted", "expected": {"current_price": 12}, "weight": 0.5}),
        ("hotel_style", {"mode": "weighted", "expected": {"room_rate_cents": 1200}, "weight": 0.5}),
        (
            "hotel_style",
            {"mode": "weighted", "expected": {"metric": "total", "value": 12}, "weight": 0.5},
        ),
        (
            "lodging_price",
            {"mode": "weighted", "expected": "current quote CNY 1234.56", "weight": 0.5},
        ),
        ("hotel_budget", 123456),
    ),
)
async def test_confirmed_preference_rejects_unknown_or_live_value(
    key: str,
    value: object,
) -> None:
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="authenticated-user-live-fact",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/agents/memory/preferences/confirm",
                json={"key": key, "value": value},
            )
    finally:
        app.dependency_overrides.pop(get_principal, None)
    assert response.status_code == 422


def test_long_term_preference_merge_gives_current_trip_precedence() -> None:
    durable = PreferenceConstitution(
        rules=(
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.FORBIDDEN,
                expected=False,
                weight=1,
                source=PreferenceSource.EXPLICIT_LONG_TERM,
            ),
        )
    )
    current = PreferenceConstitution(
        rules=(
            PreferenceRule(
                key="hotel_breakfast",
                mode=PreferenceMode.REQUIRED,
                expected=True,
                weight=1,
                source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
            ),
        )
    )
    effective = durable.merged_for_trip(current=current)
    assert effective.effective("hotel_breakfast") is not None
    assert effective.effective("hotel_breakfast").source == PreferenceSource.EXPLICIT_CURRENT_TRIP
    assert effective.effective("hotel_breakfast").expected is True


def test_revoked_or_expired_preference_is_not_loaded_into_constitution() -> None:
    store = MemoryStore()
    access = MemoryAccessContext(tenant_id="tenant", user_id="user")
    now = datetime.now(UTC)
    record = MemoryRecord(
        id="memory:user-preference:test",
        kind=MemoryKind.USER_PREFERENCE,
        scope=MemoryScope.USER,
        privacy=PrivacyBoundary.USER_PRIVATE,
        tenant_id="tenant",
        user_id="user",
        topic="user_preference",
        subject="hotel_breakfast",
        payload={
            "key": "hotel_breakfast",
            "value": {"mode": "required", "expected": True, "weight": 1},
        },
        source="user:explicit_memory_confirmation",
        captured_at=now,
        allowed_roles=(),
        volatility=MemoryVolatility.STABLE,
    )
    store.upsert(record)
    assert len(confirmed_preference_constitution(store, access, now=now).rules) == 1
    assert store.delete(record.id, access) is True
    assert confirmed_preference_constitution(store, access).rules == ()
    expired = record.model_copy(
        update={
            "id": "memory:user-preference:expired",
            "captured_at": now - timedelta(seconds=10),
            "expires_at": now - timedelta(seconds=1),
        }
    )
    store.upsert(expired)
    assert confirmed_preference_constitution(store, access, now=now).rules == ()


@pytest.mark.asyncio
async def test_anonymous_development_principal_cannot_persist_user_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "memory.json"
    monkeypatch.setattr(
        "tripchord.main.memory_store",
        PersistentMemoryStore(state_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/agents/memory/preferences/confirm",
            json={"key": "hotel_breakfast", "value": "required"},
        )

    assert response.status_code == 403
    assert "anonymous" in response.json()["detail"]
    assert not state_path.exists()
