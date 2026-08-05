from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tripchord.agents.memory import MemoryAccessContext, MemoryQuery
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
                    "key": "interview-test-breakfast",
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
    assert any(item["subject"] == "interview-test-breakfast" for item in listed.json()["records"])
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
            json={"key": "breakfast", "value": "required"},
        )

    assert response.status_code == 403
    assert "anonymous" in response.json()["detail"]
    assert not state_path.exists()
