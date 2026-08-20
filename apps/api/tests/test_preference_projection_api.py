from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from test_live_flexible_from_text_api import _payload, _RecordingPairRunner
from tripchord.agents.flexible_live_system import FlexibleLiveAgentSystem
from tripchord.agents.memory import MemoryStore
from tripchord.auth import Principal, get_principal
from tripchord.main import LiveRunCache, app, package_requirement_agent, settings


@pytest.mark.asyncio
async def test_confirmed_preferences_reach_live_runner_and_revoke_stops_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    runner = _RecordingPairRunner()
    flexible = FlexibleLiveAgentSystem(
        runner,
        now=lambda: datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
        monotonic_clock=lambda: 100.0,
    )
    cache = LiveRunCache(capacity=16, ttl=timedelta(minutes=5))
    monkeypatch.setattr("tripchord.main.memory_store", store)
    monkeypatch.setattr(app.state, "package_requirement_agent", package_requirement_agent)
    monkeypatch.setattr(app.state, "flexible_live_agent_system", flexible)
    monkeypatch.setattr(app.state, "live_run_cache", cache)
    monkeypatch.setattr(settings, "browser_bridge_require_all_providers", True)
    monkeypatch.setattr(settings, "browser_bridge_task_timeout_seconds", 60)
    monkeypatch.setattr(settings, "browser_bridge_flexible_timeout_seconds", 120)
    app.dependency_overrides[get_principal] = lambda: Principal(
        tenant_id="preference-api-user",
        auth_mode="static-token",
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 51349)),
            base_url="http://test",
        ) as client:
            record_ids = []
            for key, value in (
                ("hotel_breakfast", {"mode": "required", "weight": 1}),
                ("checked_baggage", True),
                ("flight_connections", True),
            ):
                confirmed = await client.post(
                    "/api/v1/agents/memory/preferences/confirm",
                    json={"key": key, "value": value, "source_evidence_refs": ["test"]},
                )
                assert confirmed.status_code == 200, confirmed.text
                record_ids.append(confirmed.json()["record"]["id"])

            first = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(
                    text="出发地：杭州，目的地：马累，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房"
                ),
            )
            assert first.status_code == 200, first.text
            first_intent = runner.calls[-1][0]
            assert first_intent.require_breakfast is True
            assert first_intent.require_checked_baggage is True
            assert first_intent.allow_connections is True

            current_override = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(
                    text=(
                        "出发地：杭州，目的地：马累，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房，"
                        "早餐无要求、无行李、不接受中转"
                    )
                ),
            )
            assert current_override.status_code == 200, current_override.text
            overridden_intent = runner.calls[-1][0]
            assert overridden_intent.require_breakfast is None
            assert overridden_intent.require_checked_baggage is False
            assert overridden_intent.allow_connections is False

            for record_id in record_ids:
                revoked = await client.delete(f"/api/v1/agents/memory/{record_id}")
                assert revoked.status_code == 200, revoked.text
            after_revoke = await client.post(
                "/api/v1/agents/live-flexible-plan-from-text",
                json=_payload(
                    text="出发地：杭州，目的地：马累，去程：2026年8月，玩5-8天，人数：2名成人，酒店：1间房"
                ),
            )
            assert after_revoke.status_code == 200, after_revoke.text
            revoked_intent = runner.calls[-1][0]
            assert revoked_intent.require_breakfast is None
            assert revoked_intent.require_checked_baggage is None
            assert revoked_intent.allow_connections is None
    finally:
        app.dependency_overrides.pop(get_principal, None)
