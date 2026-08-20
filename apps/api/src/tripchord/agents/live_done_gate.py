from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal, cast
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, JsonValue

from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexiblePairState,
)
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LiveDataProvider,
    LiveEventReplanRun,
    LivePackageAgentRun,
)
from tripchord.agents.models import AgentTask
from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    NormalizedFlightQuote,
    PackageDecisionState,
    PackagePlannerHandoff,
    PackagePlanningHandoff,
    PackageRepairHandoff,
    PackageVerificationHandoff,
    PackageVerificationPhase,
    TransferPriceGuarantee,
    package_budget,
)
from tripchord.planning.package_reverification import PackageReverificationReport
from tripchord.providers.browser_bridge import (
    LIVE_V5_BROWSER_PROVIDERS,
    PRODUCTION_VISIBLE_DOM_PARSER_VERSION,
    BrowserProvider,
    BrowserQuote,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    FlightSearchReceipt,
    LodgingInventoryReceipt,
    flight_search_receipt_sha256,
    lodging_inventory_receipt_sha256,
)
from tripchord.providers.icom_transfer import (
    IComLocation,
    IComTransferQuery,
    IComTransferSearchResult,
    to_package_transfer_option,
)

_EXPECTED_PROVIDERS = frozenset(LIVE_V5_BROWSER_PROVIDERS)
_LODGING_PROVIDERS = frozenset({BrowserProvider.CTRIP, BrowserProvider.QUNAR})
_EXPECTED_SOURCE_COUNTS = {
    BrowserProvider.CTRIP: 6,
    BrowserProvider.QUNAR: 6,
    BrowserProvider.TONGCHENG: 1,
}
_EXPECTED_PUBLIC_TRANSFER_TASK_IDS = (
    "public-transfer-icom-continuous-outbound",
    "public-transfer-icom-split-outbound",
    "public-transfer-icom-split-inbound",
    "public-transfer-icom-continuous-inbound",
)
_ICOM_SEARCH_TOOL = "icom_public_transfer_search"
_BROWSER_SEARCH_TOOL = "browser_bridge_search"
_MODEL_READ_ONLY_TOOLS = frozenset(
    {
        "inspect_search_capabilities",
        "inspect_normalized_inventory",
        "inspect_package_candidates",
        "inspect_package_verification",
        "inspect_planning_handoffs",
    }
)
_ICOM_HOST = "sfs-api.icomtours.com"
_ICOM_SCHEDULE_PATH = "/api/v1/public/trips/schedules"
_ICOM_BASE_FARE_PATH = "/api/v1/public/ferry-fares/schedule-base-price"
_ICOM_POLICY_PATH = "/api/v1/public/policy-sections"
_ICOM_ALLOWED_PATHS = (
    _ICOM_SCHEDULE_PATH,
    _ICOM_BASE_FARE_PATH,
    _ICOM_POLICY_PATH,
)
_REQUIRED_ICOM_FIELD_EVIDENCE = frozenset(
    {
        "trip_id",
        "schedule_id",
        "route",
        "departure_at",
        "arrival_at",
        "remaining_capacity",
        "is_cancelled",
        "availability_status",
        "fare.amount",
        "fare.currency",
        "fare.basis",
        "fare.taxes_included",
    }
)
_FORBIDDEN_PARSER_MARKERS = ("fixture", "mock", "replay", "scripted", "synthetic", "test")
_UNCONFIRMED_DRIVER_SCOPES = {
    "not_started",
    "provider_url_only_unverified",
    "visible_form_unverified",
    "navigation_response_lost",
}
_FORBIDDEN_ACTION_MARKERS = (
    "account",
    "book",
    "booking",
    "cashier",
    "checkout",
    "coupon",
    "order",
    "pay",
    "payment",
    "下单",
    "使用优惠券",
    "修改账号",
    "改账号",
    "预订",
    "优惠券",
    "支付",
)
_ALLOWED_BROWSER_TRACE_ACTIONS = frozenset(
    {
        "search",
        "filter",
        "select_outbound",
        "reselect_outbound",
        "provider_auto_selected_outbound",
        "select_return",
    }
)


class LiveDoneGateCheck(DomainModel):
    id: str = Field(min_length=1)
    passed: bool
    summary: str = Field(min_length=1)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)


class LiveDoneGateReport(DomainModel):
    gate_version: Literal["tripchord-live-v3"] = "tripchord-live-v3"
    evaluated_at: datetime
    passed: bool
    checks: tuple[LiveDoneGateCheck, ...]
    bundle_sha256: str = Field(min_length=64, max_length=64)
    claim_boundary: str = (
        "仅证明这一份已认证浏览器与 iCom 官方公共读取证据满足 TripChord live-v3 验收合同；"
        "不代表全月最低、全网最低、库存锁定、可订或生产采用。"
    )


def evaluate_live_done_gate(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    *,
    flexible: FlexibleLiveAgentRun | None = None,
    evaluated_at: datetime | None = None,
    maximum_quote_age: timedelta = timedelta(minutes=15),
) -> LiveDoneGateReport:
    """Evaluate real runtime evidence. Offline fixtures are intentionally rejected."""
    now = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    snapshots, snapshot_errors = _source_snapshots(initial)
    event_snapshots, event_snapshot_errors = _event_source_snapshots(event)
    checks = (
        _check_flexible_ranked_options(
            flexible,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _check_strict_coverage(initial),
        _check_source_dag(initial),
        _check_icom_public_transfer_evidence(
            initial,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _check_real_browser_evidence(
            snapshots,
            snapshot_errors,
            now=now,
            maximum_quote_age=maximum_quote_age,
            check_id="real_browser_evidence",
        ),
        _check_round_trip_combination_evidence(
            initial,
            event,
            snapshots,
            event_snapshots,
        ),
        _check_browser_action_trace_read_only(
            initial,
            event,
            snapshots,
            event_snapshots,
        ),
        _check_actual_overlap(snapshots),
        _check_read_only_graph(initial, event),
        _check_planner_verifier_repair(initial),
        _check_selected_party_availability(initial, event),
        _check_budget_and_evidence(initial, now=now),
        _check_event_replan(
            initial,
            event,
            event_snapshots,
            event_snapshot_errors,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
    )
    canonical = json.dumps(
        {
            "flexible": (flexible.model_dump(mode="json") if flexible is not None else None),
            "initial": initial.model_dump(mode="json"),
            "event": event.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return LiveDoneGateReport(
        evaluated_at=now,
        passed=all(check.passed for check in checks),
        checks=checks,
        bundle_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _check_flexible_ranked_options(
    flexible: FlexibleLiveAgentRun | None,
    *,
    now: datetime | None = None,
    maximum_quote_age: timedelta = timedelta(minutes=15),
) -> LiveDoneGateCheck:
    if flexible is None:
        return LiveDoneGateCheck(
            id="flexible_ranked_options",
            passed=False,
            summary="缺少灵活日期多方案运行证据",
            evidence={
                "selected_pair_count": 0,
                "completed_pair_count": 0,
                "recommendable_count": 0,
            },
        )

    selected_ids = tuple(flexible.query_plan.selected_pair_ids)
    completed_ids = {
        item.date_pair.id
        for item in flexible.pair_runs
        if item.state == FlexiblePairState.COMPLETED and item.run is not None
    }
    recommendable = tuple(
        item
        for item in flexible.ranked_options
        if item.recommendable
        and item.decision_state == PackageDecisionState.ACCEPT
        and item.all_platforms_complete
        and item.total_budget_cents is not None
        and item.evidence_completeness == Decimal(1)
        and item.date_pair_id in completed_ids
    )
    recommendable_ids = tuple(item.date_pair_id for item in recommendable)
    final_recommendable_ids = recommendable_ids[:1]
    rank_sequence = tuple(item.rank for item in flexible.ranked_options)
    pair_run_by_id = {
        execution.date_pair.id: execution.run
        for execution in flexible.pair_runs
        if execution.run is not None
    }
    evaluated_at = (now or datetime.now(UTC)).astimezone(UTC)
    public_transfer_pair_checks: dict[str, bool] = {}
    for pair_id in recommendable_ids:
        run = pair_run_by_id.get(pair_id)
        public_transfer_pair_checks[pair_id] = (
            run is not None
            and _check_source_dag(run).passed
            and _check_icom_public_transfer_evidence(
                run,
                now=evaluated_at,
                maximum_quote_age=maximum_quote_age,
            ).passed
        )
    recommended_pairs_have_icom_4_of_4 = len(public_transfer_pair_checks) >= 1 and all(
        public_transfer_pair_checks.values()
    )
    sampled_boundary_ok = (
        not flexible.sampled_not_exhaustive or "不得声称全月最低价" in flexible.claim_boundary
    )
    passed = (
        len(selected_ids) == 3
        and len(flexible.query_plan.tasks) == 33
        and len(flexible.pair_runs) == 3
        and len(recommendable) >= 1
        and flexible.recommended_option_ids == tuple(
            item.option_id
            for item in flexible.ranked_options
            if item.date_pair_id in final_recommendable_ids
        )
        and rank_sequence == tuple(range(1, len(rank_sequence) + 1))
        and recommended_pairs_have_icom_4_of_4
        and sampled_boundary_ok
    )
    return LiveDoneGateCheck(
        id="flexible_ranked_options",
        passed=passed,
        summary=(
            "三个精确日期对完成有界查询，并形成一个浏览器 11/11、iCom 4/4 的最终推荐方案"
            if passed
            else "灵活日期查询未形成至少两个浏览器与 iCom 公共证据均完整的推荐方案"
        ),
        evidence={
            "selected_pair_count": len(selected_ids),
            "planned_source_task_count": len(flexible.query_plan.tasks),
            "completed_pair_count": len(completed_ids),
            "recommendable_count": len(recommendable),
            "recommended_option_ids": list(flexible.recommended_option_ids),
            "recommended_pair_public_transfer_checks": public_transfer_pair_checks,
            "recommended_pairs_have_icom_4_of_4": recommended_pairs_have_icom_4_of_4,
            "sampled_not_exhaustive": flexible.sampled_not_exhaustive,
            "sampled_boundary_ok": sampled_boundary_ok,
        },
    )


def _check_strict_coverage(initial: LivePackageAgentRun) -> LiveDoneGateCheck:
    providers = {item.provider for item in initial.coverage}
    complete = {
        item.provider
        for item in initial.coverage
        if item.complete
        and len(item.successful_source_ids) <= _EXPECTED_SOURCE_COUNTS[item.provider]
        and len(item.terminal_outcome_source_ids) == _EXPECTED_SOURCE_COUNTS[item.provider]
        and not item.failed_source_ids
    }
    passed = (
        initial.mode == LiveCoverageMode.STRICT
        and initial.all_platforms_complete
        and providers == _EXPECTED_PROVIDERS
        and complete == _EXPECTED_PROVIDERS
    )
    return LiveDoneGateCheck(
        id="strict_three_platform_coverage",
        passed=passed,
        summary=(
            "携程、去哪儿完成机票与四段住宿，同程完成国际机票查询"
            if passed
            else "未形成严格模式下已启用 Provider 能力的完整覆盖"
        ),
        evidence={
            "mode": initial.mode.value,
            "providers": sorted(item.value for item in providers),
            "complete_providers": sorted(item.value for item in complete),
        },
    )


def _check_source_dag(initial: LivePackageAgentRun) -> LiveDoneGateCheck:
    browser_source_ids = tuple(initial.source_task_ids)
    public_transfer_ids = tuple(initial.public_transfer_task_ids)
    all_source_ids = (*browser_source_ids, *public_transfer_ids)
    graph_ids = {task.id for task in initial.scheduler.graph.tasks}
    expected_lodging_suffixes = (
        "lodging-full",
        "lodging-first",
        "lodging-middle",
        "lodging-last",
        *(
            ("lodging-hulhumale-full",)
            if initial.stay_plan_candidate_set is not None
            else ()
        ),
    )
    expected_browser = {
        f"source-{provider.value}-{suffix}"
        for provider in LIVE_V5_BROWSER_PROVIDERS
        for suffix in (
            "flight",
            *(expected_lodging_suffixes if provider in _LODGING_PROVIDERS else ()),
        )
    }
    expected_browser_count = len(expected_browser)
    coverage = initial.public_transfer_coverage
    public_coverage_complete = (
        coverage is not None
        and coverage.requested
        and coverage.enabled
        and coverage.complete
        and coverage.expected_source_ids == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and coverage.successful_source_ids == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and not coverage.failed_source_ids
        and coverage.usable_option_count >= 4
    )
    passed = (
        len(browser_source_ids) == expected_browser_count
        and set(browser_source_ids) == expected_browser
        and public_transfer_ids == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and len(all_source_ids) == expected_browser_count + len(
            _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        )
        and set(all_source_ids) <= graph_ids
        and public_coverage_complete
        and initial.scheduler.succeeded
        and initial.scheduler.max_parallel_tasks >= len(all_source_ids)
    )
    return LiveDoneGateCheck(
        id="versioned_source_agent_dag",
        passed=passed,
        summary=(
            f"{expected_browser_count} 路浏览器与 4 路 iCom 官方公共读取进入同一 "
            f"{expected_browser_count + 4} 源可追溯 DAG"
            if passed
            else (
                f"{expected_browser_count + 4} 源 DAG、iCom 4/4 覆盖、"
                "调度成功或并发证据不完整"
            )
        ),
        evidence={
            "browser_source_count": len(browser_source_ids),
            "public_transfer_source_count": len(public_transfer_ids),
            "source_count": len(all_source_ids),
            "public_transfer_task_ids": list(public_transfer_ids),
            "public_transfer_coverage_complete": public_coverage_complete,
            "public_transfer_usable_option_count": (
                coverage.usable_option_count if coverage is not None else 0
            ),
            "scheduler_succeeded": initial.scheduler.succeeded,
            "scheduler_max_parallel_tasks": initial.scheduler.max_parallel_tasks,
        },
    )


def _icom_expected_queries(
    initial: LivePackageAgentRun,
) -> dict[str, IComTransferQuery]:
    intent = initial.intent
    return {
        "public-transfer-icom-continuous-outbound": IComTransferQuery(
            travel_date=intent.start_date,
            origin=IComLocation.AIRPORT,
            destination=IComLocation.MAAFUSHI,
            adults=2,
        ),
        "public-transfer-icom-split-outbound": IComTransferQuery(
            travel_date=intent.start_date + timedelta(days=1),
            origin=IComLocation.AIRPORT,
            destination=IComLocation.MAAFUSHI,
            adults=2,
        ),
        "public-transfer-icom-split-inbound": IComTransferQuery(
            travel_date=intent.end_date - timedelta(days=1),
            origin=IComLocation.MAAFUSHI,
            destination=IComLocation.AIRPORT,
            adults=2,
        ),
        "public-transfer-icom-continuous-inbound": IComTransferQuery(
            travel_date=intent.end_date,
            origin=IComLocation.MAAFUSHI,
            destination=IComLocation.AIRPORT,
            adults=2,
        ),
    }


def _icom_results(
    initial: LivePackageAgentRun,
) -> tuple[dict[str, IComTransferSearchResult], list[str]]:
    expected = set(_EXPECTED_PUBLIC_TRANSFER_TASK_IDS)
    parsed: dict[str, IComTransferSearchResult] = {}
    errors: list[str] = []
    for task_result in initial.scheduler.results:
        if task_result.task_id not in expected:
            continue
        raw = task_result.output.get("result")
        if raw is None:
            errors.append(f"{task_result.task_id}: missing IComTransferSearchResult")
            continue
        try:
            parsed[task_result.task_id] = IComTransferSearchResult.model_validate(raw)
        except Exception as exc:
            errors.append(f"{task_result.task_id}: {type(exc).__name__}")
    for task_id in _EXPECTED_PUBLIC_TRANSFER_TASK_IDS:
        if task_id not in parsed and not any(error.startswith(f"{task_id}:") for error in errors):
            errors.append(f"{task_id}: missing task result")
    return parsed, errors


def _fresh_timestamp(
    value: datetime,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> bool:
    normalized = value.astimezone(UTC)
    return normalized <= now and now - normalized <= maximum_quote_age


def _field_value_sha256(value: object) -> str:
    if isinstance(value, datetime):
        normalized: object = value.isoformat()
    elif isinstance(value, Decimal):
        normalized = str(value)
    elif isinstance(value, StrEnum):
        normalized = value.value
    else:
        normalized = value
    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _icom_url_error(url: str, query: IComTransferQuery) -> str | None:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return "invalid port"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != _ICOM_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path not in _ICOM_ALLOWED_PATHS
    ):
        return "outside exact public-read allowlist"
    parameters = parse_qs(parsed.query, keep_blank_values=True)
    if parsed.path == _ICOM_SCHEDULE_PATH:
        expected_date = query.travel_date.isoformat()
        if parameters != {"date": [expected_date]}:
            return "schedule URL does not carry the exact query date"
    elif parameters:
        return "non-schedule public URL unexpectedly has query parameters"
    return None


def _icom_result_errors(
    task_id: str,
    result: IComTransferSearchResult,
    expected_query: IComTransferQuery,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
    require_fresh_evidence: bool = True,
) -> tuple[list[str], int, int]:
    errors: list[str] = []
    if result.query != expected_query:
        errors.append(f"{task_id}: query does not match exact date/direction/2 adults")
    if result.searched_at.astimezone(UTC) > now or (
        require_fresh_evidence
        and not _fresh_timestamp(
            result.searched_at,
            now=now,
            maximum_quote_age=maximum_quote_age,
        )
    ):
        errors.append(f"{task_id}: searched_at outside freshness window")

    if len(result.source_urls) != 3:
        errors.append(f"{task_id}: expected exactly three official source URLs")
    expected_paths = _ICOM_ALLOWED_PATHS
    for index, url in enumerate(result.source_urls):
        if problem := _icom_url_error(url, expected_query):
            errors.append(f"{task_id}: source URL {problem}")
        if index < len(expected_paths) and urlsplit(url).path != expected_paths[index]:
            errors.append(f"{task_id}: source URL order/path contract mismatch")

    response_hashes: defaultdict[str, set[str]] = defaultdict(set)
    usable_count = 0
    field_evidence_count = 0
    for option in result.options:
        if (
            option.origin != expected_query.origin
            or option.destination != expected_query.destination
        ):
            errors.append(f"{task_id}: option direction differs from query")
        if option.departure_at.date() != expected_query.travel_date:
            errors.append(f"{task_id}: option departure date differs from query")
        if option.source_url != result.source_urls[0]:
            errors.append(f"{task_id}: option source is not the schedules response")
        if option.captured_at > result.searched_at:
            errors.append(f"{task_id}: option was captured after searched_at")
        if option.captured_at.astimezone(UTC) > now or (
            require_fresh_evidence
            and not _fresh_timestamp(
                option.captured_at,
                now=now,
                maximum_quote_age=maximum_quote_age,
            )
        ):
            errors.append(f"{task_id}: option captured_at outside freshness window")

        evidence_rows = (*option.evidence, *option.fare.evidence)
        evidence_by_field = {item.normalized_field: item for item in evidence_rows}
        if len(evidence_by_field) != len(evidence_rows):
            errors.append(f"{task_id}: duplicate normalized field evidence")
        fields = set(evidence_by_field)
        missing_fields = _REQUIRED_ICOM_FIELD_EVIDENCE - fields
        if missing_fields:
            errors.append(f"{task_id}: missing field evidence {','.join(sorted(missing_fields))}")
        expected_values = {
            "trip_id": option.trip_id,
            "schedule_id": option.schedule_id,
            "route": option.route,
            "departure_at": option.departure_at,
            "arrival_at": option.arrival_at,
            "remaining_capacity": option.remaining_capacity,
            "is_cancelled": option.is_cancelled,
            "availability_status": option.availability_status,
            "fare.amount": option.fare.amount,
            "fare.currency": option.fare.currency,
            "fare.basis": option.fare.basis,
            "fare.taxes_included": option.fare.taxes_included,
        }
        for field, value in expected_values.items():
            evidence = evidence_by_field.get(field)
            if evidence is not None and evidence.value_sha256 != _field_value_sha256(value):
                errors.append(f"{task_id}: field value SHA mismatch for {field}")
        for item in evidence_rows:
            field_evidence_count += 1
            if item.source_url not in result.source_urls:
                errors.append(f"{task_id}: field evidence URL outside result sources")
            expected_path = (
                _ICOM_BASE_FARE_PATH
                if item.normalized_field.startswith("fare.")
                else _ICOM_SCHEDULE_PATH
            )
            if urlsplit(item.source_url).path != expected_path:
                errors.append(
                    f"{task_id}: field evidence source mismatch for {item.normalized_field}"
                )
            if problem := _icom_url_error(item.source_url, expected_query):
                errors.append(f"{task_id}: field evidence URL {problem}")
            if item.captured_at > result.searched_at:
                errors.append(f"{task_id}: field evidence was captured after searched_at")
            if item.captured_at.astimezone(UTC) > now or (
                require_fresh_evidence
                and not _fresh_timestamp(
                    item.captured_at,
                    now=now,
                    maximum_quote_age=maximum_quote_age,
                )
            ):
                errors.append(f"{task_id}: field evidence outside freshness window")
            response_hashes[item.source_url].add(item.response_sha256)

        policy = option.currency_policy_evidence
        if policy is None:
            errors.append(f"{task_id}: missing official USD currency-policy evidence")
        else:
            normalized_statement = " ".join(policy.statement.split()).casefold()
            if policy.source_url not in result.source_urls:
                errors.append(f"{task_id}: currency-policy URL outside result sources")
            if urlsplit(policy.source_url).path != _ICOM_POLICY_PATH:
                errors.append(f"{task_id}: currency-policy source mismatch")
            if problem := _icom_url_error(policy.source_url, expected_query):
                errors.append(f"{task_id}: currency-policy URL {problem}")
            if (
                "displayed and charged" not in normalized_statement
                or "usd" not in normalized_statement
                or policy.evidence_sha256 != _field_value_sha256(" ".join(policy.statement.split()))
            ):
                errors.append(f"{task_id}: invalid USD currency-policy statement evidence")
            if policy.captured_at > result.searched_at:
                errors.append(f"{task_id}: currency-policy evidence was captured after searched_at")
            if policy.captured_at.astimezone(UTC) > now or (
                require_fresh_evidence
                and not _fresh_timestamp(
                    policy.captured_at,
                    now=now,
                    maximum_quote_age=maximum_quote_age,
                )
            ):
                errors.append(f"{task_id}: currency-policy evidence outside freshness window")
            response_hashes[policy.source_url].add(policy.response_sha256)

        converted = to_package_transfer_option(option, adults=expected_query.adults)
        if converted is not None:
            usable_count += 1
            if (
                converted.service_date != expected_query.travel_date
                or converted.adults != 2
                or converted.price_guarantee != TransferPriceGuarantee.PUBLISHED_BASE_FARE
                or converted.currency != "USD"
                or converted.taxes_and_fees_included is not None
            ):
                errors.append(f"{task_id}: converted option violates package boundary")

    for url in result.source_urls:
        hashes = response_hashes.get(url, set())
        if len(hashes) != 1:
            errors.append(f"{task_id}: source lacks one stable response SHA-256")
    if usable_count < 1:
        errors.append(f"{task_id}: no convertible available schedule")
    return errors, usable_count, field_evidence_count


def _check_icom_public_transfer_evidence(
    initial: LivePackageAgentRun,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
    require_fresh_evidence: bool = True,
) -> LiveDoneGateCheck:
    parsed, errors = _icom_results(initial)
    expected_queries = _icom_expected_queries(initial)
    usable_by_task: dict[str, int] = {}
    field_evidence_by_task: dict[str, int] = {}
    if initial.intent.adults != 2:
        errors.append("package intent must contain exactly two adults")
    for task_id in _EXPECTED_PUBLIC_TRANSFER_TASK_IDS:
        result = parsed.get(task_id)
        if result is None:
            continue
        task_errors, usable_count, field_count = _icom_result_errors(
            task_id,
            result,
            expected_queries[task_id],
            now=now,
            maximum_quote_age=maximum_quote_age,
            require_fresh_evidence=require_fresh_evidence,
        )
        errors.extend(task_errors)
        usable_by_task[task_id] = usable_count
        field_evidence_by_task[task_id] = field_count
    coverage = initial.public_transfer_coverage
    coverage_ok = (
        coverage is not None
        and coverage.requested
        and coverage.enabled
        and coverage.complete
        and coverage.expected_source_ids == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and coverage.successful_source_ids == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and not coverage.failed_source_ids
        and coverage.usable_option_count >= 4
    )
    if not coverage_ok:
        errors.append("public transfer coverage is not exact 4/4")
    passed = (
        tuple(initial.public_transfer_task_ids) == _EXPECTED_PUBLIC_TRANSFER_TASK_IDS
        and len(parsed) == 4
        and len(usable_by_task) == 4
        and all(count >= 1 for count in usable_by_task.values())
        and not errors
    )
    return LiveDoneGateCheck(
        id="icom_public_transfer_evidence",
        passed=passed,
        summary=(
            "4 路 iCom 官方公共读取均匹配精确日期、方向、2 成人并含新鲜字段级哈希证据"
            if passed
            else "iCom 4/4 覆盖、查询合同、官方 URL、字段哈希、新鲜度或可用班次证据不完整"
        ),
        evidence={
            "parsed_task_count": len(parsed),
            "coverage_4_of_4": coverage_ok,
            "usable_options_by_task": usable_by_task,
            "field_evidence_rows_by_task": field_evidence_by_task,
            "errors": errors,
        },
    )


def _source_snapshots(
    initial: LivePackageAgentRun,
) -> tuple[tuple[BrowserTaskSnapshot, ...], tuple[str, ...]]:
    expected = set(initial.source_task_ids)
    snapshots: list[BrowserTaskSnapshot] = []
    errors: list[str] = []
    for result in initial.scheduler.results:
        if result.task_id not in expected:
            continue
        raw = result.output.get("snapshot")
        if raw is None:
            errors.append(f"{result.task_id}: missing snapshot")
            continue
        try:
            snapshots.append(BrowserTaskSnapshot.model_validate(raw))
        except Exception as exc:
            errors.append(f"{result.task_id}: {type(exc).__name__}")
    missing = expected - {result.task_id for result in initial.scheduler.results}
    errors.extend(f"{task_id}: missing task result" for task_id in sorted(missing))
    return tuple(snapshots), tuple(errors)


def _event_source_snapshots(
    event: LiveEventReplanRun,
) -> tuple[tuple[BrowserTaskSnapshot, ...], tuple[str, ...]]:
    snapshots: list[BrowserTaskSnapshot] = []
    errors: list[str] = []
    expected = set(event.source_task_ids)
    for result in event.scheduler.results:
        if result.task_id not in expected:
            continue
        raw = result.output.get("snapshot")
        if raw is None:
            errors.append(f"{result.task_id}: missing snapshot")
            continue
        try:
            snapshots.append(BrowserTaskSnapshot.model_validate(raw))
        except Exception as exc:
            errors.append(f"{result.task_id}: {type(exc).__name__}")
    return tuple(snapshots), tuple(errors)


def _flight_quote_reference(quote: BrowserQuote) -> str:
    return f"browser:{quote.provider.value}:sha256:{quote.evidence_sha256}"


def _selected_raw_flight_quotes(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    snapshots: tuple[BrowserTaskSnapshot, ...],
    event_snapshots: tuple[BrowserTaskSnapshot, ...],
) -> tuple[dict[str, BrowserQuote], tuple[str, ...]]:
    quote_by_reference = {
        _flight_quote_reference(quote): quote
        for snapshot in (*snapshots, *event_snapshots)
        for quote in snapshot.quotes
        if quote.kind == BrowserVertical.FLIGHT
    }
    selected: dict[str, BrowserQuote] = {}
    errors: list[str] = []
    for label, package in (
        ("initial", initial.package),
        ("event", event.package),
    ):
        if package is None:
            errors.append(f"{label}: missing final package")
            continue
        flight = package.final_candidate.flight
        matching_references = tuple(
            reference for reference in flight.evidence_refs if reference in quote_by_reference
        )
        if len(matching_references) != 1:
            errors.append(f"{label}: selected flight must resolve to exactly one browser quote")
            continue
        quote = quote_by_reference[matching_references[0]]
        if quote.provider.value != flight.provider:
            errors.append(f"{label}: selected provider differs from raw browser quote")
            continue
        selected[label] = quote
    return selected, tuple(errors)


def _round_trip_quote_errors(quote: BrowserQuote) -> tuple[str, ...]:
    errors: list[str] = []
    details = quote.details
    expected_workflow = {
        BrowserProvider.CTRIP: "staged_outbound_return",
        BrowserProvider.FLIGGY: "staged_outbound_return",
        BrowserProvider.QUNAR: "combined_roundtrip_card",
        BrowserProvider.TONGCHENG: "staged_outbound_return",
    }[quote.provider]
    expected_party_status = {
        BrowserProvider.CTRIP: "confirmed_for_party",
        BrowserProvider.FLIGGY: "comparison_only",
        BrowserProvider.QUNAR: "confirmed_for_party",
        BrowserProvider.TONGCHENG: "confirmed_for_party",
    }[quote.provider]
    expected_fields = {
        "workflow_kind": expected_workflow,
        "combination_status": "round_trip_complete",
        "journey_price_scope": "round_trip",
        "price_finality": "final_for_combination",
        "party_availability_status": expected_party_status,
    }
    for field, expected in expected_fields.items():
        if details.get(field) != expected:
            errors.append(f"{field} must be {expected}")
    if quote.provider == BrowserProvider.QUNAR:
        comparison = details.get("party_price_comparison")
        if not isinstance(comparison, dict) or comparison.get(
            "schema"
        ) != "tripchord.flight_party_comparison.v1":
            errors.append(
                "Qunar flight requires a server-verified same-product one/two-adult comparison"
            )
    for field in (
        "combination_id",
        "price_basis_evidence",
        "tax_evidence",
        "selection_evidence",
    ):
        value = details.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{field} missing")
    if quote.taxes_included is not True:
        errors.append("taxes_included is not confirmed")

    timestamps: dict[str, datetime] = {}
    for field in (
        "outbound_departure_at",
        "outbound_arrival_at",
        "return_departure_at",
        "return_arrival_at",
    ):
        raw = details.get(field)
        if not isinstance(raw, str) or not raw.strip():
            errors.append(f"{field} missing")
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            errors.append(f"{field} is not ISO-8601")
            continue
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            errors.append(f"{field} has no timezone")
            continue
        timestamps[field] = parsed
    if len(timestamps) == 4:
        if timestamps["outbound_arrival_at"] <= timestamps["outbound_departure_at"]:
            errors.append("outbound interval is invalid")
        if timestamps["return_departure_at"] <= timestamps["outbound_arrival_at"]:
            errors.append("return does not follow outbound")
        if timestamps["return_arrival_at"] <= timestamps["return_departure_at"]:
            errors.append("return interval is invalid")
    return tuple(errors)


def _selected_flight_consistency_errors(
    selected: NormalizedFlightQuote,
    quote: BrowserQuote,
) -> tuple[str, ...]:
    errors: list[str] = []
    if selected.provider != quote.provider.value:
        errors.append("selected provider differs from raw quote")
    if selected.currency != quote.currency:
        errors.append("selected currency differs from raw quote")
    if selected.adults != quote.details.get("adults"):
        errors.append("selected adult count differs from raw quote")
    if selected.origin != quote.details.get("origin"):
        errors.append("selected origin differs from raw quote")
    if selected.destination != quote.details.get("destination"):
        errors.append("selected destination differs from raw quote")

    raw_times: dict[str, datetime] = {}
    for raw_field in (
        "outbound_departure_at",
        "outbound_arrival_at",
        "return_departure_at",
        "return_arrival_at",
    ):
        raw = quote.details.get(raw_field)
        if not isinstance(raw, str):
            continue
        try:
            raw_times[raw_field] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
    expected_times = {
        "outbound_departure_at": selected.outbound_depart_at,
        "outbound_arrival_at": selected.outbound_arrive_at,
        "return_departure_at": selected.return_depart_at,
        "return_arrival_at": selected.return_arrive_at,
    }
    for field, expected in expected_times.items():
        if raw_times.get(field) != expected:
            errors.append(f"selected {field} differs from raw quote")

    raw_party_confirmed = quote.details.get("party_availability_status") == "confirmed_for_party"
    if selected.party_availability_confirmed is not raw_party_confirmed:
        errors.append("selected party availability differs from raw quote")

    multiplier = selected.adults if quote.price_basis.value == "per_person" else 1
    raw_total = quote.amount * Decimal(multiplier) * Decimal(100)
    if raw_total != raw_total.to_integral_value():
        errors.append("raw quote total is not an integral number of cents")
    elif selected.total_for_party_cents != int(raw_total):
        errors.append("selected total differs from raw quote")
    return tuple(errors)


def _browser_action_trace_errors(quote: BrowserQuote) -> tuple[str, ...]:
    trace = quote.details.get("action_trace")
    if not isinstance(trace, list) or not trace or len(trace) > 8:
        return ("action_trace missing or outside one-to-eight action bound",)
    actions: list[str] = []
    errors: list[str] = []
    for index, entry in enumerate(trace):
        if not isinstance(entry, dict):
            errors.append(f"action_trace[{index}] is not an object")
            continue
        action = entry.get("action")
        if not isinstance(action, str) or action not in _ALLOWED_BROWSER_TRACE_ACTIONS:
            errors.append(f"action_trace[{index}] is outside read-only allowlist")
            continue
        serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True).lower()
        if any(marker in serialized for marker in _FORBIDDEN_ACTION_MARKERS):
            errors.append(f"action_trace[{index}] contains transaction/account marker")
        actions.append(action)
    if not actions or actions[0] != "search":
        errors.append("action_trace does not begin with search")
    workflow = quote.details.get("workflow_kind")
    if workflow == "staged_outbound_return" and not any(
        action in {"select_outbound", "provider_auto_selected_outbound"} for action in actions
    ):
        errors.append("staged workflow has no outbound selection")
    if workflow == "combined_roundtrip_card" and any(
        action
        in {
            "select_outbound",
            "reselect_outbound",
            "provider_auto_selected_outbound",
        }
        for action in actions
    ):
        errors.append("combined card workflow attempted outbound selection")
    return tuple(errors)


def _check_round_trip_combination_evidence(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    snapshots: tuple[BrowserTaskSnapshot, ...],
    event_snapshots: tuple[BrowserTaskSnapshot, ...],
) -> LiveDoneGateCheck:
    selected, resolution_errors = _selected_raw_flight_quotes(
        initial,
        event,
        snapshots,
        event_snapshots,
    )
    errors = list(resolution_errors)
    combinations: dict[str, JsonValue] = {}
    for label, quote in selected.items():
        quote_errors = _round_trip_quote_errors(quote)
        errors.extend(f"{label}: {error}" for error in quote_errors)
        package = initial.package if label == "initial" else event.package
        if package is not None:
            consistency_errors = _selected_flight_consistency_errors(
                package.final_candidate.flight,
                quote,
            )
            errors.extend(f"{label}: {error}" for error in consistency_errors)
        combinations[label] = cast(
            JsonValue,
            {
                "provider": quote.provider.value,
                "combination_id": quote.details.get("combination_id"),
                "workflow_kind": quote.details.get("workflow_kind"),
                "evidence_ref": _flight_quote_reference(quote),
                "selected_total_for_party_cents": (
                    package.final_candidate.flight.total_for_party_cents
                    if package is not None
                    else None
                ),
            },
        )
    passed = set(selected) == {"initial", "event"} and not errors
    return LiveDoneGateCheck(
        id="round_trip_combination_evidence",
        passed=passed,
        summary=(
            "初始与事件最终方案的机票均绑定四个带时区时刻和同一笔最终往返组合价"
            if passed
            else "初始或事件最终方案缺少可回溯的完整往返组合证据"
        ),
        evidence={
            "selected_combinations": combinations,
            "errors": errors,
        },
    )


def _check_browser_action_trace_read_only(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    snapshots: tuple[BrowserTaskSnapshot, ...],
    event_snapshots: tuple[BrowserTaskSnapshot, ...],
) -> LiveDoneGateCheck:
    selected, resolution_errors = _selected_raw_flight_quotes(
        initial,
        event,
        snapshots,
        event_snapshots,
    )
    errors = list(resolution_errors)
    action_sequences: dict[str, JsonValue] = {}
    for label, quote in selected.items():
        quote_errors = _browser_action_trace_errors(quote)
        errors.extend(f"{label}: {error}" for error in quote_errors)
        trace = quote.details.get("action_trace")
        action_sequences[label] = cast(
            JsonValue,
            [
                entry.get("action")
                for entry in trace
                if isinstance(entry, dict) and isinstance(entry.get("action"), str)
            ]
            if isinstance(trace, list)
            else [],
        )
    passed = set(selected) == {"initial", "event"} and not errors
    return LiveDoneGateCheck(
        id="browser_action_trace_read_only",
        passed=passed,
        summary=(
            "初始与事件最终机票仅执行搜索、筛选及去程选择类只读动作"
            if passed
            else "初始或事件最终机票动作链缺失、越权或含交易/账号动作"
        ),
        evidence={
            "selected_action_sequences": action_sequences,
            "allowed_actions": sorted(_ALLOWED_BROWSER_TRACE_ACTIONS),
            "errors": errors,
        },
    )


def _check_real_browser_evidence(
    snapshots: tuple[BrowserTaskSnapshot, ...],
    errors: tuple[str, ...],
    *,
    now: datetime,
    maximum_quote_age: timedelta,
    check_id: str,
) -> LiveDoneGateCheck:
    reasons = list(errors)
    quote_count = 0
    flight_quote_count = 0
    terminal_receipt_count = 0
    parser_versions: set[str] = set()
    for snapshot in snapshots:
        terminal_receipt = _verified_browser_terminal_receipt(snapshot)
        if terminal_receipt:
            terminal_receipt_count += 1
        elif snapshot.state != BrowserTaskState.SUCCEEDED:
            reasons.append(f"{snapshot.id}: {snapshot.state.value}")
        if snapshot.claimed_by is None or snapshot.claimed_at is None:
            reasons.append(f"{snapshot.id}: missing runtime claim evidence")
        for quote in snapshot.quotes:
            quote_count += 1
            parser_versions.add(quote.parser_version)
            if hashlib.sha256(quote.visible_evidence.encode()).hexdigest() != quote.evidence_sha256:
                reasons.append(f"{snapshot.id}: visible evidence SHA mismatch")
            lowered = quote.parser_version.lower()
            if any(marker in lowered for marker in _FORBIDDEN_PARSER_MARKERS):
                reasons.append(f"{snapshot.id}: non-live parser {quote.parser_version}")
            if quote.parser_version != PRODUCTION_VISIBLE_DOM_PARSER_VERSION:
                reasons.append(f"{snapshot.id}: unsupported parser {quote.parser_version}")
            if quote.kind == BrowserVertical.FLIGHT:
                flight_quote_count += 1
                reasons.extend(
                    f"{snapshot.id}: {error}"
                    for error in (
                        *_round_trip_quote_errors(quote),
                        *_browser_action_trace_errors(quote),
                    )
                )
            if quote.captured_at > now or now - quote.captured_at > maximum_quote_age:
                reasons.append(f"{snapshot.id}: quote outside freshness window")
            driver = quote.details.get("driver")
            if not isinstance(driver, dict):
                reasons.append(f"{snapshot.id}: missing driver evidence")
                continue
            scope = driver.get("confirmation_scope")
            confirmed = driver.get("confirmed_query")
            if driver.get("triggered") is not True:
                reasons.append(f"{snapshot.id}: search was not visibly triggered")
            if not isinstance(confirmed, dict) or not confirmed:
                reasons.append(f"{snapshot.id}: submitted query was not visibly confirmed")
            if not isinstance(scope, str) or scope in _UNCONFIRMED_DRIVER_SCOPES:
                reasons.append(f"{snapshot.id}: unconfirmed driver scope")
    passed = (
        bool(snapshots)
        and quote_count > 0
        and quote_count + terminal_receipt_count >= len(snapshots)
        and not reasons
    )
    return LiveDoneGateCheck(
        id=check_id,
        passed=passed,
        summary=(
            "每个源任务都有新鲜可见报价或签名的精确查询终态"
            if passed
            else "真实浏览器报价、查询确认或新鲜度证据不完整"
        ),
        evidence={
            "snapshot_count": len(snapshots),
            "quote_count": quote_count,
            "flight_quote_count": flight_quote_count,
            "terminal_receipt_count": terminal_receipt_count,
            "parser_versions": sorted(parser_versions),
            "errors": reasons,
        },
    )


def _verified_browser_terminal_receipt(snapshot: BrowserTaskSnapshot) -> bool:
    failure = snapshot.failure
    if snapshot.state != BrowserTaskState.FAILED or failure is None:
        return False
    details = failure.details
    if snapshot.kind == BrowserVertical.FLIGHT:
        raw = details.get("flight_search_receipt")
        sealed = details.get("flight_search_receipt_sha256")
        if not isinstance(raw, dict) or not isinstance(sealed, str):
            return False
        try:
            flight_receipt = FlightSearchReceipt.model_validate(raw)
        except ValueError:
            return False
        flight_confirmed = flight_receipt.confirmed_query
        query = snapshot.query
        return (
            flight_search_receipt_sha256(raw) == sealed
            and flight_receipt.provider == snapshot.provider
            and query.origin is not None
            and query.origin_code is not None
            and query.destination_code is not None
            and query.end_date is not None
            and flight_confirmed.origin == query.origin
            and flight_confirmed.destination == query.destination
            and flight_confirmed.origin_code == query.origin_code
            and flight_confirmed.destination_code == query.destination_code
            and flight_confirmed.start_date == query.start_date
            and flight_confirmed.end_date == query.end_date
            and flight_confirmed.adults == query.adults
            and flight_receipt.page_url == failure.page_url
            and flight_receipt.captured_at == failure.captured_at
        )
    if snapshot.kind != BrowserVertical.LODGING:
        return False
    raw = details.get("inventory_receipt")
    sealed = details.get("inventory_receipt_sha256")
    if not isinstance(raw, dict) or not isinstance(sealed, str):
        return False
    try:
        lodging_receipt = LodgingInventoryReceipt.model_validate(raw)
    except ValueError:
        return False
    expected_options = {
        key: snapshot.query.options.get(key)
        for key in (
            "expected_lodging_place_key",
            "expected_package_area",
            "segment",
        )
    }
    lodging_confirmed = lodging_receipt.confirmed_query
    return (
        all(isinstance(value, str) and value for value in expected_options.values())
        and lodging_inventory_receipt_sha256(raw) == sealed
        and lodging_receipt.provider == snapshot.provider
        and lodging_confirmed.destination == snapshot.query.destination
        and lodging_confirmed.start_date == snapshot.query.start_date
        and lodging_confirmed.end_date == snapshot.query.end_date
        and lodging_confirmed.adults == snapshot.query.adults
        and lodging_confirmed.rooms == snapshot.query.rooms
        and lodging_confirmed.options == expected_options
        and lodging_receipt.page_url == failure.page_url
        and lodging_receipt.captured_at == failure.captured_at
    )


def _check_actual_overlap(
    snapshots: tuple[BrowserTaskSnapshot, ...],
) -> LiveDoneGateCheck:
    intervals = tuple(
        (snapshot.claimed_at, snapshot.updated_at, snapshot.provider)
        for snapshot in snapshots
        if snapshot.claimed_at is not None and snapshot.updated_at >= snapshot.claimed_at
    )
    max_overlap = 0
    max_provider_overlap = 0
    for start, _, _ in intervals:
        active = tuple(
            provider
            for other_start, other_end, provider in intervals
            if other_start <= start <= other_end
        )
        max_overlap = max(max_overlap, len(active))
        max_provider_overlap = max(max_provider_overlap, len(set(active)))
    passed = max_overlap >= 3 and max_provider_overlap == 3
    return LiveDoneGateCheck(
        id="observed_cross_platform_overlap",
        passed=passed,
        summary=(
            "运行时间区间证明三平台查询存在真实重叠"
            if passed
            else "只有调度配置，未证明三平台浏览器任务实际并发重叠"
        ),
        evidence={
            "interval_count": len(intervals),
            "max_overlapping_tasks": max_overlap,
            "max_overlapping_providers": max_provider_overlap,
        },
    )


def _check_read_only_graph(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
) -> LiveDoneGateCheck:
    violations: list[str] = []
    initial_tasks_by_id = {task.id: task for task in initial.scheduler.graph.tasks}
    graph_tasks = (
        *((task, "initial") for task in initial.scheduler.graph.tasks),
        *((task, "event") for task in event.scheduler.graph.tasks),
    )
    event_tool = (
        _ICOM_SEARCH_TOOL
        if event.requeried_providers == (LiveDataProvider.ICOM_PUBLIC_TRANSFER,)
        else _BROWSER_SEARCH_TOOL
    )
    for task, graph_name in graph_tasks:
        combined = f"{task.id} {task.goal} {' '.join(task.allowed_tools)}".lower()
        if any(marker in combined for marker in _FORBIDDEN_ACTION_MARKERS):
            violations.append(f"{graph_name}:{task.id}: transaction marker")
        expected_tool: str | None = None
        publication_auxiliary = graph_name == "initial" and task.id.startswith(
            ("publication-retry-source-", "publication-failover-source-")
        )
        if publication_auxiliary:
            if _is_declared_publication_auxiliary_source(
                task,
                initial=initial,
                tasks_by_id=initial_tasks_by_id,
            ):
                expected_tool = _BROWSER_SEARCH_TOOL
        elif graph_name == "initial" and task.id in initial.source_task_ids:
            expected_tool = _BROWSER_SEARCH_TOOL
        elif graph_name == "initial" and task.id in initial.public_transfer_task_ids:
            expected_tool = _ICOM_SEARCH_TOOL
        elif graph_name == "event" and task.id in event.source_task_ids:
            expected_tool = event_tool
        if expected_tool is not None and tuple(task.allowed_tools) != (expected_tool,):
            violations.append(f"{graph_name}:{task.id}: unexpected tool scope")
        elif expected_tool is None and task.allowed_tools:
            undeclared = set(task.allowed_tools) - _MODEL_READ_ONLY_TOOLS
            if undeclared:
                violations.append(
                    f"{graph_name}:{task.id}: non-source tool outside internal read-only set"
                )
    passed = not violations
    return LiveDoneGateCheck(
        id="read_only_action_surface",
        passed=passed,
        summary=(
            "Source worker 仅持有外部只读工具，模型 Agent 仅持有内部检查工具"
            if passed
            else "DAG 中发现交易词或超出只读搜索边界的 Source 工具"
        ),
        evidence={
            "allowed_source_tools": [_BROWSER_SEARCH_TOOL, _ICOM_SEARCH_TOOL],
            "allowed_model_tools": sorted(_MODEL_READ_ONLY_TOOLS),
            "violations": violations,
        },
    )


def _is_declared_publication_auxiliary_source(
    task: AgentTask,
    *,
    initial: LivePackageAgentRun,
    tasks_by_id: dict[str, AgentTask],
) -> bool:
    """Recognize bounded publication retry/failover workers from graph metadata.

    Publication graphs contain dormant retry/failover workers even when their
    observations are not selected into ``source_task_ids``.  Tool-surface
    auditing must therefore use their frozen graph contract, not whether their
    evidence won.  Prefixes alone are deliberately insufficient: a forged task
    must also bind to a real primary query or a fully declared flight failover.
    """

    try:
        submission = BrowserTaskSubmission.model_validate(task.input.get("submission"))
    except (TypeError, ValueError):
        return False
    if task.dependencies != ("normalize-publication-primary",):
        return False

    if task.id.startswith("publication-retry-source-"):
        retry_of = task.input.get("publication_retry_of")
        if not isinstance(retry_of, str) or retry_of not in initial.source_task_ids:
            return False
        if not retry_of.startswith("publication-source-"):
            return False
        expected_id = retry_of.replace(
            "publication-source-",
            "publication-retry-source-",
            1,
        )
        if task.id != expected_id:
            return False
        if task.input.get("publication_retry_vertical") != submission.kind.value:
            return False
        primary = tasks_by_id.get(retry_of)
        if primary is None or primary.allowed_tools != (_BROWSER_SEARCH_TOOL,):
            return False
        try:
            primary_submission = BrowserTaskSubmission.model_validate(
                primary.input.get("submission")
            )
        except (TypeError, ValueError):
            return False
        return primary_submission == submission

    if not task.id.startswith("publication-failover-source-"):
        return False
    if submission.kind != BrowserVertical.FLIGHT:
        return False
    if task.input.get("publication_failover_vertical") != BrowserVertical.FLIGHT.value:
        return False
    if task.id != f"publication-failover-source-{submission.provider.value}-flight":
        return False
    raw_from_provider = task.input.get("publication_failover_from_provider")
    raw_seed_quote_id = task.input.get("publication_failover_seed_quote_id")
    try:
        from_provider = BrowserProvider(str(raw_from_provider))
    except ValueError:
        return False
    return (
        from_provider != submission.provider
        and isinstance(raw_seed_quote_id, str)
        and bool(raw_seed_quote_id.strip())
    )


def _check_planner_verifier_repair(initial: LivePackageAgentRun) -> LiveDoneGateCheck:
    expected_dependencies = {
        "plan-travel-package": ("normalize-browser-quotes",),
        "prepare-candidate-decision-frontier": ("plan-travel-package",),
        "analyze-live-evidence": ("prepare-candidate-decision-frontier",),
        "curate-travel-candidates": ("analyze-live-evidence",),
        "verify-travel-package": ("curate-travel-candidates",),
        "criticize-travel-package": ("verify-travel-package",),
        "strategize-package-repair": ("criticize-travel-package",),
        "repair-travel-package": ("strategize-package-repair",),
        "reverify-travel-package": ("repair-travel-package",),
        "recriticize-repaired-package": ("reverify-travel-package",),
        "recommend-final-decision": ("recriticize-repaired-package",),
        "orchestrate-travel-package": ("recommend-final-decision",),
        "explain-final-decision": ("orchestrate-travel-package",),
        "curate-run-memory": ("explain-final-decision",),
        "publish-live-run": ("curate-run-memory",),
    }
    graph_tasks = {task.id: task for task in initial.scheduler.graph.tasks}
    reverify_node_present = "reverify-travel-package" in graph_tasks
    graph_chain_ok = all(
        task_id in graph_tasks and graph_tasks[task_id].dependencies == expected
        for task_id, expected in expected_dependencies.items()
    )
    package = initial.package
    result_by_task = {result.task_id: result for result in initial.scheduler.results}
    recritic_stage_completed = bool(
        result_by_task.get("recriticize-repaired-package") is not None
        and result_by_task["recriticize-repaired-package"].success
    )
    planning_handoff_present = False
    stage_handoffs_match = False
    stage_handoff_errors: list[str] = []
    identity_chain_ok = False
    reason_chain_ok = False
    planner_candidate_id: str | None = None
    initial_verifier_candidate_id: str | None = None
    repaired_candidate_id: str | None = None
    reverified_candidate_id: str | None = None
    rejection_error_codes: list[str] = []
    independent_audit = initial.package_reverification_audit
    independent_audit_present = independent_audit is not None
    independent_audit_passed = bool(independent_audit is not None and independent_audit.passed)
    independent_audit_stage_matches = False
    independent_failed_codes = (
        [item.value for item in independent_audit.failed_codes]
        if independent_audit is not None
        else []
    )
    if package is None:
        passed = False
        states: list[str] = []
        violation_codes: list[str] = []
        changed = False
    else:
        states = [decision.state.value for decision in package.decisions]
        violation_codes = [violation.code.value for violation in package.initial_violations]
        changed = package.diff is not None and package.diff.changed
        handoff = package.planning_handoff
        planning_handoff_present = handoff is not None
        if handoff is not None:
            selected = handoff.planner.selected_candidate
            repaired = handoff.repair.outcome.candidate
            reverification = handoff.reverification
            planner_candidate_id = (
                selected.id if selected is not None else handoff.planner.selected_candidate_id
            )
            initial_verifier_candidate_id = handoff.initial_verification.candidate_id
            repaired_candidate_id = repaired.id if repaired is not None else None
            reverified_candidate_id = (
                reverification.candidate_id if reverification is not None else None
            )
            expected_error_codes = tuple(
                violation.code for violation in handoff.initial_verification.errors
            )
            rejection_error_codes = [code.value for code in handoff.repair.rejection_error_codes]
            reason_chain_ok = (
                handoff.repair.rejection_error_codes == expected_error_codes
                and handoff.repair.attempted == bool(expected_error_codes)
                and handoff.repair.rejected_candidate_id
                == handoff.initial_verification.candidate_id
            )
            identity_chain_ok = (
                selected == package.initial_candidate
                and handoff.initial_verification.phase == PackageVerificationPhase.INITIAL
                and handoff.initial_verification.matches(package.initial_candidate)
                and handoff.initial_verification.violations == package.initial_violations
                and repaired == package.final_candidate
                and handoff.repair.outcome.diff == package.diff
                and reverification is not None
                and reverification.phase == PackageVerificationPhase.REVERIFICATION
                and reverification.matches(package.final_candidate)
                and reverification.violations == package.final_violations
                and not reverification.errors
            )

            stage_results = {result.task_id: result for result in initial.scheduler.results}
            stage_ids = (
                "plan-travel-package",
                "verify-travel-package",
                "repair-travel-package",
                "reverify-travel-package",
                "orchestrate-travel-package",
            )
            missing_or_failed = [
                task_id
                for task_id in stage_ids
                if task_id not in stage_results
                or not stage_results[task_id].success
                or stage_results[task_id].output.get("handoff") is None
            ]
            if missing_or_failed:
                stage_handoff_errors.extend(
                    f"{task_id}: missing, failed, or empty handoff" for task_id in missing_or_failed
                )
            else:
                try:
                    planner_stage = PackagePlannerHandoff.model_validate(
                        stage_results["plan-travel-package"].output["handoff"]
                    )
                    initial_verifier_stage = PackageVerificationHandoff.model_validate(
                        stage_results["verify-travel-package"].output["handoff"]
                    )
                    repair_stage = PackageRepairHandoff.model_validate(
                        stage_results["repair-travel-package"].output["handoff"]
                    )
                    reverifier_stage = PackageVerificationHandoff.model_validate(
                        stage_results["reverify-travel-package"].output["handoff"]
                    )
                    reverifier_audit_stage = PackageReverificationReport.model_validate(
                        stage_results["reverify-travel-package"].output[
                            "independent_invariant_audit"
                        ]
                    )
                    orchestrator_stage = PackagePlanningHandoff.model_validate(
                        stage_results["orchestrate-travel-package"].output["handoff"]
                    )
                    independent_audit_stage_matches = (
                        independent_audit is not None
                        and reverifier_audit_stage == independent_audit
                        and stage_results["reverify-travel-package"].output.get(
                            "independent_engine"
                        )
                        == independent_audit.engine
                        and stage_results["reverify-travel-package"].output.get(
                            "independent_audit_passed"
                        )
                        is independent_audit.passed
                    )
                    stage_handoffs_match = (
                        planner_stage == handoff.planner
                        and initial_verifier_stage == handoff.initial_verification
                        and repair_stage == handoff.repair
                        and reverifier_stage == handoff.reverification
                        and orchestrator_stage == handoff
                        and independent_audit_stage_matches
                    )
                    if not stage_handoffs_match:
                        stage_handoff_errors.append(
                            "task outputs do not match the final planning handoff"
                        )
                except Exception as exc:
                    stage_handoff_errors.append(f"handoff parse failed: {type(exc).__name__}")
        repaired_after_rejection = (
            states[:2]
            == [
                PackageDecisionState.REJECT_AND_REPLAN.value,
                PackageDecisionState.ACCEPT.value,
            ]
            and bool(package.initial_violations)
            and changed
        )
        clean_noop_repair = (
            states == [PackageDecisionState.ACCEPT.value]
            and not changed
            and not [
                violation
                for violation in package.initial_violations
                if violation.severity.value == "error"
            ]
        )
        soft_risk_repair = bool(
            handoff is not None
            and states == [PackageDecisionState.ACCEPT.value]
            and changed
            and handoff.repair.agent_strategy_applied
            and not handoff.initial_verification.errors
        )
        passed = (
            graph_chain_ok
            and recritic_stage_completed
            and planning_handoff_present
            and identity_chain_ok
            and reason_chain_ok
            and stage_handoffs_match
            and independent_audit_present
            and independent_audit_passed
            and independent_audit_stage_matches
            and (repaired_after_rejection or soft_risk_repair or clean_noop_repair)
            and not [
                violation
                for violation in package.final_violations
                if violation.severity.value == "error"
            ]
            and package.final_decision.state == PackageDecisionState.ACCEPT
            and initial.decision.state == PackageDecisionState.ACCEPT
        )
    return LiveDoneGateCheck(
        id="planner_verifier_repair_orchestrator",
        passed=passed,
        summary=(
            "Planner 初案经 Verifier、Repair、独立 ReVerifier 与 ReCritic 完整交接后由主控接受"
            if passed
            else "独立 Planner–Verifier–Repair–ReVerifier 交接链或主控裁决证据不完整"
        ),
        evidence={
            "graph_chain_ok": graph_chain_ok,
            "reverify_node_present": reverify_node_present,
            "recritic_stage_completed": recritic_stage_completed,
            "planning_handoff_present": planning_handoff_present,
            "stage_handoffs_match": stage_handoffs_match,
            "stage_handoff_errors": stage_handoff_errors,
            "identity_chain_ok": identity_chain_ok,
            "reason_chain_ok": reason_chain_ok,
            "independent_audit_present": independent_audit_present,
            "independent_audit_passed": independent_audit_passed,
            "independent_audit_stage_matches": independent_audit_stage_matches,
            "independent_audit_engine": (
                independent_audit.engine if independent_audit is not None else None
            ),
            "independent_check_count": (
                len(independent_audit.checks) if independent_audit is not None else 0
            ),
            "independent_failed_codes": independent_failed_codes,
            "planner_candidate_id": planner_candidate_id,
            "initial_verifier_candidate_id": initial_verifier_candidate_id,
            "repaired_candidate_id": repaired_candidate_id,
            "reverified_candidate_id": reverified_candidate_id,
            "rejection_error_codes": rejection_error_codes,
            "decision_states": states,
            "initial_violation_codes": violation_codes,
            "diff_changed": changed,
            "repair_execution_mode": (
                "repaired_after_rejection"
                if package is not None and repaired_after_rejection
                else "agent_soft_risk_switch"
                if package is not None and soft_risk_repair
                else "verified_noop"
                if package is not None and clean_noop_repair
                else "incomplete"
            ),
        },
    )


def _check_budget_and_evidence(
    initial: LivePackageAgentRun,
    *,
    now: datetime,
) -> LiveDoneGateCheck:
    package = initial.package
    selected_icom_count = 0
    supplemental_usd_cents = 0
    published_base_fare_boundary_ok = False
    if package is None:
        passed = False
        expected_total = None
        declared_total = None
        evidence_count = 0
    else:
        candidate = package.final_candidate
        recomputed_budget = package_budget(candidate)
        expected_total = candidate.computed_total_cents
        declared_total = package.budget.total_cents
        evidence_count = len(package.evidence_refs)
        selected_quotes = (
            candidate.flight,
            *candidate.lodgings,
            *candidate.transfers,
        )
        selected_icom = tuple(
            transfer
            for transfer in candidate.transfers
            if transfer.provider == "icom-public-transfer"
        )
        selected_icom_count = len(selected_icom)
        supplemental = package.budget.supplemental_published_base_fares
        if not selected_icom:
            published_base_fare_boundary_ok = True
        else:
            selected_contract_ids = {transfer.price_contract_id for transfer in selected_icom}
            selected_transfer_ids = {transfer.id for transfer in selected_icom}
            supplemental_contract_ids = {
                contract_id for item in supplemental for contract_id in item.price_contract_ids
            }
            supplemental_transfer_ids = {
                transfer_id for item in supplemental for transfer_id in item.transfer_ids
            }
            supplemental_usd_cents = sum(
                item.total_for_party_cents for item in supplemental if item.currency == "USD"
            )
            violation_codes = {violation.code.value for violation in package.final_violations}
            required_violation_codes = {"published_base_fare_not_all_in"}
            if initial.intent.budget_cents is not None:
                required_violation_codes.add("budget_not_fully_verified")
            formula = package.budget.formula
            published_base_fare_boundary_ok = (
                package.budget == recomputed_budget
                and candidate.currency == "CNY"
                and all(
                    transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                    and transfer.currency == "USD"
                    and transfer.taxes_and_fees_included is None
                    and transfer.adults == 2
                    and "公开基础价 USD" in transfer.contract_evidence_text
                    and "税费未确认" in transfer.contract_evidence_text
                    for transfer in selected_icom
                )
                and bool(supplemental)
                and all(
                    item.currency == "USD"
                    and item.adults == 2
                    and item.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                    and item.taxes_and_fees_included is None
                    for item in supplemental
                )
                and selected_contract_ids == supplemental_contract_ids
                and selected_transfer_ids == supplemental_transfer_ids
                and supplemental_usd_cents > 0
                and not package.budget.is_all_in_total
                and not package.budget.budget_compliance_fully_verified
                and package.budget.confirmed_subtotal_cents == package.budget.total_cents
                and "另有公开基础价 USD" in formula
                and "税费未知" in formula
                and "未换汇" in formula
                and "未计入 CNY 已确认小计" in formula
                and required_violation_codes <= violation_codes
            )
        passed = (
            expected_total == declared_total == candidate.declared_total_cents
            and evidence_count > 0
            and all(quote.evidence_refs for quote in selected_quotes)
            and set(package.evidence_refs) == set(candidate.evidence_refs)
            and all(quote.is_fresh(now) for quote in selected_quotes)
            and published_base_fare_boundary_ok
        )
    return LiveDoneGateCheck(
        id="exact_budget_and_selected_evidence",
        passed=passed,
        summary=(
            "CNY 已确认小计逐项相等；入选 iCom 时另列 non-all-in USD 基础价、税费未知且未换汇"
            if passed
            else "预算算式、报价新鲜度、被选组件证据或 iCom 跨币种基础价边界不完整"
        ),
        evidence={
            "computed_total_cents": expected_total,
            "declared_total_cents": declared_total,
            "evidence_ref_count": evidence_count,
            "selected_icom_transfer_count": selected_icom_count,
            "supplemental_usd_cents": supplemental_usd_cents,
            "published_base_fare_boundary_ok": published_base_fare_boundary_ok,
        },
    )


def _check_selected_party_availability(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
) -> LiveDoneGateCheck:
    initial_confirmed = (
        initial.package is not None
        and initial.package.final_candidate.flight.party_availability_confirmed
    )
    event_confirmed = (
        event.package is not None
        and event.package.final_candidate.flight.party_availability_confirmed
    )
    selected_party_availability_confirmed = initial_confirmed and event_confirmed
    return LiveDoneGateCheck(
        id="selected_party_availability_confirmed",
        passed=selected_party_availability_confirmed,
        summary=(
            "初始整包与事件重规划整包均已确认请求人数的航班库存"
            if selected_party_availability_confirmed
            else "初始整包或事件重规划整包未确认请求人数的航班库存"
        ),
        evidence={
            "selected_party_availability_confirmed": selected_party_availability_confirmed,
            "initial_selected_party_availability_confirmed": initial_confirmed,
            "event_selected_party_availability_confirmed": event_confirmed,
        },
    )


def _check_event_replan(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    snapshots: tuple[BrowserTaskSnapshot, ...],
    snapshot_errors: tuple[str, ...],
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> LiveDoneGateCheck:
    browser_check = _check_real_browser_evidence(
        snapshots,
        snapshot_errors,
        now=now,
        maximum_quote_age=maximum_quote_age,
        check_id="event_browser_evidence",
    )
    package = event.package
    previous = initial.package
    event_handoff_present = False
    event_handoff_ok = False
    selected_party_availability_confirmed = False
    current_candidate_id: str | None = None
    repaired_candidate_id: str | None = None
    reverified_candidate_id: str | None = None
    reverification_phase: str | None = None
    independent_audit_present = False
    independent_audit_passed = False
    independent_audit_before_candidate_id: str | None = None
    independent_audit_after_candidate_id: str | None = None
    if package is None or previous is None:
        passed = False
        preservation = Decimal(0)
        removed = 0
        added = 0
        budget_matches = False
    else:
        selected_party_availability_confirmed = (
            package.final_candidate.flight.party_availability_confirmed
        )
        diff = package.diff
        preservation = package.preservation_ratio
        removed = len(diff.removed_component_ids) if diff else 0
        added = len(diff.added_component_ids) if diff else 0
        budget_matches = (
            package.budget.total_cents
            == package.final_candidate.computed_total_cents
            == package.final_candidate.declared_total_cents
        )
        handoff = package.event_handoff
        independent_audit = event.package_reverification_audit
        independent_audit_present = independent_audit is not None
        if independent_audit is not None:
            independent_audit_passed = independent_audit.passed
            independent_audit_before_candidate_id = independent_audit.before_candidate_id
            independent_audit_after_candidate_id = independent_audit.after_candidate_id
        event_handoff_present = handoff is not None
        if handoff is not None:
            repair = handoff.repair
            repaired = repair.outcome.candidate
            reverification = handoff.reverification
            current_candidate_id = repair.current_candidate_id
            repaired_candidate_id = repaired.id if repaired is not None else None
            reverified_candidate_id = (
                reverification.candidate_id if reverification is not None else None
            )
            reverification_phase = (
                reverification.phase.value if reverification is not None else None
            )
            event_handoff_ok = (
                repair.event.id == event.event.id
                and repair.event.kind == event.event.kind
                and repair.event.target_component_id == event.event.target_component_id
                and repair.current_candidate_id == previous.final_candidate.id
                and repair.current_candidate_version == previous.final_candidate.version
                and repair.current_component_ids == previous.final_candidate.component_ids
                and repaired == package.final_candidate
                and repair.outcome.diff == package.diff
                and repaired is not None
                and diff is not None
                and repair.event.replacement_component_id in diff.added_component_ids
                and reverification is not None
                and reverification.phase == PackageVerificationPhase.EVENT_REVERIFICATION
                and reverification.matches(package.final_candidate)
                and reverification.violations == package.final_violations
                and not reverification.errors
                and independent_audit is not None
                and independent_audit.passed
                and independent_audit.before_candidate_id == previous.final_candidate.id
                and independent_audit.after_candidate_id == package.final_candidate.id
            )
        passed = (
            browser_check.passed
            and event_handoff_present
            and event_handoff_ok
            and event.decision.state == PackageDecisionState.ACCEPT
            and package.final_decision.state == PackageDecisionState.ACCEPT
            and len(event.requeried_providers) == 1
            and len(event.source_task_ids) == 1
            and removed == 1
            and added == 1
            and preservation >= Decimal("0.75")
            and budget_matches
            and selected_party_availability_confirmed
            and package.final_candidate.id != previous.final_candidate.id
            and "不得声称重新完成三平台全量核价" in event.claim_boundary
        )
    return LiveDoneGateCheck(
        id="event_injection_dynamic_replan",
        passed=passed,
        summary=(
            "事件仅重查受影响组件，Repair 经 EVENT_REVERIFICATION 复验后重新核算预算并接受"
            if passed
            else "事件重查、event_handoff、独立复验、局部差异或预算证据未通过"
        ),
        evidence={
            "requeried_providers": [item.value for item in event.requeried_providers],
            "source_task_count": len(event.source_task_ids),
            "event_handoff_present": event_handoff_present,
            "event_handoff_ok": event_handoff_ok,
            "current_candidate_id": current_candidate_id,
            "repaired_candidate_id": repaired_candidate_id,
            "reverified_candidate_id": reverified_candidate_id,
            "reverification_phase": reverification_phase,
            "independent_audit_present": independent_audit_present,
            "independent_audit_passed": independent_audit_passed,
            "independent_audit_before_candidate_id": (independent_audit_before_candidate_id),
            "independent_audit_after_candidate_id": (independent_audit_after_candidate_id),
            "removed_component_count": removed,
            "added_component_count": added,
            "preservation_ratio": str(preservation),
            "budget_matches": budget_matches,
            "selected_party_availability_confirmed": selected_party_availability_confirmed,
            "browser_evidence": cast(JsonValue, browser_check.model_dump(mode="json")),
        },
    )
