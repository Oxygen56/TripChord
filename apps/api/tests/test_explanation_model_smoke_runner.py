from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from scripts.run_explanation_model_smoke import (
    build_parser,
    execute_from_arguments,
    main,
    validate_arguments,
)


def _arguments(tmp_path: Path) -> list[str]:
    return [
        "--ack-live-cost",
        "--provider",
        "openai_compatible",
        "--model",
        "deepseek-v4-flash",
        "--base-url",
        "https://model.example/v1",
        "--output",
        str(tmp_path / "explanation-smoke.json"),
    ]


def _selection_from_first_request(
    first_body: dict[str, Any],
    *,
    final_candidate_id: str | None = None,
) -> dict[str, Any]:
    messages = first_body["messages"]
    assert isinstance(messages, list)
    initial: dict[str, Any] | None = None
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            continue
        try:
            parsed = json.loads(message["content"])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("proposal_policy") is not None:
            initial = parsed
            break
    assert initial is not None
    policy = initial["proposal_policy"]["context"]
    allowed = policy["allowed_claim_ids_by_section"]
    return {
        "catalogue_sha256": policy["catalogue_sha256"],
        "final_candidate_id": final_candidate_id or policy["final_candidate_id"],
        "summary_claim_id": allowed["summary"][0],
        "why_selected_claim_ids": [allowed["why_selected"][0]],
        "tradeoff_claim_ids": allowed["tradeoff"][:1],
        "uncertainty_claim_ids": allowed["uncertainty"][:3],
        "next_user_action_claim_ids": allowed["next_user_action"][:1],
    }


def test_cli_requires_cost_ack_before_any_model_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "must-not-be-used")
    with pytest.raises(SystemExit):
        main(
            [
                "--provider",
                "openai_compatible",
                "--model",
                "deepseek-v4-flash",
                "--base-url",
                "https://unreachable.invalid/v1",
                "--output",
                str(tmp_path / "must-not-exist.json"),
            ]
        )
    assert not (tmp_path / "must-not-exist.json").exists()


@pytest.mark.asyncio
async def test_focused_explanation_smoke_uses_production_schema_and_redacts_plaintext(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret = "focused-smoke-secret"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    bodies: list[dict[str, object]] = []
    invalid_freeform_text = "该酒店包含早餐"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            message: dict[str, object] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "focused-handoff-call",
                        "type": "function",
                        "function": {
                            "name": "inspect_planning_handoffs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        elif len(bodies) == 2:
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "summary": invalid_freeform_text,
                        "why_selected": ["模型自由改写的原因"],
                        "tradeoffs": [],
                        "uncertainties": [],
                        "next_user_actions": [],
                        "evidence_refs": [],
                        "grounding": [],
                    },
                    ensure_ascii=False,
                ),
            }
            finish_reason = "stop"
        elif len(bodies) == 3:
            message = {
                "role": "assistant",
                "content": json.dumps(
                    _selection_from_first_request(bodies[0]),
                    ensure_ascii=False,
                ),
            }
            finish_reason = "stop"
        else:
            raise AssertionError("focused explanation smoke exceeded three calls")
        return httpx.Response(
            200,
            json={
                "id": f"focused-response-{len(bodies)}",
                "choices": [{"finish_reason": finish_reason, "message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    parser = build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    validate_arguments(parser, args)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await execute_from_arguments(args, http_client=client)

    serialized = json.dumps(report, ensure_ascii=False)
    contract = report["contract"]
    observed = report["observed"]
    assert isinstance(contract, dict)
    assert isinstance(observed, dict)
    assert report["passed"] is True
    assert report["schema_version"] == "tripchord-explanation-model-smoke-v2"
    assert contract["model_output_schema"] == "ExplanationSelectionProposal"
    assert contract["materialized_public_schema"] == "ExplanationProposal"
    assert contract["model_selects_claim_ids_only"] is True
    assert contract["server_materializes_user_visible_prose"] is True
    assert contract["local_selection_policy_passed"] is True
    assert contract["tool_removed_before_final_json"] is True
    assert contract["local_grounding_validation_passed"] is True
    assert observed["logical_model_calls"] == 2
    assert observed["http_attempts"] == 3
    assert observed["request_tool_counts"] == [1, 0]
    assert observed["request_max_tokens"] == [2048, 2048]
    assert observed["proposal_repair_count"] == 0
    assert observed["selected_why_claim_id_count"] == 1
    assert observed["selected_tradeoff_claim_id_count"] == 1
    assert observed["selected_uncertainty_claim_id_count"] == 3
    assert observed["selected_next_action_claim_id_count"] == 1
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert "tools" in bodies[0]
    assert "tools" not in bodies[1]
    assert "tools" not in bodies[2]
    final_schema_prompt = json.dumps(bodies[1]["messages"], ensure_ascii=False)
    assert "summary_claim_id" in final_schema_prompt
    assert "why_selected_claim_ids" in final_schema_prompt
    assert "grounding.claim" not in final_schema_prompt
    schema_repair_body = json.dumps(bodies[2], ensure_ascii=False)
    assert "Structured output repair attempt 1" in schema_repair_body
    assert "allowed_claim_ids_by_section" in schema_repair_body
    assert "required_claim_ids" in schema_repair_body
    assert bodies[2]["max_tokens"] == 4096
    assert invalid_freeform_text not in schema_repair_body
    assert secret not in serialized
    assert invalid_freeform_text not in serialized
    assert "sanitized-provider-a" not in serialized
    assert "Explain the bounded final handoff" not in serialized


@pytest.mark.asyncio
async def test_focused_explanation_smoke_fails_closed_after_one_selection_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "focused-fail-closed-secret")
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            message: dict[str, object] = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "focused-fail-closed-call",
                        "type": "function",
                        "function": {
                            "name": "inspect_planning_handoffs",
                            "arguments": "{}",
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": json.dumps(
                    _selection_from_first_request(
                        bodies[0],
                        final_candidate_id="package:not-the-frozen-candidate",
                    ),
                    ensure_ascii=False,
                ),
            }
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"focused-fail-closed-{len(bodies)}",
                "choices": [{"finish_reason": finish_reason, "message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    parser = build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    validate_arguments(parser, args)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(
            RuntimeError,
            match="required explanation model failed closed: ValueError",
        ):
            await execute_from_arguments(args, http_client=client)

    assert len(bodies) == 3
    assert "tools" in bodies[0]
    assert "tools" not in bodies[1]
    assert "tools" not in bodies[2]
    repair_body = json.dumps(bodies[2], ensure_ascii=False)
    assert "explanation-evidence-constrained-discourse-v3" in repair_body
    assert "final_candidate_id" in repair_body
    assert "catalogue_sha256" in repair_body


@pytest.mark.asyncio
async def test_execute_requires_key_and_cost_ack_before_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(_arguments(tmp_path))
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="missing API key"):
        await execute_from_arguments(args)

    args.ack_live_cost = False
    monkeypatch.setenv("MODEL_API_KEY", "unused")
    with pytest.raises(RuntimeError, match="cost acknowledgement"):
        await execute_from_arguments(args)
