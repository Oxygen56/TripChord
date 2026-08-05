from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from tripchord.agents.companion_control_tools import (
    COMPANION_BUILD_FILE_ALLOWLIST,
    BrowserCompanionReleaseSeal,
    companion_build_sha256,
)

from scripts import capture_live_browser_evidence as capture

CTRIP_TASK_ID = "browser-task-ctrip-focused-fixture"
QUNAR_TASK_ID = "browser-task-qunar-focused-fixture"
BUILD_SHA256 = "a" * 64
OLD_BUILD_SHA256 = "b" * 64
RUNTIME_ID = "tripchord-runtime-current-fixture-0001"
OLD_RUNTIME_ID = "tripchord-runtime-previous-fixture-001"
COMPANION_ID = "chrome-mv3-fixture-companion"


def _query(*, start_date: str = "2026-08-21") -> dict[str, Any]:
    return {
        "origin": None,
        "destination": "Maafushi",
        "start_date": start_date,
        "end_date": "2026-08-26",
        "adults": 2,
        "rooms": 1,
        "currency": "CNY",
        "origin_code": None,
        "destination_code": None,
        "search_url": None,
        "options": {
            "segment": "full",
            "expected_package_area": "destination_island",
            "expected_lodging_place_key": "maafushi",
            "__tripchord_allow_recent_quote_reuse": False,
        },
    }


def _quote(*, amount: str, evidence_sha256: str, property_id: str) -> dict[str, Any]:
    property_name = f"Fixture Hotel {property_id}"
    return {
        "provider": "ctrip",
        "kind": "lodging",
        "page_url": (
            "https://hotels.ctrip.com/hotels/detail/?"
            f"hotelId={property_id}&checkIn=2026-08-21&checkOut=2026-08-26&"
            "adult=2&crn=1&curr=CNY&masterhotelid_tracelogid=secret-tracker&"
            "detailFilters=unsafe-long-visible-contract"
        ),
        "captured_at": "2026-08-05T04:37:21.355000Z",
        "parser_version": "tripchord-visible-dom-v3",
        "visible_evidence": "very long raw visible page text that must not be persisted",
        "evidence_sha256": evidence_sha256,
        "currency": "CNY",
        "amount": amount,
        "price_basis": "per_night",
        "taxes_included": True,
        "title": property_name,
        "details": {
            "property_id": property_id,
            "property_name": property_name,
            "price_finality": "final_for_rate",
            "availability": "available",
            "rate_text": "raw rate row must not be persisted",
        },
    }


def _ctrip_task() -> dict[str, Any]:
    return {
        "id": CTRIP_TASK_ID,
        "provider": "ctrip",
        "kind": "lodging",
        "query": _query(),
        "state": "succeeded",
        "created_at": "2026-08-05T04:37:05.721502Z",
        "updated_at": "2026-08-05T04:37:24.970958Z",
        "attempt_count": 1,
        "claimed_by": "fixture",
        "claimed_at": "2026-08-05T04:37:06Z",
        "quotes": [
            _quote(amount="900", evidence_sha256="1" * 64, property_id="2"),
            _quote(amount="778", evidence_sha256="2" * 64, property_id="1"),
        ],
        "failure": None,
        "reused_from_task_id": None,
        "reuse_age_seconds": None,
        "inflight_coalesced": False,
    }


def _url_shape(host: str, path: str, *, query_keys: list[str]) -> dict[str, Any]:
    return {
        "parseable": True,
        "scheme": "https",
        "host": host,
        "path_shape": path,
        "query_keys": query_keys,
        "query_keys_truncated": False,
        "has_fragment": False,
    }


def _qunar_task() -> dict[str, Any]:
    return {
        "id": QUNAR_TASK_ID,
        "provider": "qunar",
        "kind": "lodging",
        "query": _query(),
        "state": "blocked",
        "created_at": "2026-08-05T04:37:05.721570Z",
        "updated_at": "2026-08-05T04:37:07.522539Z",
        "attempt_count": 1,
        "claimed_by": "fixture",
        "claimed_at": "2026-08-05T04:37:06Z",
        "quotes": [],
        "failure": {
            "code": "login_required",
            "message": "long visible failure text must not be persisted",
            "retryable": False,
            "page_url": "https://hotel.qunar.com/global/?utm_source=tracking",
            "captured_at": "2026-08-05T04:37:07.485000Z",
            "details": {
                "navigation_diagnostic": {
                    "provider": "qunar",
                    "vertical": "lodging",
                    "stage": "observe_navigation",
                    "reason": "audited_login_redirect",
                    "rejected_url": _url_shape(
                        "user.qunar.com", "/passport/login.jsp", query_keys=["ret"]
                    ),
                    "login_return_url": _url_shape(
                        "hotel.qunar.com", "/city/i-ka_maafushi", query_keys=[]
                    ),
                    "redirect_trace": [
                        {
                            "sequence": 0,
                            "phase": "observer_started",
                            "tab_role": "source",
                            "status": None,
                            "transient": False,
                            "url": _url_shape(
                                "hotel.qunar.com", "/global", query_keys=[]
                            ),
                        },
                        {
                            "sequence": 1,
                            "phase": "navigation_loading",
                            "tab_role": "source",
                            "status": "loading",
                            "transient": False,
                            "url": _url_shape(
                                "user.qunar.com",
                                "/passport/login.jsp",
                                query_keys=["ret"],
                            ),
                        },
                    ],
                },
                "browser_isolation": {
                    "scope": "companion_owned_unfocused_normal_window_active_tab",
                    "owner": "browser_companion",
                    "window_type": "normal",
                    "requested_window_state": "normal",
                    "requested_focused": False,
                    "tab_active_in_isolated_window": True,
                    "reused_user_window": False,
                    "minimized": False,
                    "cleanup_policy": "close_before_lease_completion_and_retry_in_finally",
                    "fallback_policy": "fail_closed_without_activating_a_user_window",
                    "lifecycle_state": "closed_before_lease_completion",
                    "observed_focused": False,
                    "observed_window_state": "normal",
                    "observed_active_tab_count": 1,
                },
                "lease_timing": {
                    "deadline_source": "server_absolute",
                    "lease_duration_ms": 120000,
                    "completion_reserve_ms": 20000,
                    "lease_expires_at": "2026-08-05T04:39:05.991Z",
                    "work_deadline_at": "2026-08-05T04:38:45.991Z",
                },
                "stage_trace": [
                    {
                        "sequence": 1,
                        "stage": "initial_landing",
                        "status": "completed",
                        "budget_ms": 40000,
                        "elapsed_ms": 960,
                        "remaining_lease_ms": 99031,
                        "failure_code": None,
                    },
                    {
                        "sequence": 2,
                        "stage": "trigger_search",
                        "status": "failed",
                        "budget_ms": 30000,
                        "elapsed_ms": 502,
                        "remaining_lease_ms": 98506,
                        "failure_code": "login_required",
                    },
                ],
                "dom_diagnostics": {"visible_text": "must not escape"},
            },
        },
        "reused_from_task_id": None,
        "reuse_age_seconds": None,
        "inflight_coalesced": False,
    }


def _build_identity() -> dict[str, str]:
    return {
        "protocol_version": "tripchord-companion-control-v1",
        "manifest_version": "0.1.14",
        "build_sha256": BUILD_SHA256,
        "content_runtime_version": "2026-08-05.14",
    }


def _companion_status() -> dict[str, Any]:
    return {
        "status": "connected",
        "server_time": "2026-08-05T04:40:00Z",
        "stale_after_seconds": 45,
        "companions": [
            {
                "companion_id": COMPANION_ID,
                "providers": ["ctrip", "qunar", "tongcheng"],
                "last_seen": "2026-08-05T04:39:59Z",
                "age_seconds": 1,
                "is_fresh": True,
                "build_identity": _build_identity(),
                "runtime_instance_id": RUNTIME_ID,
            }
        ],
    }


def _runtime() -> dict[str, Any]:
    return {
        "codex_runtime_dependency": False,
        "chatgpt_runtime_dependency": False,
        "model_enabled": True,
        "model_required": True,
        "model_provider": "openai_compatible",
        "primary_model": "deepseek-v4-flash",
        "fast_model": "deepseek-v4-flash",
        "model_trace_count": 999,
        "browser_companion_control_enabled": False,
        "browser_companion_auto_reload_enabled": True,
        "browser_companion_supervisor_running": True,
        "browser_companion_supervisor_outcome": "local_build_invalid",
        "browser_companion_supervisor_attempt_count": 1,
        "browser_companion_last_reconcile": {
            "request_id": "companion-reload-fixture-request",
            "companion_id": COMPANION_ID,
            "state": "applied",
            "old_build_sha256": OLD_BUILD_SHA256,
            "target_build_sha256": BUILD_SHA256,
            "old_runtime_instance_id": OLD_RUNTIME_ID,
            "new_runtime_instance_id": RUNTIME_ID,
            "timings": {
                "requested_at": "2026-08-05T04:35:44.068925Z",
                "updated_at": "2026-08-05T04:35:44.722783Z",
                "accepted_at": "2026-08-05T04:35:44.352786Z",
                "applied_at": "2026-08-05T04:35:44.722783Z",
                "elapsed_ms": 653,
            },
        },
    }


def _source_summary() -> dict[str, Any]:
    query = _query()
    query.pop("origin")
    query.pop("origin_code")
    query.pop("destination_code")
    query.pop("search_url")
    options = query.pop("options")
    query.update(
        {
            "segment": options["segment"],
            "expected_package_area": options["expected_package_area"],
            "expected_lodging_place_key": options["expected_lodging_place_key"],
        }
    )
    return {
        "schema_version": "tripchord-live-browser-focused-evidence-v1",
        "captured_at": "2026-08-05T04:37:24.970958Z",
        "scope": "background_read_only_lodging_canary",
        "query": query,
        "runtime": {
            "model_provider": "openai_compatible",
            "primary_model": "deepseek-v4-flash",
            "model_required": True,
            "companion_manifest_version": "0.1.14",
            "companion_content_runtime_version": "2026-08-05.14",
            "companion_build_sha256": BUILD_SHA256,
            "companion_runtime_instance_id": RUNTIME_ID,
            "background_reload_verified": True,
            "browser_focused": False,
        },
        "sources": [
            {
                "task_id": CTRIP_TASK_ID,
                "provider": "ctrip",
                "state": "succeeded",
                "attempt_count": 1,
                "quote_count": 2,
                "lowest_audited_rate": {
                    "amount": "778",
                    "currency": "CNY",
                    "price_basis": "per_night",
                    "taxes_included": True,
                    "property_id": "1",
                    "property_name": "Fixture Hotel 1",
                    "evidence_sha256": "2" * 64,
                },
            },
            {
                "task_id": QUNAR_TASK_ID,
                "provider": "qunar",
                "state": "blocked",
                "attempt_count": 1,
                "quote_count": 0,
                "failure": {"code": "login_required"},
            },
            {
                "provider": "tongcheng",
                "state": "skipped_by_user",
                "vertical": "lodging",
                "coverage_credit": False,
            },
        ],
        "assessment": {
            "one_provider_exact_lodging_quote": True,
            "two_provider_exact_lodging_quote": False,
            "strict_lodging_gate_passed": False,
            "publication_gate_attempted": False,
            "publication_gate_passed": False,
        },
    }


def _release_binding() -> dict[str, Any]:
    return {
        "sealed_identity": _build_identity(),
        "release_seal_sha256": "3" * 64,
        "build_meta_sha256": "4" * 64,
        "current_worktree_build_sha256": "5" * 64,
        "current_worktree_matches_captured_build": False,
        "boundary": "The worktree changed after the captured terminal tasks.",
    }


def _build_artifact() -> dict[str, object]:
    return capture.build_sealed_evidence(
        task_payloads=[_ctrip_task(), _qunar_task()],
        task_ids=[CTRIP_TASK_ID, QUNAR_TASK_ID],
        companion_payload=_companion_status(),
        companion_id=COMPANION_ID,
        runtime_payload=_runtime(),
        source_summary=_source_summary(),
        source_artifacts=[{"path": "summary.json", "sha256": "6" * 64}],
        release_binding=_release_binding(),
    )


def test_sealed_projection_is_recomputable_and_drops_raw_page_text() -> None:
    artifact = _build_artifact()

    capture.verify_sealed_evidence(artifact)
    serialized = json.dumps(artifact, ensure_ascii=False)
    assert "visible_evidence" not in serialized
    assert "raw rate row" not in serialized
    assert "long visible failure text" not in serialized
    assert "masterhotelid_tracelogid" not in serialized
    assert "secret-tracker" not in serialized
    assert artifact["derived_assessment"] == {
        "quote_count_by_provider": {"ctrip": 2, "qunar": 0},
        "terminal_state_by_provider": {"ctrip": "succeeded", "qunar": "blocked"},
        "exact_all_in_lodging_providers": ["ctrip"],
        "exact_all_in_lodging_provider_count": 1,
        "lowest_exact_all_in_rate_by_provider": {
            "ctrip": {
                "amount": "778",
                "currency": "CNY",
                "price_basis": "per_night",
                "taxes_included": True,
                "price_finality": "final_for_rate",
                "property": {"id": "1", "name": "Fixture Hotel 1"},
                "evidence_sha256": "2" * 64,
            }
        },
        "strict_lodging_provider_threshold": 2,
        "strict_lodging_gate_passed": False,
        "publication_gate_attempted": False,
        "publication_gate_passed": False,
    }
    sources = {item["provider"]: item for item in artifact["source_tasks"]}
    ctrip_quote = sources["ctrip"]["quotes"][0]
    assert ctrip_quote["page_url"].startswith("https://hotels.ctrip.com/hotels/detail/")
    assert ctrip_quote["property"] == {"id": "1", "name": "Fixture Hotel 1"}
    qunar_failure = sources["qunar"]["failure"]
    assert qunar_failure["navigation_diagnostic"]["rejected_url"]["host"] == (
        "user.qunar.com"
    )
    assert qunar_failure["browser_isolation"]["observed_focused"] is False
    receipt = artifact["runtime"]["browser_companion_last_reconcile"]
    assert receipt["state"] == "applied"
    assert receipt["old_build_sha256"] == OLD_BUILD_SHA256
    assert receipt["target_build_sha256"] == BUILD_SHA256
    assert artifact["integrity"]["input_sha256"]
    assert artifact["integrity"]["result_sha256"]


def test_tampered_quote_fails_integrity_verification() -> None:
    artifact = copy.deepcopy(_build_artifact())
    artifact["source_tasks"][0]["quotes"][0]["amount"] = "1"

    with pytest.raises(capture.EvidenceCaptureError, match="task snapshot digest"):
        capture.verify_sealed_evidence(artifact)


def test_capture_without_human_summary_and_derive_compact_summary() -> None:
    artifact = capture.build_sealed_evidence(
        task_payloads=[_ctrip_task(), _qunar_task()],
        task_ids=[CTRIP_TASK_ID, QUNAR_TASK_ID],
        companion_payload=_companion_status(),
        companion_id=COMPANION_ID,
        runtime_payload=_runtime(),
        source_summary=None,
        source_artifacts=[{"path": "source.py", "sha256": "7" * 64}],
        release_binding=_release_binding(),
        scope_decisions=[
            {
                "provider": "tongcheng",
                "vertical": "lodging",
                "state": "skipped_by_user",
                "coverage_credit": False,
            }
        ],
    )

    summary = capture.build_run_summary(
        artifact,
        sealed_artifact_name="focused.sealed.json",
    )

    assert artifact["scope_decisions"] == [
        {
            "provider": "tongcheng",
            "vertical": "lodging",
            "state": "skipped_by_user",
            "coverage_credit": False,
        }
    ]
    assert summary["schema_version"] == "tripchord-live-browser-focused-run-summary-v2"
    assert summary["sources"][0]["quote_count"] == 2
    assert summary["sources"][1]["failure"]["code"] == "login_required"
    assert summary["assessment"]["one_provider_exact_lodging_quote"] is True
    assert summary["sealed_evidence"]["result_sha256"] == artifact["integrity"][
        "result_sha256"
    ]
    unsigned = dict(summary)
    result_sha256 = unsigned.pop("result_sha256")
    assert result_sha256 == capture._canonical_sha256(unsigned)


def test_query_mismatch_fails_before_sealing() -> None:
    qunar = _qunar_task()
    qunar["query"] = _query(start_date="2026-08-22")

    with pytest.raises(capture.EvidenceCaptureError, match="one exact query"):
        capture.build_sealed_evidence(
            task_payloads=[_ctrip_task(), qunar],
            task_ids=[CTRIP_TASK_ID, QUNAR_TASK_ID],
            companion_payload=_companion_status(),
            companion_id=COMPANION_ID,
            runtime_payload=_runtime(),
            source_summary=_source_summary(),
            source_artifacts=[{"path": "summary.json", "sha256": "6" * 64}],
            release_binding=_release_binding(),
        )


def test_atomic_writer_uses_mode_0600_and_refuses_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "sealed.json"

    capture._atomic_write_json(output, _build_artifact())

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    capture.verify_sealed_evidence(json.loads(output.read_text(encoding="utf-8")))
    with pytest.raises(capture.EvidenceCaptureError, match="refusing to overwrite"):
        capture._atomic_write_json(output, _build_artifact())


def test_compact_summary_writer_uses_mode_0644(tmp_path: Path) -> None:
    output = tmp_path / "summary.json"
    artifact = _build_artifact()

    capture._atomic_write_json(
        output,
        capture.build_run_summary(artifact, sealed_artifact_name="focused.sealed.json"),
        mode=0o644,
    )

    assert stat.S_IMODE(output.stat().st_mode) == 0o644


def test_secure_token_rejects_open_permissions_and_symlink(tmp_path: Path) -> None:
    token = tmp_path / "bridge-token"
    token.write_text("x" * 64, encoding="utf-8")
    token.chmod(0o644)
    with pytest.raises(capture.EvidenceCaptureError, match="mode 0600"):
        capture._secure_token(token)
    token.chmod(0o600)
    assert capture._secure_token(token) == "x" * 64
    link = tmp_path / "bridge-token-link"
    link.symlink_to(token)
    with pytest.raises(capture.EvidenceCaptureError, match="not a symlink"):
        capture._secure_token(link)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000",
        "http://example.com:8000",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
    ],
)
def test_base_url_is_loopback_only(base_url: str) -> None:
    with pytest.raises(capture.EvidenceCaptureError, match="loopback"):
        capture._validate_base_url(base_url)


def test_http_reader_uses_get_and_does_not_send_bridge_token_to_runtime() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get(capture.BRIDGE_TOKEN_HEADER)))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    ) as client:
        assert capture._fetch_json(client, "/browser", bridge_token="x" * 64) == {
            "ok": True
        }
        assert capture._fetch_json(client, "/runtime") == {"ok": True}

    assert seen == [("GET", "x" * 64), ("GET", None)]


def test_historical_release_binding_retains_seal_after_worktree_change(
    tmp_path: Path,
) -> None:
    companion = tmp_path / "apps" / "browser-companion"
    for relative_path in COMPANION_BUILD_FILE_ALLOWLIST:
        path = companion / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative_path == "manifest.json":
            path.write_text(json.dumps({"version": "0.1.14"}), encoding="utf-8")
        elif relative_path == "src/background.js":
            path.write_text(
                'const CONTENT_RUNTIME_VERSION = "2026-08-05.14";',
                encoding="utf-8",
            )
        else:
            path.write_text(f"fixture:{relative_path}", encoding="utf-8")
    build_sha256 = companion_build_sha256(companion)
    identity = {
        "protocol_version": "tripchord-companion-control-v1",
        "manifest_version": "0.1.14",
        "build_sha256": build_sha256,
        "content_runtime_version": "2026-08-05.14",
    }
    meta = companion / "src" / "build-meta.js"
    meta.write_text(
        "// Generated by scripts/update-build-meta.mjs. Do not edit by hand.\n"
        "globalThis.TRIPCHORD_COMPANION_BUILD_META = "
        f"Object.freeze({json.dumps(identity)});\n",
        encoding="utf-8",
    )
    seal = BrowserCompanionReleaseSeal(
        **identity,
        build_meta_sha256=hashlib.sha256(meta.read_bytes()).hexdigest(),
    )
    seal_path = companion / ".tripchord-release-seal.json"
    seal_path.write_text(
        json.dumps(seal.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )
    seal_path.chmod(0o600)
    (companion / "popup.css").write_text("changed after canary", encoding="utf-8")

    binding = capture._release_binding(tmp_path)

    assert binding["sealed_identity"] == identity
    assert binding["current_worktree_matches_captured_build"] is False
    assert binding["current_worktree_build_sha256"] != build_sha256
    assert os.stat(seal_path).st_mode & 0o777 == 0o600
