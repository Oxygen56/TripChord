from __future__ import annotations

import pytest
from tripchord.mcp_server import (
    _canonical_requirement,
    _stable_request_id,
    _trip_card,
    _version_summary,
    create_plan,
    mcp,
)


def test_mcp_exposes_only_coarse_tripchord_operations() -> None:
    assert set(mcp._tool_manager._tools) == {
        "create_plan",
        "get_plan_status",
        "get_plan",
        "modify_plan",
    }


def test_mcp_idempotency_key_is_stable_per_requirement_and_reference_date() -> None:
    first = _stable_request_id("  去\n上海  ", "2026-08-26", None)
    retry = _stable_request_id("去 上海", "2026-08-26", None)
    next_reference_date = _stable_request_id("去 上海", "2026-08-27", None)

    assert first == retry
    assert first != next_reference_date
    assert _stable_request_id("去上海", "2026-08-26", "caller-request-1") == ("caller-request-1")


@pytest.mark.asyncio
async def test_create_plan_submits_the_same_canonical_text_used_for_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_request(
        method: str,
        path: str,
        *,
        json: object = None,
        headers: object = None,
        operation: str,
    ) -> dict[str, object]:
        captured.update({"method": method, "path": path, "json": json, "headers": headers})
        return {"job": {"id": "run-1", "state": "queued"}}

    monkeypatch.setattr("tripchord.mcp_server._request", fake_request)
    raw = "  2名成人，\n  杭州出发到上海  "
    result = await create_plan(raw, reference_date=" 2026-08-26 ")

    canonical = _canonical_requirement(raw)
    payload = captured["json"]
    headers = captured["headers"]
    assert isinstance(payload, dict)
    assert isinstance(headers, dict)
    assert payload["requirement"]["text"] == canonical
    assert payload["requirement"]["reference_date"] == "2026-08-26"
    assert headers["Idempotency-Key"] == _stable_request_id(canonical, "2026-08-26", None)
    assert result["run_id"] == "run-1"


def test_mcp_plan_view_does_not_expose_internal_agent_labels() -> None:
    card = _trip_card(
        {
            "selected_trip_card": {
                "status": "final",
                "participating_agent_roles": ["internal-role"],
                "applied_skill_ids": ["internal-skill"],
                "total_cny_cents": 100,
            }
        }
    )

    assert card == {"status": "final", "total_cny_cents": 100}


def test_mcp_version_summary_projects_card_status() -> None:
    summary = _version_summary(
        {
            "id": "run:plan:v1",
            "version": 1,
            "status": None,
            "selected_trip_card": {"status": "final"},
        }
    )

    assert summary["status"] == "final"
