from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import Field, JsonValue

from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexiblePairState,
)
from tripchord.agents.live_done_gate import (
    LiveDoneGateCheck,
    _check_actual_overlap,
    _check_budget_and_evidence,
    _check_event_replan,
    _check_icom_public_transfer_evidence,
    _check_planner_verifier_repair,
    _check_read_only_graph,
    _check_real_browser_evidence,
    _check_selected_party_availability,
    _event_source_snapshots,
    _icom_result_errors,
    _source_snapshots,
    _verified_browser_terminal_receipt,
)
from tripchord.agents.live_system import (
    FlightSearchOutcomeState,
    LiveCoverageMode,
    LiveDataProvider,
    LiveEventReplanRun,
    LiveEvidenceScope,
    LiveFinalizationState,
    LivePackageAgentRun,
    LiveRunPurpose,
)
from tripchord.agents.models import AgentTask, AgentTaskResult
from tripchord.domain.common import DomainModel
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORM_QUERY_KINDS,
    LIVE_V5_PLATFORMS,
    QueryTaskKind,
)
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageDecisionState,
)
from tripchord.planning.stay_plans import (
    StayInventoryResultState,
    StayPlanCandidateSet,
    StayPlanInventoryOutcome,
    stay_plan_for_candidate,
)
from tripchord.providers.browser_bridge import (
    QUNAR_CURRENT_DETAIL_FALLBACK_SUMMARY_VERSION,
    QUNAR_DETAIL_SEED_SELECTION_POLICY,
    BrowserFailure,
    BrowserFailureCode,
    BrowserProvider,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
    BrowserVertical,
    FlightSearchReceipt,
    FlightSearchReceiptState,
    LodgingInventoryReceipt,
    LodgingInventoryReceiptState,
    flight_search_receipt_sha256,
    lodging_inventory_query_fingerprint_sha256,
    lodging_inventory_receipt_sha256,
    qunar_detail_seed_selection,
    trusted_search_url_contract,
)
from tripchord.providers.icom_transfer import (
    IComTransferQuery,
    IComTransferSearchResult,
    to_package_transfer_option,
)
from tripchord.providers.quote_normalizer import BrowserQuoteNormalizer

_PUBLICATION_QUOTE_TTL = timedelta(seconds=600)
_STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT = 2
_EXPLORATION_DEFERRED_STAGE_IDS = (
    "explain-final-decision",
    "curate-run-memory",
    "publish-live-run",
)
_EXPLORATION_SEAL_TASK_ID = "seal-exploration-run"
_DECISION_STAGE_DEPENDENCIES = {
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
}
_FINALIZATION_STAGE_DEPENDENCIES = {
    "explain-final-decision": ("orchestrate-travel-package",),
    "curate-run-memory": ("explain-final-decision",),
    "publish-live-run": ("curate-run-memory",),
}


class LiveV4DoneGateCheck(DomainModel):
    name: str = Field(min_length=1)
    passed: bool
    summary: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    evidence: dict[str, JsonValue] = Field(default_factory=dict)


class LiveV4DoneGateReport(DomainModel):
    schema_version: str = "tripchord-live-v4-done-gate-report"
    passed: bool
    checks: tuple[LiveV4DoneGateCheck, ...] = Field(min_length=1)


def evaluate_live_v4_done_gate(
    run: FlexibleLiveAgentRun,
    *,
    expected_candidate_set: StayPlanCandidateSet,
    selected_initial: LivePackageAgentRun | None = None,
    event: LiveEventReplanRun | None = None,
    evaluated_at: datetime | None = None,
    maximum_quote_age_minutes: int = 15,
    minimum_recommendable_options: int = 2,
    minimum_exact_providers_per_selected_segment: int = (
        _STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT
    ),
) -> LiveV4DoneGateReport:
    """Fail-closed gate for the candidate-set additions layered over live-v3."""

    if (
        minimum_exact_providers_per_selected_segment
        != _STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT
    ):
        raise ValueError("strict selected-segment exact provider threshold is frozen at 2")
    now = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    maximum_quote_age = timedelta(minutes=maximum_quote_age_minutes)
    selected_exploration = selected_initial
    if selected_initial is not None:
        selected_pair = next(
            (
                execution
                for execution in run.pair_runs
                if execution.run is not None
                and execution.run.intent.trip_id == selected_initial.intent.trip_id
            ),
            None,
        )
        if selected_pair is not None:
            selected_exploration = (
                getattr(selected_pair, "exploration_run", None) or selected_initial
            )
    checks = (
        _check_prefrozen_candidate_set(run, expected_candidate_set),
        _check_v4_source_graph(run, expected_candidate_set),
        _check_stage_aware_run_contracts(run),
        _check_inventory_outcome_contract(
            run,
            expected_candidate_set,
            minimum_exact_providers_per_selected_segment=(
                _STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT
            ),
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _check_selected_plan_handoffs(run, expected_candidate_set),
        _check_recommendable_options(
            run,
            minimum_recommendable_options,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _check_all_recommended_publication_closures(run, now=now),
        _check_selected_v4_runtime_evidence(
            selected_initial,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _check_v4_flight_search_outcomes(
            selected_exploration,
            now=now,
            maximum_quote_age=maximum_quote_age,
            require_fresh_quotes=(selected_exploration is selected_initial),
        ),
        _check_v4_observed_overlap(selected_exploration),
        _check_v4_strict_selected_coverage(
            selected_exploration,
            expected_candidate_set,
        ),
        _check_v4_public_transfer_evidence(
            selected_exploration,
            selected_initial,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
        _adapt_v3_check(
            _check_planner_verifier_repair(selected_initial)
            if selected_initial is not None
            else _missing_v3_check(
                "planner_verifier_repair_orchestrator",
                "缺少选中初始运行，无法验证多 Agent 规划闭环",
            )
        ),
        _adapt_v3_check(
            _check_budget_and_evidence(selected_initial, now=now)
            if selected_initial is not None
            else _missing_v3_check(
                "exact_budget_and_selected_evidence",
                "缺少选中初始运行，无法复算预算与证据",
            )
        ),
        _check_v4_event_chain(
            selected_initial,
            event,
            expected_candidate_set,
            now=now,
            maximum_quote_age=maximum_quote_age,
        ),
    )
    return LiveV4DoneGateReport(
        passed=all(item.passed for item in checks),
        checks=checks,
    )


def _check_prefrozen_candidate_set(
    run: FlexibleLiveAgentRun,
    expected: StayPlanCandidateSet,
) -> LiveV4DoneGateCheck:
    actual = run.stay_plan_candidate_set
    errors: list[str] = []
    if actual is None:
        errors.append("运行结果缺少预冻结住宿候选集")
    elif actual.candidate_set_sha256 != expected.candidate_set_sha256:
        errors.append("运行时住宿候选集与场景冻结 SHA 不一致")
    if run.query_plan.stay_plan_candidate_set_sha256 != expected.candidate_set_sha256:
        errors.append("查询计划未绑定场景冻结 SHA")
    if run.query_plan.frozen_stay_plan_ids != expected.stay_plan_ids:
        errors.append("查询计划改变了冻结方案顺序或成员")
    return LiveV4DoneGateCheck(
        name="prefrozen_stay_plan_candidate_set",
        passed=not errors,
        summary="；".join(errors) if errors else "候选集在搜索前冻结且 SHA 贯穿查询计划",
        evidence_refs=(f"sha256:{expected.candidate_set_sha256}",),
    )


def _check_v4_source_graph(
    run: FlexibleLiveAgentRun,
    candidate_set: StayPlanCandidateSet,
) -> LiveV4DoneGateCheck:
    errors: list[str] = []
    lodging_task_count = sum(len(item.segments) for item in candidate_set.candidates)
    lodging_platforms = {
        platform
        for platform in LIVE_V5_PLATFORMS
        if QueryTaskKind.LODGING_FULL_STAY in LIVE_V5_PLATFORM_QUERY_KINDS[platform]
    }
    expected_lodging_tasks = lodging_task_count * len(lodging_platforms)
    expected_browser_tasks = len(LIVE_V5_PLATFORMS) + expected_lodging_tasks
    expected_icom_tasks = {
        f"public-transfer-icom-{contract.contract_id.removeprefix('icom-')}"
        for plan in candidate_set.candidates
        for contract in plan.required_transfer_contracts
        if contract.required_provider == "icom-public-transfer"
    }
    expected_query_shapes = {
        (platform, kind)
        for platform in LIVE_V5_PLATFORMS
        for kind in LIVE_V5_PLATFORM_QUERY_KINDS[platform]
    }
    expected_browser_source_ids = {
        f"source-{platform.value}-{suffix}"
        for platform in LIVE_V5_PLATFORMS
        for suffix in (
            "flight",
            *(
                f"lodging-{segment.query_segment}"
                for plan in candidate_set.candidates
                for segment in plan.segments
                if platform in lodging_platforms
            ),
        )
    }
    pair_ids = tuple(execution.date_pair.id for execution in run.pair_runs)
    pair_dates = tuple(
        (
            execution.date_pair.departure_date,
            execution.date_pair.return_date,
        )
        for execution in run.pair_runs
    )
    selected_pair_ids = run.query_plan.selected_pair_ids
    planned_tasks = run.query_plan.tasks
    if len(run.pair_runs) != 3:
        errors.append(f"live-v4 必须执行 3 个日期对，实际 {len(run.pair_runs)} 个")
    if len(pair_ids) != len(set(pair_ids)):
        errors.append("pair_runs 存在重复 date_pair.id")
    if len(pair_dates) != len(set(pair_dates)):
        errors.append("pair_runs 存在重复出发/返程日期")
    if (
        len(selected_pair_ids) != 3
        or len(set(selected_pair_ids)) != 3
        or tuple(selected_pair_ids) != pair_ids
    ):
        errors.append("query_plan.selected_pair_ids 未精确绑定 3 个唯一 pair_runs")
    if (
        run.query_plan.total_task_count != expected_browser_tasks * 3
        or len(planned_tasks) != expected_browser_tasks * 3
    ):
        errors.append(f"query_plan 必须恰好包含 {expected_browser_tasks * 3} 路浏览器查询")
    planned_task_ids = tuple(task.id for task in planned_tasks)
    if len(planned_task_ids) != len(set(planned_task_ids)):
        errors.append("query_plan.tasks 存在重复 task.id")
    planned_pair_ids = tuple(task.date_pair_id for task in planned_tasks)
    if set(planned_pair_ids) != set(pair_ids) or any(
        planned_pair_ids.count(pair_id) != expected_browser_tasks for pair_id in pair_ids
    ):
        errors.append("query_plan.tasks 未按每个唯一日期对精确分配启用能力查询")
    for execution in run.pair_runs:
        source_run = getattr(execution, "exploration_run", None) or execution.run
        if source_run is None:
            errors.append(f"{execution.date_pair.id}: 日期对未完成真实运行")
            continue
        if execution.state != FlexiblePairState.COMPLETED:
            errors.append(f"{execution.date_pair.id}: 日期对状态不是 completed")
        pair_query_tasks = execution.query_tasks
        pair_query_ids = tuple(task.id for task in pair_query_tasks)
        planned_pair_tasks = tuple(
            task for task in planned_tasks if task.date_pair_id == execution.date_pair.id
        )
        if len(pair_query_tasks) != expected_browser_tasks:
            errors.append(f"{execution.date_pair.id}: 查询计划不是 {expected_browser_tasks} 路")
        if len(pair_query_ids) != len(set(pair_query_ids)):
            errors.append(f"{execution.date_pair.id}: 查询计划存在重复 task.id")
        if any(task.date_pair_id != execution.date_pair.id for task in pair_query_tasks):
            errors.append(f"{execution.date_pair.id}: execution 查询任务混入其他日期对")
        if tuple(pair_query_tasks) != planned_pair_tasks:
            errors.append(f"{execution.date_pair.id}: execution 查询任务未精确绑定 query_plan 子集")
        pair_query_shapes = {(task.platform, task.kind) for task in pair_query_tasks}
        if (
            len(pair_query_shapes) != expected_browser_tasks
            or pair_query_shapes != expected_query_shapes
        ):
            errors.append(f"{execution.date_pair.id}: 查询任务未完整覆盖已启用平台能力")
        source_ids = source_run.source_task_ids
        if (
            len(source_ids) != expected_browser_tasks
            or len(source_ids) != len(set(source_ids))
            or set(source_ids) != expected_browser_source_ids
        ):
            errors.append(
                f"{execution.date_pair.id}: 实际浏览器 DAG 未精确绑定 "
                f"{expected_browser_tasks} 个唯一 Source ID"
            )
        public_transfer_ids = source_run.public_transfer_task_ids
        if (
            len(public_transfer_ids) != len(expected_icom_tasks)
            or len(public_transfer_ids) != len(set(public_transfer_ids))
            or set(public_transfer_ids) != expected_icom_tasks
        ):
            errors.append(f"{execution.date_pair.id}: iCom Source Agent 未从冻结接驳合同完整生成")
        graph_ids = {item.id for item in source_run.scheduler.graph.tasks}
        if (
            not (set(source_run.source_task_ids) | set(source_run.public_transfer_task_ids))
            <= graph_ids
        ):
            errors.append(f"{execution.date_pair.id}: Source Agent 未全部进入可追溯 DAG")
    return LiveV4DoneGateCheck(
        name="v4_source_graph",
        passed=not errors,
        summary=(
            "；".join(errors)
            if errors
            else (
                f"每个日期对固定为 {expected_browser_tasks} 路浏览器 Source Agent，"
                f"并从冻结合同生成 {len(expected_icom_tasks)} 路 iCom Source Agent"
            )
        ),
    )


def _check_stage_aware_run_contracts(
    run: FlexibleLiveAgentRun,
) -> LiveV4DoneGateCheck:
    """Require three sealed explorations and two fully published refreshes.

    ``deferred`` is a lifecycle fact, not a successful task result.  This gate
    independently cross-checks the declared purpose against the scheduler DAG
    and results so a forged flag cannot make an incomplete run rankable.
    """

    errors: list[str] = []
    evidence_by_pair: dict[str, JsonValue] = {}
    exploration_count = 0
    exploration_trip_ids: list[str] = []
    publication_count = 0
    publication_option_ids: list[str] = []

    for execution in run.pair_runs:
        pair_id = execution.date_pair.id
        exploration = execution.exploration_run or execution.run
        publication = (
            execution.run
            if execution.run is not None
            and execution.run.evidence_scope == LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH
            else None
        )
        pair_evidence: dict[str, JsonValue] = {
            "exploration_present": exploration is not None,
            "publication_present": publication is not None,
        }

        if exploration is None:
            errors.append(f"{pair_id}: 缺少可封存的探索运行")
        else:
            exploration_count += 1
            exploration_trip_ids.append(exploration.intent.trip_id)
            exploration_errors = _stage_run_errors(
                exploration,
                purpose=LiveRunPurpose.EXPLORATION_SELECTION,
                finalization_state=LiveFinalizationState.EXPLORATION_SEALED,
                required_dependencies={
                    **_DECISION_STAGE_DEPENDENCIES,
                    _EXPLORATION_SEAL_TASK_ID: ("orchestrate-travel-package",),
                },
                terminal_task_id=_EXPLORATION_SEAL_TASK_ID,
            )
            if exploration.evidence_scope != LiveEvidenceScope.FULL_SEARCH:
                exploration_errors.append("探索运行不是 full_search 证据范围")
            if (
                exploration.intent.trip_id != f"flexible:{pair_id}"
                or exploration.search_query.start_date != execution.date_pair.departure_date
                or exploration.search_query.end_date != execution.date_pair.return_date
            ):
                exploration_errors.append("探索运行未绑定当前唯一日期对")
            if exploration.deferred_stage_ids != _EXPLORATION_DEFERRED_STAGE_IDS:
                exploration_errors.append("探索运行未精确延后 Explanation/Memory/Publish")
            if not exploration.exploration_seal_passed:
                exploration_errors.append("探索运行未声明通过确定性 seal")
            if exploration.explanation is not None or exploration.memory_candidates is not None:
                exploration_errors.append("探索运行不得产生 Explanation 或 Memory 产物")
            exploration_errors.extend(_deferred_stage_execution_errors(exploration))
            seal_result = {item.task_id: item for item in exploration.scheduler.results}.get(
                _EXPLORATION_SEAL_TASK_ID
            )
            if seal_result is None or seal_result.output.get("exploration_seal_passed") is not True:
                exploration_errors.append("seal 终端结果未返回 exploration_seal_passed=true")
            elif (
                seal_result.output.get("decision_present") is not True
                or seal_result.output.get("model_required_failed") is not False
                or seal_result.output.get("memory_persisted") is not False
                or seal_result.output.get("deferred_stage_ids")
                != list(_EXPLORATION_DEFERRED_STAGE_IDS)
            ):
                exploration_errors.append(
                    "seal 终端回执未证明决策完整、模型无失败、memory 未持久化且三个最终阶段精确延后"
                )
            errors.extend(f"{pair_id}: {item}" for item in exploration_errors)
            pair_evidence["exploration"] = cast(
                JsonValue,
                {
                    "run_purpose": exploration.run_purpose.value,
                    "finalization_state": exploration.finalization_state.value,
                    "deferred_stage_ids": list(exploration.deferred_stage_ids),
                    "terminal_task_id": _EXPLORATION_SEAL_TASK_ID,
                    "errors": exploration_errors,
                },
            )

        if publication is not None:
            publication_count += 1
            publication_errors = _stage_run_errors(
                publication,
                purpose=LiveRunPurpose.FINAL_PUBLICATION,
                finalization_state=LiveFinalizationState.FINAL_PUBLISHED,
                required_dependencies={
                    **_DECISION_STAGE_DEPENDENCIES,
                    **_FINALIZATION_STAGE_DEPENDENCIES,
                },
                terminal_task_id="publish-live-run",
            )
            if publication.deferred_stage_ids:
                publication_errors.append("最终发布运行不得延后任何阶段")
            if (
                exploration is None
                or publication.intent.trip_id != exploration.intent.trip_id
                or publication.search_query != exploration.search_query
            ):
                publication_errors.append("最终发布未与对应探索日期对强绑定")
            if publication.exploration_seal_passed:
                publication_errors.append("最终发布不得冒充 exploration seal")
            publish_result = {item.task_id: item for item in publication.scheduler.results}.get(
                "publish-live-run"
            )
            if (
                publish_result is None
                or publish_result.output.get("publication_gate_passed") is not True
            ):
                publication_errors.append("发布终端结果未返回 publication_gate_passed=true")
            audit = execution.publication_refresh_audit
            if audit is None or not audit.binding_passed or audit.refreshed_option_id is None:
                publication_errors.append("最终发布运行未绑定通过的 refresh audit")
            else:
                publication_option_ids.append(audit.refreshed_option_id)
            errors.extend(f"{pair_id}: {item}" for item in publication_errors)
            pair_evidence["publication"] = cast(
                JsonValue,
                {
                    "run_purpose": publication.run_purpose.value,
                    "finalization_state": publication.finalization_state.value,
                    "deferred_stage_ids": list(publication.deferred_stage_ids),
                    "terminal_task_id": "publish-live-run",
                    "errors": publication_errors,
                },
            )
        evidence_by_pair[pair_id] = cast(JsonValue, pair_evidence)

    if exploration_count != 3:
        errors.append(f"必须恰好封存 3 个探索运行，实际 {exploration_count} 个")
    if len(set(exploration_trip_ids)) != 3:
        errors.append("3 个探索运行必须绑定 3 个唯一 trip_id")
    if publication_count != 2:
        errors.append(f"必须恰好完成 2 个发布刷新运行，实际 {publication_count} 个")
    if run.publication_refresh_minimum_options != 2:
        errors.append("发布刷新冻结下限必须为 2")
    if (
        len(publication_option_ids) != len(set(publication_option_ids))
        or len(run.recommended_option_ids) != 2
        or set(publication_option_ids) != set(run.recommended_option_ids)
    ):
        errors.append("最终推荐必须精确等于两个完整发布运行的 option_id 集合")

    return LiveV4DoneGateCheck(
        name="stage_aware_exploration_publication_contract",
        passed=not errors,
        summary=(
            "3 个探索运行均已封存且仅延后 Explanation/Memory/Publish，"
            "2 个最终方案均完成全量发布尾链"
            if not errors
            else "；".join(errors)
        ),
        evidence={
            "exploration_count": exploration_count,
            "publication_count": publication_count,
            "publication_option_ids": publication_option_ids,
            "pairs": evidence_by_pair,
            "errors": errors,
        },
    )


def _stage_run_errors(
    live_run: LivePackageAgentRun,
    *,
    purpose: LiveRunPurpose,
    finalization_state: LiveFinalizationState,
    required_dependencies: dict[str, tuple[str, ...]],
    terminal_task_id: str,
) -> list[str]:
    errors: list[str] = []
    if live_run.run_purpose != purpose:
        errors.append(f"run_purpose 应为 {purpose.value}，实际 {live_run.run_purpose.value}")
    if live_run.finalization_state != finalization_state:
        errors.append(
            "finalization_state 应为 "
            f"{finalization_state.value}，实际 {live_run.finalization_state.value}"
        )
    graph_by_id = {task.id: task for task in live_run.scheduler.graph.tasks}
    result_by_id = {result.task_id: result for result in live_run.scheduler.results}
    for task_id, dependencies in required_dependencies.items():
        graph_task = graph_by_id.get(task_id)
        if graph_task is None:
            errors.append(f"缺少必需阶段 {task_id}")
            continue
        if tuple(graph_task.dependencies) != dependencies:
            errors.append(f"{task_id} 依赖链不匹配")
        result = result_by_id.get(task_id)
        if result is None or not result.success:
            errors.append(f"{task_id} 没有成功结果")
    terminal = result_by_id.get(terminal_task_id)
    if terminal is None or not terminal.success:
        errors.append(f"终端阶段 {terminal_task_id} 未成功")
    if not live_run.scheduler.succeeded:
        errors.append("scheduler 未完整成功")
    return errors


def _deferred_stage_execution_errors(
    exploration: LivePackageAgentRun,
) -> list[str]:
    graph_ids = {task.id for task in exploration.scheduler.graph.tasks}
    result_ids = {result.task_id for result in exploration.scheduler.results}
    trace_ids = {stage.task_id for stage in exploration.agentic.stages}
    errors: list[str] = []
    for stage_id in _EXPLORATION_DEFERRED_STAGE_IDS:
        surfaces = tuple(
            name
            for name, ids in (
                ("graph", graph_ids),
                ("result", result_ids),
                ("agentic_trace", trace_ids),
            )
            if stage_id in ids
        )
        if surfaces:
            errors.append(f"延后阶段 {stage_id} 不得冒充成功执行：{','.join(surfaces)}")
    return errors


def _check_inventory_outcome_contract(
    run: FlexibleLiveAgentRun,
    candidate_set: StayPlanCandidateSet,
    *,
    minimum_exact_providers_per_selected_segment: int,
    now: datetime | None = None,
    maximum_quote_age: timedelta | None = None,
) -> LiveV4DoneGateCheck:
    if (
        minimum_exact_providers_per_selected_segment
        != _STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT
    ):
        raise ValueError("strict selected-segment exact provider threshold is frozen at 2")
    errors: list[str] = []
    expected = {
        (
            provider,
            plan.stay_plan_id,
            segment.segment_id,
        )
        for provider in ("ctrip", "qunar")
        for plan in candidate_set.candidates
        for segment in plan.segments
    }
    for execution in run.pair_runs:
        live_run = getattr(execution, "exploration_run", None) or execution.run
        if live_run is None:
            continue
        observed_rows = tuple(
            (item.provider, item.stay_plan_id, item.segment_id)
            for item in live_run.stay_plan_inventory_outcomes
        )
        observed = set(observed_rows)
        missing = expected - observed
        if missing:
            errors.append(
                f"{execution.date_pair.id}: {len(missing)} 路启用住宿结果没有合法四态证据"
            )
        if len(observed_rows) != len(observed):
            errors.append(f"{execution.date_pair.id}: 住宿四态结果存在重复身份")
        configured_refresh = getattr(
            run,
            "publication_refresh_minimum_options",
            0,
        )
        if now is not None and maximum_quote_age is not None:
            errors.extend(
                f"{execution.date_pair.id}: {error}"
                for error in _inventory_outcome_evidence_errors(
                    live_run,
                    candidate_set,
                    now=now,
                    maximum_quote_age=maximum_quote_age,
                    require_fresh_quotes=(configured_refresh == 0),
                )
            )
        selected = live_run.selected_stay_plan_id
        if selected is None:
            continue
        required_segments = {item.segment_id for item in candidate_set.candidate(selected).segments}
        selected_outcomes = tuple(
            item for item in live_run.stay_plan_inventory_outcomes if item.stay_plan_id == selected
        )
        exact_providers = {
            segment: {
                item.provider
                for item in selected_outcomes
                if item.segment_id == segment and item.state == StayInventoryResultState.QUOTE_FOUND
            }
            for segment in required_segments
        }
        insufficient = {
            segment: providers
            for segment, providers in exact_providers.items()
            if len(providers) < minimum_exact_providers_per_selected_segment
        }
        if insufficient:
            rendered = "，".join(
                f"{segment}={len(providers)}家"
                for segment, providers in sorted(insufficient.items())
            )
            errors.append(
                f"{execution.date_pair.id}: 选中方案逐分段精确报价不足 "
                f"{minimum_exact_providers_per_selected_segment} 家（{rendered}）"
            )
    four_state_summary = (
        "住宿库存四态为 exact_quote、confirmed_empty、"
        "bounded_no_exact_quote、bounded_provider_pending"
    )
    return LiveV4DoneGateCheck(
        name="stay_inventory_four_state_contract",
        passed=not errors,
        summary=(
            f"{four_state_summary}；{'；'.join(errors)}"
            if errors
            else (
                f"{four_state_summary}；所有住宿 Source 均显式落入其中之一；"
                f"选中方案每个分段至少有 "
                f"{minimum_exact_providers_per_selected_segment} 家平台精确报价"
            )
        ),
        evidence={
            "minimum_exact_providers_per_selected_segment": (
                minimum_exact_providers_per_selected_segment
            ),
            "inventory_states": [
                "exact_quote",
                StayInventoryResultState.CONFIRMED_EMPTY.value,
                StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE.value,
                StayInventoryResultState.BOUNDED_PROVIDER_PENDING.value,
            ],
        },
    )


def _verified_confirmed_empty_observation_chain(
    raw_receipt: dict[str, JsonValue],
    receipt: LodgingInventoryReceipt,
    failure: BrowserFailure,
) -> bool:
    """Independently recompute both child receipts and the v2 parent lineage."""

    raw_chain = raw_receipt.get("observation_chain")
    chain = receipt.observation_chain
    if (
        receipt.state != LodgingInventoryReceiptState.CONFIRMED_EMPTY
        or receipt.schema_version != "tripchord-lodging-inventory-receipt-v2"
        or chain is None
        or not isinstance(raw_chain, dict)
        or set(raw_chain)
        != {
            "schema_version",
            "query_fingerprint_sha256",
            "observations",
            "observed_interval_ms",
            "detail_fallback",
            "sealed_at",
        }
        or raw_chain.get("schema_version") != "tripchord-qunar-empty-observation-chain-v1"
    ):
        return False
    raw_observations = raw_chain.get("observations")
    if not isinstance(raw_observations, list) or len(raw_observations) != 2:
        return False
    expected_observation_keys = {
        "ordinal",
        "receipt",
        "receipt_sha256",
        "captured_at",
        "query_fingerprint_sha256",
        "lineage",
    }
    expected_lineage_keys = {
        "schema_version",
        "isolation_scope",
        "runtime_lineage_sha256",
        "window_lineage_sha256",
        "tab_lineage_sha256",
    }
    validated: list[tuple[dict[str, JsonValue], datetime, dict[str, JsonValue]]] = []
    for ordinal, raw_observation in enumerate(raw_observations, start=1):
        if (
            not isinstance(raw_observation, dict)
            or set(raw_observation) != expected_observation_keys
            or raw_observation.get("ordinal") != ordinal
        ):
            return False
        raw_child = raw_observation.get("receipt")
        child_sha = raw_observation.get("receipt_sha256")
        captured_at = raw_observation.get("captured_at")
        query_fingerprint = raw_observation.get("query_fingerprint_sha256")
        lineage = raw_observation.get("lineage")
        if (
            not isinstance(raw_child, dict)
            or not isinstance(child_sha, str)
            or lodging_inventory_receipt_sha256(raw_child) != child_sha
            or not isinstance(captured_at, str)
            or captured_at != raw_child.get("captured_at")
            or not isinstance(query_fingerprint, str)
            or query_fingerprint
            != lodging_inventory_query_fingerprint_sha256(raw_child.get("confirmed_query"))
            or query_fingerprint != raw_chain.get("query_fingerprint_sha256")
            or not isinstance(lineage, dict)
            or set(lineage) != expected_lineage_keys
            or lineage.get("schema_version") != "tripchord-browser-lineage-hash-v1"
            or lineage.get("isolation_scope")
            != "companion_owned_unfocused_normal_window_active_tab"
            or any(
                not isinstance(lineage.get(field), str)
                or re.fullmatch(r"[a-f0-9]{64}", str(lineage.get(field))) is None
                for field in (
                    "runtime_lineage_sha256",
                    "window_lineage_sha256",
                    "tab_lineage_sha256",
                )
            )
        ):
            return False
        try:
            parsed_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        validated.append((raw_child, parsed_at, lineage))
    first_child, first_at, first_lineage = validated[0]
    second_child, second_at, second_lineage = validated[1]
    interval_ms = int((second_at - first_at).total_seconds() * 1000)
    expected_parent = {
        **second_child,
        "schema_version": "tripchord-lodging-inventory-receipt-v2",
        "observation_chain": raw_chain,
    }
    detail_fallback = raw_chain.get("detail_fallback")
    detail_orchestration = failure.details.get("detail_orchestration")
    expected_seed_offset, expected_seed_property_ids = qunar_detail_seed_selection(
        receipt.confirmed_query
    )
    raw_fallback_results = (
        detail_fallback.get("observed_results") if isinstance(detail_fallback, dict) else None
    )
    return bool(
        interval_ms == raw_chain.get("observed_interval_ms")
        and 2_000 <= interval_ms <= 120_000
        and first_child.get("confirmed_query") == second_child.get("confirmed_query")
        and first_lineage == second_lineage
        and raw_receipt == expected_parent
        and isinstance(detail_fallback, dict)
        and detail_fallback.get("contract_version") == QUNAR_CURRENT_DETAIL_FALLBACK_SUMMARY_VERSION
        and detail_fallback.get("seed_selection_policy") == QUNAR_DETAIL_SEED_SELECTION_POLICY
        and detail_fallback.get("seed_selection_offset") == expected_seed_offset
        and detail_fallback.get("target_property_ids") == list(expected_seed_property_ids)
        and isinstance(raw_fallback_results, list)
        and [item.get("property_id") for item in raw_fallback_results if isinstance(item, dict)]
        == list(expected_seed_property_ids)
        and detail_fallback.get("verified_quote_count") == 0
        and isinstance(detail_orchestration, dict)
        and detail_orchestration.get("state") == "stable_empty_no_verified_detail_quote"
        and detail_orchestration.get("verified_quote_count") == 0
        and failure.details.get("inventory_observation_chain_schema_version")
        == raw_chain.get("schema_version")
    )


def _verified_v4_lodging_inventory_receipt(
    snapshot: BrowserTaskSnapshot,
    outcome: StayPlanInventoryOutcome,
    *,
    expected_options: dict[str, str],
    now: datetime,
    maximum_quote_age: timedelta,
    require_fresh: bool,
) -> bool:
    """Cross-check one non-quote lodging outcome against every receipt surface."""

    expected_receipt_states = {
        StayInventoryResultState.CONFIRMED_EMPTY: (LodgingInventoryReceiptState.CONFIRMED_EMPTY),
        StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE: (
            LodgingInventoryReceiptState.BOUNDED_NO_EXACT_QUOTE
        ),
        StayInventoryResultState.BOUNDED_PROVIDER_PENDING: (
            LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING
        ),
    }
    expected_state = expected_receipt_states.get(outcome.state)
    failure = snapshot.failure
    if (
        expected_state is None
        or snapshot.state != BrowserTaskState.FAILED
        or failure is None
        or failure.retryable
        or outcome.inventory_receipt_sha256 is None
    ):
        return False
    raw_receipt = failure.details.get("inventory_receipt")
    sealed_sha = failure.details.get("inventory_receipt_sha256")
    if not isinstance(raw_receipt, dict) or not isinstance(sealed_sha, str):
        return False
    try:
        receipt = LodgingInventoryReceipt.model_validate(raw_receipt)
    except ValueError:
        return False

    expected_failure_codes = {
        LodgingInventoryReceiptState.CONFIRMED_EMPTY: {
            BrowserFailureCode.NO_INVENTORY,
        },
        LodgingInventoryReceiptState.BOUNDED_NO_EXACT_QUOTE: {
            BrowserFailureCode.DOM_DRIFT,
            BrowserFailureCode.EXTRACTION_ERROR,
        },
        LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING: {
            BrowserFailureCode.EXTRACTION_ERROR,
        },
    }
    pending_duration_valid = True
    if expected_state == LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING:
        pending = receipt.provider_pending_evidence
        if snapshot.claimed_at is None or pending is None:
            pending_duration_valid = False
        else:
            observed_elapsed_ms = int(
                (receipt.captured_at - snapshot.claimed_at).total_seconds() * 1000
            )
            pending_duration_valid = (
                failure.details.get("bounded_pending_observed_ms") == pending.observed_duration_ms
                and observed_elapsed_ms >= pending.observed_duration_ms
            )
    else:
        pending_duration_valid = failure.details.get("bounded_pending_observed_ms") is None

    confirmed = receipt.confirmed_query
    expected_confirmed_exhaustive = expected_state == LodgingInventoryReceiptState.CONFIRMED_EMPTY
    confirmed_empty_chain_valid = (
        _verified_confirmed_empty_observation_chain(raw_receipt, receipt, failure)
        if expected_state == LodgingInventoryReceiptState.CONFIRMED_EMPTY
        else receipt.observation_chain is None
    )
    return (
        lodging_inventory_receipt_sha256(raw_receipt) == sealed_sha
        and outcome.inventory_receipt_sha256 == sealed_sha
        and f"inventory-receipt:sha256:{sealed_sha}" in outcome.evidence_refs
        and f"browser-task:{snapshot.id}" in outcome.evidence_refs
        and receipt.provider.value == outcome.provider
        and snapshot.provider.value == outcome.provider
        and snapshot.kind == BrowserVertical.LODGING
        and receipt.state == expected_state
        and failure.code in expected_failure_codes[expected_state]
        and failure.details.get("inventory_result_state") == expected_state.value
        and failure.details.get("confirmed_exhaustive") is expected_confirmed_exhaustive
        and outcome.confirmed_exhaustive is expected_confirmed_exhaustive
        and receipt.scan_limit == outcome.scan_limit
        and receipt.scanned_count == outcome.scanned_count
        and failure.details.get("scanned_count") == outcome.scanned_count
        and confirmed.destination == snapshot.query.destination
        and confirmed.start_date == snapshot.query.start_date
        and confirmed.end_date == snapshot.query.end_date
        and confirmed.adults == snapshot.query.adults
        and confirmed.rooms == snapshot.query.rooms
        and confirmed.options == expected_options
        and all(snapshot.query.options.get(key) == value for key, value in expected_options.items())
        and receipt.page_url == failure.page_url
        and receipt.captured_at == failure.captured_at
        and receipt.captured_at <= snapshot.updated_at
        and receipt.captured_at <= now
        and (not require_fresh or now - receipt.captured_at <= maximum_quote_age)
        and pending_duration_valid
        and confirmed_empty_chain_valid
    )


def _inventory_outcome_evidence_errors(
    run: LivePackageAgentRun,
    candidate_set: StayPlanCandidateSet,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
    require_fresh_quotes: bool = True,
) -> tuple[str, ...]:
    errors: list[str] = []
    expected_source_ids = set(run.source_task_ids)
    snapshots_by_source: dict[str, list[BrowserTaskSnapshot]] = {}
    for task_result in run.scheduler.results:
        if task_result.task_id not in expected_source_ids:
            continue
        raw_snapshot = task_result.output.get("snapshot")
        if raw_snapshot is None:
            errors.append(f"{task_result.task_id}: 缺少原始浏览器 snapshot")
            continue
        try:
            snapshot = BrowserTaskSnapshot.model_validate(raw_snapshot)
        except ValueError:
            errors.append(f"{task_result.task_id}: 原始浏览器 snapshot 结构无效")
            continue
        snapshots_by_source.setdefault(task_result.task_id, []).append(snapshot)
    missing_sources = expected_source_ids - snapshots_by_source.keys()
    errors.extend(f"{task_id}: 缺少 Source task 结果" for task_id in missing_sources)
    normalizer = BrowserQuoteNormalizer()
    for outcome in run.stay_plan_inventory_outcomes:
        try:
            plan = candidate_set.candidate(outcome.stay_plan_id)
            segment = next(item for item in plan.segments if item.segment_id == outcome.segment_id)
        except (StopIteration, ValueError):
            errors.append(f"{outcome.source_task_id}: 四态结果不属于冻结住宿方案分段")
            continue
        expected_task_id = f"source-{outcome.provider}-lodging-{segment.query_segment}"
        expected_check_in = segment.check_in.resolve(run.intent)
        expected_check_out = segment.check_out.resolve(run.intent)
        if (
            outcome.source_task_id != expected_task_id
            or outcome.exact_place_key != segment.exact_place_key
            or outcome.scan_limit != plan.scan_limit_per_platform
        ):
            errors.append(f"{outcome.source_task_id}: 四态身份、精确地点或冻结扫描上限不匹配")
            continue
        matching_snapshots = snapshots_by_source.get(outcome.source_task_id, [])
        if len(matching_snapshots) != 1:
            errors.append(f"{outcome.source_task_id}: 必须唯一关联一个原始浏览器 snapshot")
            continue
        snapshot = matching_snapshots[0]
        expected_options = {
            "expected_lodging_place_key": segment.exact_place_key.value,
            "expected_package_area": segment.area.value,
            "segment": segment.query_segment,
        }
        if (
            snapshot.provider.value != outcome.provider
            or snapshot.kind != BrowserVertical.LODGING
            or snapshot.query.start_date != expected_check_in
            or snapshot.query.end_date != expected_check_out
            or snapshot.query.adults != run.intent.adults
            or snapshot.query.rooms != run.intent.rooms
            or any(
                snapshot.query.options.get(key) != value for key, value in expected_options.items()
            )
        ):
            errors.append(f"{outcome.source_task_id}: snapshot 查询与冻结分段、地点或人数不匹配")
            continue
        if outcome.state == StayInventoryResultState.QUOTE_FOUND:
            if (
                snapshot.state != BrowserTaskState.SUCCEEDED
                or outcome.raw_snapshot_id != snapshot.id
                or f"browser-task:{snapshot.id}" not in outcome.evidence_refs
            ):
                errors.append(f"{outcome.source_task_id}: 报价命中未关联成功 snapshot")
                continue
            for quote_id, normalization_ref, raw_sha in zip(
                outcome.quote_ids,
                outcome.normalization_result_refs,
                outcome.raw_quote_evidence_sha256s,
                strict=True,
            ):
                matches = tuple(
                    result
                    for result in run.normalization_results
                    if result.usable
                    and isinstance(result.quote, NormalizedLodgingQuote)
                    and result.quote.id == quote_id
                )
                expected_ref = f"normalization-result:{outcome.source_task_id}:{quote_id}"
                if len(matches) != 1 or normalization_ref != expected_ref:
                    errors.append(
                        f"{outcome.source_task_id}: 报价未唯一关联可用 normalization result"
                    )
                    continue
                normalized_result = matches[0]
                quote = normalized_result.quote
                assert isinstance(quote, NormalizedLodgingQuote)
                raw_matches = tuple(
                    raw for raw in snapshot.quotes if raw.evidence_sha256 == raw_sha
                )
                if len(raw_matches) != 1:
                    errors.append(f"{outcome.source_task_id}: normalization 未唯一关联原始可见报价")
                    continue
                raw = raw_matches[0]
                visible_sha = hashlib.sha256(raw.visible_evidence.encode()).hexdigest()
                recomputed = normalizer.normalize(raw, snapshot.query)
                if (
                    raw.provider.value != outcome.provider
                    or raw.kind != BrowserVertical.LODGING
                    or visible_sha != raw.evidence_sha256
                    or recomputed != normalized_result
                    or quote.provider != outcome.provider
                    or quote.place_key != segment.exact_place_key
                    or quote.area != segment.area
                    or quote.check_in != expected_check_in
                    or quote.check_out != expected_check_out
                    or quote.adults != run.intent.adults
                    or quote.rooms != run.intent.rooms
                    or quote.captured_at != raw.captured_at
                    or quote.captured_at > now
                    or (
                        require_fresh_quotes
                        and (not quote.is_fresh(now) or now - quote.captured_at > maximum_quote_age)
                    )
                ):
                    errors.append(
                        f"{outcome.source_task_id}: 报价 provider/分段/地点/日期/人数/"
                        "可见 SHA 或新鲜度交叉验证失败"
                    )
        elif outcome.state == StayInventoryResultState.CONFIRMED_EMPTY:
            if not _verified_v4_lodging_inventory_receipt(
                snapshot,
                outcome,
                expected_options=expected_options,
                now=now,
                maximum_quote_age=maximum_quote_age,
                require_fresh=require_fresh_quotes,
            ):
                errors.append(
                    f"{outcome.source_task_id}: confirmed_empty receipt 的状态、SHA、"
                    "查询或原始 failure 交叉验证失败"
                )
        elif outcome.state == StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE:
            if not _verified_v4_lodging_inventory_receipt(
                snapshot,
                outcome,
                expected_options=expected_options,
                now=now,
                maximum_quote_age=maximum_quote_age,
                require_fresh=require_fresh_quotes,
            ):
                errors.append(
                    f"{outcome.source_task_id}: bounded_no_exact_quote receipt 的状态、"
                    "SHA、查询或原始 failure 交叉验证失败"
                )
        elif outcome.state == StayInventoryResultState.BOUNDED_PROVIDER_PENDING:
            if not _verified_v4_lodging_inventory_receipt(
                snapshot,
                outcome,
                expected_options=expected_options,
                now=now,
                maximum_quote_age=maximum_quote_age,
                require_fresh=require_fresh_quotes,
            ):
                errors.append(
                    f"{outcome.source_task_id}: bounded_provider_pending receipt 的状态、"
                    "SHA、持续时间、查询或原始 failure 交叉验证失败"
                )
        else:
            errors.append(f"{outcome.source_task_id}: 住宿库存结果未落入受审计四态")
    return tuple(errors)


def _check_selected_plan_handoffs(
    run: FlexibleLiveAgentRun,
    candidate_set: StayPlanCandidateSet,
) -> LiveV4DoneGateCheck:
    errors: list[str] = []
    evidence: list[str] = []
    for execution in run.pair_runs:
        live_run = execution.run
        if live_run is None or live_run.package is None:
            continue
        handoff = live_run.stay_plan_planning_handoff
        if handoff is None:
            errors.append(f"{execution.date_pair.id}: 主控缺少住宿方案强交接链")
            continue
        if handoff.planner.candidate_set_sha256 != candidate_set.candidate_set_sha256:
            errors.append(f"{execution.date_pair.id}: 交接链冻结 SHA 不一致")
        final = live_run.package.final_candidate
        matched = stay_plan_for_candidate(candidate_set, live_run.intent, final)
        if matched != live_run.selected_stay_plan_id:
            errors.append(f"{execution.date_pair.id}: 最终整包与主控声明方案不一致")
        if handoff.repair.repaired_candidate_id is not None and handoff.reverification is None:
            errors.append(f"{execution.date_pair.id}: Repair 输出未经过 ReVerifier")
        evidence.extend(final.evidence_refs)
    if not evidence:
        errors.append("没有形成可审计的最终整包")
    return LiveV4DoneGateCheck(
        name="planner_verifier_repair_master_stay_plan_chain",
        passed=not errors,
        summary=(
            "；".join(errors)
            if errors
            else "Planner、Verifier、Repair、ReVerifier 与主控绑定同一冻结方案和整包组件"
        ),
        evidence_refs=tuple(dict.fromkeys(evidence)),
    )


def _check_recommendable_options(
    run: FlexibleLiveAgentRun,
    minimum: int,
    *,
    now: datetime | None = None,
    maximum_quote_age: timedelta | None = None,
) -> LiveV4DoneGateCheck:
    recommendable = tuple(item for item in run.ranked_options if item.recommendable)
    errors: list[str] = []
    option_ids = tuple(item.option_id for item in recommendable)
    option_pair_ids = tuple(item.date_pair_id for item in recommendable)
    option_dates = tuple((item.departure_date, item.return_date) for item in recommendable)
    freshness_by_option: dict[str, JsonValue] = {}
    if len(option_ids) != len(set(option_ids)):
        errors.append("可推荐选项存在重复 option_id")
    if len(option_pair_ids) != len(set(option_pair_ids)):
        errors.append("可推荐选项重复使用同一 date_pair_id")
    if len(option_dates) != len(set(option_dates)):
        errors.append("可推荐选项重复使用同一出发/返程日期")
    if tuple(run.recommended_option_ids) != option_ids:
        errors.append("recommended_option_ids 未精确等于唯一可推荐 option_id 序列")
    for item in recommendable:
        matching_executions = tuple(
            execution for execution in run.pair_runs if execution.date_pair.id == item.date_pair_id
        )
        matching_execution = matching_executions[0] if len(matching_executions) == 1 else None
        refresh_audit = (
            getattr(matching_execution, "publication_refresh_audit", None)
            if matching_execution is not None
            else None
        )
        refreshed_components_are_current = True
        if (
            now is not None
            and maximum_quote_age is not None
            and matching_execution is not None
            and matching_execution.run is not None
            and matching_execution.run.package is not None
        ):
            candidate = matching_execution.run.package.final_candidate
            components = (
                candidate.flight,
                *candidate.lodgings,
                *candidate.transfers,
            )
            refreshed_components_are_current = all(
                component.is_fresh(now)
                and component.captured_at <= now
                and now - component.captured_at < _PUBLICATION_QUOTE_TTL
                and component.expires_at - component.captured_at <= _PUBLICATION_QUOTE_TTL
                and now - component.captured_at <= maximum_quote_age
                for component in components
            )
            freshness_by_option[item.option_id] = cast(
                JsonValue,
                [
                    {
                        "component_id": component.id,
                        "captured_at": component.captured_at.isoformat(),
                        "expires_at": component.expires_at.isoformat(),
                        "age_seconds_at_post_event_gate": int(
                            (now - component.captured_at).total_seconds()
                        ),
                        "ttl_seconds": int(
                            (component.expires_at - component.captured_at).total_seconds()
                        ),
                        "fresh_at_post_event_gate": component.is_fresh(now),
                    }
                    for component in components
                ],
            )
        if (
            item.stay_plan_id is None
            or item.option_id != f"{item.date_pair_id}:{item.stay_plan_id.value}"
            or len(matching_executions) != 1
            or matching_executions[0].state != FlexiblePairState.COMPLETED
            or matching_executions[0].run is None
            or matching_executions[0].date_pair.departure_date != item.departure_date
            or matching_executions[0].date_pair.return_date != item.return_date
            or item.decision_state != PackageDecisionState.ACCEPT
            or not item.all_platforms_complete
            or item.total_budget_cents is None
            or item.evidence_completeness != 1
            or not refreshed_components_are_current
            or (
                getattr(run, "publication_refresh_minimum_options", 0) > 0
                and (
                    refresh_audit is None
                    or not refresh_audit.binding_passed
                    or refresh_audit.refreshed_option_id != item.option_id
                    or matching_execution is None
                    or matching_execution.exploration_run is None
                    or matching_execution.exploration_run.evidence_scope
                    != LiveEvidenceScope.FULL_SEARCH
                    or matching_execution.run is None
                    or matching_execution.run.evidence_scope
                    != LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH
                    or tuple(refresh_audit.source_task_ids)
                    != tuple(matching_execution.run.source_task_ids)
                )
            )
        ):
            errors.append(
                f"{item.option_id}: 推荐选项未唯一绑定 completed 日期对、"
                "住宿方案、ACCEPT 裁决、完整证据和发布前集中重新核价"
            )
    distinct_recommendable_dates = len(set(option_dates))
    if distinct_recommendable_dates < minimum:
        errors.append(
            f"仅形成 {distinct_recommendable_dates} 个独立可推荐日期对，要求至少 {minimum} 个"
        )
    configured_refresh = getattr(run, "publication_refresh_minimum_options", None)
    if configured_refresh is not None and configured_refresh < minimum:
        errors.append(
            "运行未要求足够数量的发布前集中重新核价："
            f"configured={configured_refresh}, required={minimum}"
        )
    passed = not errors
    return LiveV4DoneGateCheck(
        name="recommendable_date_pair_stay_plan_options",
        passed=passed,
        summary=(
            f"形成 {distinct_recommendable_dates} 个独立可推荐 (日期对,住宿方案) 组合"
            if passed
            else "；".join(errors)
        ),
        evidence={
            "freshness_ttl_seconds": 600,
            "freshness_evaluated_after_event_at": (now.isoformat() if now is not None else None),
            "freshness_by_option": freshness_by_option,
        },
    )


def _check_v4_public_transfer_evidence(
    exploration: LivePackageAgentRun | None,
    publication: LivePackageAgentRun | None,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> LiveV4DoneGateCheck:
    """Split full exploration coverage from selected publication freshness."""

    if exploration is None or publication is None:
        return LiveV4DoneGateCheck(
            name="icom_exploration_and_publication_evidence",
            passed=False,
            summary="缺少探索或发布运行，无法分层验证 iCom 官方公共读取证据",
        )
    exploration_check = _check_icom_public_transfer_evidence(
        exploration,
        now=now,
        maximum_quote_age=maximum_quote_age,
        require_fresh_evidence=False,
    )
    errors: list[str] = []
    target_task_ids = tuple(publication.public_transfer_task_ids)
    if len(target_task_ids) != len(set(target_task_ids)):
        errors.append("发布重搜 iCom Source task id 重复")
    selected_icom_ids = {
        item.id
        for item in (
            publication.package.final_candidate.transfers if publication.package is not None else ()
        )
        if item.provider == LiveDataProvider.ICOM_PUBLIC_TRANSFER.value
    }
    if selected_icom_ids and not target_task_ids:
        errors.append("最终方案使用 iCom 组件，但发布重搜没有对应公共 Source")
    if target_task_ids:
        coverage = publication.public_transfer_coverage
        if (
            coverage is None
            or not coverage.requested
            or not coverage.enabled
            or not coverage.complete
            or tuple(coverage.expected_source_ids) != target_task_ids
            or set(coverage.successful_source_ids) != set(target_task_ids)
            or coverage.failed_source_ids
        ):
            errors.append("发布重搜 iCom coverage 未精确覆盖实际选中路线")

    graph_by_id = {task.id: task for task in publication.scheduler.graph.tasks}
    results_by_id: dict[str, list[AgentTaskResult]] = {}
    for result in publication.scheduler.results:
        results_by_id.setdefault(result.task_id, []).append(result)
    converted_ids: set[str] = set()
    usable_by_task: dict[str, int] = {}
    for task_id in target_task_ids:
        task = graph_by_id.get(task_id)
        task_results = results_by_id.get(task_id, [])
        if task is None or len(task_results) != 1:
            errors.append(f"{task_id}: 发布 iCom Source 未唯一绑定 graph/result")
            continue
        try:
            query = IComTransferQuery.model_validate(task.input.get("icom_query"))
            parsed_result = IComTransferSearchResult.model_validate(
                task_results[0].output.get("result")
            )
        except (TypeError, ValueError, AttributeError):
            errors.append(f"{task_id}: 发布 iCom 查询或结果合同无效")
            continue
        task_errors, usable_count, _ = _icom_result_errors(
            task_id,
            parsed_result,
            query,
            now=now,
            maximum_quote_age=maximum_quote_age,
            require_fresh_evidence=True,
        )
        errors.extend(task_errors)
        usable_by_task[task_id] = usable_count
        for option in parsed_result.options:
            converted = to_package_transfer_option(option, adults=query.adults)
            if converted is not None:
                converted_ids.add(converted.id)
    missing_selected = selected_icom_ids - converted_ids
    if missing_selected:
        errors.append(
            "发布方案 iCom 组件未绑定本轮新官方结果：" + ",".join(sorted(missing_selected))
        )

    passed = exploration_check.passed and not errors
    return LiveV4DoneGateCheck(
        name="icom_exploration_and_publication_evidence",
        passed=passed,
        summary=(
            "探索运行保留 iCom 4/4 官方证据，发布运行仅重查并绑定最终使用路线"
            if passed
            else "；".join(
                (
                    *(() if exploration_check.passed else (exploration_check.summary,)),
                    *errors,
                )
            )
        ),
        evidence={
            "exploration_full_coverage": cast(
                JsonValue,
                exploration_check.model_dump(mode="json"),
            ),
            "publication_target_task_ids": list(target_task_ids),
            "publication_selected_icom_component_ids": sorted(selected_icom_ids),
            "publication_converted_component_ids": sorted(converted_ids),
            "publication_usable_options_by_task": usable_by_task,
            "publication_errors": errors,
        },
    )


def _check_all_recommended_publication_closures(
    run: FlexibleLiveAgentRun,
    *,
    now: datetime,
) -> LiveV4DoneGateCheck:
    """Deep-check P/V/R/ReV/controller and budget for every final option."""

    errors: list[str] = []
    evidence_by_option: dict[str, JsonValue] = {}
    require_publication_scope = getattr(run, "publication_refresh_minimum_options", 0) > 0
    ranked_by_id = {item.option_id: item for item in run.ranked_options}
    for option_id in run.recommended_option_ids:
        ranked = ranked_by_id.get(option_id)
        matching = tuple(
            execution
            for execution in run.pair_runs
            if ranked is not None and execution.date_pair.id == ranked.date_pair_id
        )
        if ranked is None or len(matching) != 1 or matching[0].run is None:
            errors.append(f"{option_id}: 无法唯一定位发布运行")
            continue
        execution = matching[0]
        publication = execution.run
        assert publication is not None
        exploration = execution.exploration_run or publication
        if (
            require_publication_scope
            and publication.evidence_scope != LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH
        ):
            errors.append(f"{option_id}: 深检对象不是发布组件刷新运行")
            continue
        planner_check = _check_planner_verifier_repair(publication)
        budget_check = _check_budget_and_evidence(publication, now=now)
        public_transfer_check = _check_v4_public_transfer_evidence(
            exploration,
            publication,
            now=now,
            maximum_quote_age=_PUBLICATION_QUOTE_TTL,
        )
        if not planner_check.passed:
            errors.append(f"{option_id}: {planner_check.summary}")
        if not budget_check.passed:
            errors.append(f"{option_id}: {budget_check.summary}")
        if not public_transfer_check.passed:
            errors.append(f"{option_id}: {public_transfer_check.summary}")
        evidence_by_option[option_id] = cast(
            JsonValue,
            {
                "evidence_scope": publication.evidence_scope.value,
                "planner_verifier_repair": planner_check.model_dump(mode="json"),
                "budget_and_selected_evidence": budget_check.model_dump(mode="json"),
                "public_transfer_evidence": public_transfer_check.model_dump(mode="json"),
            },
        )
    if not run.recommended_option_ids:
        errors.append("没有最终推荐方案可执行全量发布闭环深检")
    passed = not errors
    return LiveV4DoneGateCheck(
        name="all_recommended_publication_closures",
        passed=passed,
        summary=(
            f"{len(run.recommended_option_ids)} 个最终方案均通过 P/V/R/ReV/主控与预算证据深检"
            if passed
            else "；".join(errors)
        ),
        evidence={"options": evidence_by_option, "errors": errors},
    )


def _missing_v3_check(check_id: str, summary: str) -> LiveDoneGateCheck:
    return LiveDoneGateCheck(
        id=check_id,
        passed=False,
        summary=summary,
    )


def _adapt_v3_check(check: LiveDoneGateCheck) -> LiveV4DoneGateCheck:
    return LiveV4DoneGateCheck(
        name=check.id,
        passed=check.passed,
        summary=check.summary,
        evidence=check.evidence,
    )


def _v4_terminal_outcome_is_verified(
    initial: LivePackageAgentRun,
    snapshot: BrowserTaskSnapshot,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> bool:
    if snapshot.kind == BrowserVertical.LODGING:
        matching_outcomes = tuple(
            outcome
            for outcome in initial.stay_plan_inventory_outcomes
            if outcome.state
            in {
                StayInventoryResultState.CONFIRMED_EMPTY,
                StayInventoryResultState.BOUNDED_NO_EXACT_QUOTE,
                StayInventoryResultState.BOUNDED_PROVIDER_PENDING,
            }
            and f"browser-task:{snapshot.id}" in outcome.evidence_refs
        )
        expected_options = {
            key: value
            for key in (
                "expected_lodging_place_key",
                "expected_package_area",
                "segment",
            )
            if isinstance((value := snapshot.query.options.get(key)), str) and value
        }
        return (
            len(matching_outcomes) == 1
            and len(expected_options) == 3
            and matching_outcomes[0].exact_place_key.value
            == expected_options["expected_lodging_place_key"]
            and _verified_v4_lodging_inventory_receipt(
                snapshot,
                matching_outcomes[0],
                expected_options=expected_options,
                now=now,
                maximum_quote_age=maximum_quote_age,
                require_fresh=True,
            )
        )

    if snapshot.kind != BrowserVertical.FLIGHT:
        return False
    matching_flight_outcomes = tuple(
        outcome
        for outcome in initial.flight_search_outcomes
        if outcome.raw_snapshot_id == snapshot.id
        and outcome.state
        in {
            FlightSearchOutcomeState.COMPARISON_PRICE_ONLY,
            FlightSearchOutcomeState.BOUNDED_NO_EXACT_QUOTE,
        }
    )
    if len(matching_flight_outcomes) != 1 or snapshot.failure is None:
        return False
    outcome = matching_flight_outcomes[0]
    raw_receipt = snapshot.failure.details.get("flight_search_receipt")
    sealed_sha = snapshot.failure.details.get("flight_search_receipt_sha256")
    if not isinstance(raw_receipt, dict) or not isinstance(sealed_sha, str):
        return False
    try:
        receipt = FlightSearchReceipt.model_validate(raw_receipt)
    except ValueError:
        return False
    expected_state = (
        FlightSearchReceiptState.COMPARISON_PRICE_ONLY
        if outcome.state == FlightSearchOutcomeState.COMPARISON_PRICE_ONLY
        else FlightSearchReceiptState.BOUNDED_NO_EXACT_QUOTE
    )
    return (
        _verified_browser_terminal_receipt(snapshot)
        and outcome.flight_search_receipt_sha256 == sealed_sha
        and receipt.state == expected_state
        and receipt.captured_at <= now
        and now - receipt.captured_at <= maximum_quote_age
    )


def _publication_retry_recovery_errors(
    *,
    primary_task_id: str,
    retry_task_id: str,
    source_task_ids: set[str],
    graph_by_id: dict[str, AgentTask],
    snapshots_by_task_id: dict[str, BrowserTaskSnapshot],
    submissions_by_task_id: dict[str, BrowserTaskSubmission],
    now: datetime,
    maximum_quote_age: timedelta,
) -> tuple[str, ...]:
    errors: list[str] = []
    primary_task = graph_by_id.get(primary_task_id)
    retry_task = graph_by_id.get(retry_task_id)
    primary_snapshot = snapshots_by_task_id.get(primary_task_id)
    retry_snapshot = snapshots_by_task_id.get(retry_task_id)
    primary_submission = submissions_by_task_id.get(primary_task_id)
    retry_submission = submissions_by_task_id.get(retry_task_id)
    if primary_task_id == retry_task_id or primary_task_id not in source_task_ids:
        errors.append("publication_retry_of 未指向同一证据范围内的首个 Source")
    if (
        primary_task is None
        or retry_task is None
        or primary_snapshot is None
        or retry_snapshot is None
        or primary_submission is None
        or retry_submission is None
    ):
        errors.append("publication retry lineage 缺少任务、submission 或 snapshot")
        return tuple(errors)
    failover_fields = (
        "publication_failover_vertical",
        "publication_failover_from_provider",
        "publication_failover_seed_quote_id",
    )
    if primary_task.input.get("publication_retry_of") is not None or any(
        primary_task.input.get(field) is not None for field in failover_fields
    ):
        errors.append("publication_retry_of 必须指向非 retry、非 failover 的首个 Source")
    if (
        retry_task.input.get("publication_retry_vertical") != retry_submission.kind.value
        or any(retry_task.input.get(field) is not None for field in failover_fields)
        or retry_task.dependencies != ("normalize-publication-primary",)
    ):
        errors.append("retry 任务未声明独立且有界的 publication retry 合同")
    if (
        primary_submission.provider != retry_submission.provider
        or primary_submission.kind != retry_submission.kind
        or primary_submission.query != retry_submission.query
    ):
        errors.append("retry 与首个 Source 的 provider、vertical 或精确查询不一致")
    if (
        primary_snapshot.provider != primary_submission.provider
        or primary_snapshot.kind != primary_submission.kind
        or primary_snapshot.query != primary_submission.query
        or retry_snapshot.provider != retry_submission.provider
        or retry_snapshot.kind != retry_submission.kind
        or retry_snapshot.query != retry_submission.query
    ):
        errors.append("Source snapshot 未精确绑定各自 graph submission")
    if (
        primary_snapshot.state != BrowserTaskState.FAILED
        or primary_snapshot.failure is None
        or primary_snapshot.failure.code != BrowserFailureCode.DOM_DRIFT
    ):
        errors.append("只有首个 dom_drift Source 才允许由 retry 恢复")
    if (
        primary_snapshot.claimed_by is None
        or primary_snapshot.claimed_at is None
        or retry_snapshot.claimed_by is None
        or retry_snapshot.claimed_at is None
    ):
        errors.append("首个 Source 或 retry 缺少真实 Companion claim")
    elif retry_snapshot.claimed_at < primary_snapshot.updated_at:
        errors.append("retry claim 早于首个 Source 的终态，attempt 顺序无效")
    if (
        retry_snapshot.state != BrowserTaskState.SUCCEEDED
        or retry_snapshot.failure is not None
        or not retry_snapshot.quotes
        or retry_snapshot.reused_from_task_id is not None
        or retry_snapshot.id == primary_snapshot.id
    ):
        errors.append("retry 未形成独立成功报价 snapshot")
    retry_quote_check = _check_real_browser_evidence(
        (retry_snapshot,),
        (),
        now=now,
        maximum_quote_age=maximum_quote_age,
        check_id="publication_retry_quote_evidence",
    )
    if not retry_quote_check.passed:
        errors.append("retry 报价未通过生产解析器、可见 SHA、查询或新鲜度验证")
    return tuple(errors)


def _check_selected_v4_runtime_evidence(
    initial: LivePackageAgentRun | None,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> LiveV4DoneGateCheck:
    if initial is None:
        return LiveV4DoneGateCheck(
            name="real_v4_browser_source_evidence",
            passed=False,
            summary="缺少选中初始运行，无法验证真实浏览器 Source 证据",
        )
    source_task_ids = tuple(initial.source_task_ids)
    source_task_id_set = set(source_task_ids)
    errors: list[str] = []
    if len(source_task_ids) != len(source_task_id_set):
        errors.append("Source task id 存在重复，无法建立唯一 attempt lineage")
    graph_by_id = {task.id: task for task in initial.scheduler.graph.tasks}
    results_by_task_id: dict[str, list[AgentTaskResult]] = {}
    for result in initial.scheduler.results:
        if result.task_id in source_task_id_set:
            results_by_task_id.setdefault(result.task_id, []).append(result)
    snapshots_by_task_id: dict[str, BrowserTaskSnapshot] = {}
    submissions_by_task_id: dict[str, BrowserTaskSubmission] = {}
    for task_id in source_task_ids:
        task = graph_by_id.get(task_id)
        matching_results = results_by_task_id.get(task_id, [])
        if task is None:
            errors.append(f"{task_id}: Source task 不在冻结 graph 中")
            continue
        if len(matching_results) != 1:
            errors.append(f"{task_id}: 必须唯一关联一个 Source task result")
            continue
        try:
            submission = BrowserTaskSubmission.model_validate(task.input.get("submission"))
        except ValueError:
            errors.append(f"{task_id}: graph submission 结构无效")
            continue
        raw_snapshot = matching_results[0].output.get("snapshot")
        if raw_snapshot is None:
            errors.append(f"{task_id}: 缺少原始浏览器 snapshot")
            continue
        try:
            snapshot = BrowserTaskSnapshot.model_validate(raw_snapshot)
        except ValueError:
            errors.append(f"{task_id}: 原始浏览器 snapshot 结构无效")
            continue
        submissions_by_task_id[task_id] = submission
        snapshots_by_task_id[task_id] = snapshot
        if (
            snapshot.provider != submission.provider
            or snapshot.kind != submission.kind
            or snapshot.query != submission.query
        ):
            errors.append(
                f"{task_id}: snapshot provider、vertical 或精确查询与 graph submission 不一致"
            )

    retry_task_ids_by_primary: dict[str, list[str]] = {}
    retry_lineage_errors: list[str] = []
    for task_id in source_task_ids:
        task = graph_by_id.get(task_id)
        if task is None:
            continue
        raw_primary_task_id = task.input.get("publication_retry_of")
        if raw_primary_task_id is None:
            continue
        if not isinstance(raw_primary_task_id, str) or not raw_primary_task_id:
            retry_lineage_errors.append(
                f"{task_id}: publication_retry_of 必须是非空 Source task id"
            )
            continue
        retry_task_ids_by_primary.setdefault(raw_primary_task_id, []).append(task_id)

    recovered_failed_snapshot_ids: set[str] = set()
    for primary_task_id, retry_task_ids in retry_task_ids_by_primary.items():
        if len(retry_task_ids) != 1:
            retry_lineage_errors.append(f"{primary_task_id}: 必须唯一关联一次 publication retry")
            continue
        retry_task_id = retry_task_ids[0]
        lineage_errors = _publication_retry_recovery_errors(
            primary_task_id=primary_task_id,
            retry_task_id=retry_task_id,
            source_task_ids=source_task_id_set,
            graph_by_id=graph_by_id,
            snapshots_by_task_id=snapshots_by_task_id,
            submissions_by_task_id=submissions_by_task_id,
            now=now,
            maximum_quote_age=maximum_quote_age,
        )
        retry_lineage_errors.extend(f"{retry_task_id}: {error}" for error in lineage_errors)
        if lineage_errors:
            continue
        primary_snapshot = snapshots_by_task_id.get(primary_task_id)
        assert primary_snapshot is not None
        recovered_failed_snapshot_ids.add(primary_snapshot.id)

    errors.extend(retry_lineage_errors)
    snapshots = tuple(
        snapshots_by_task_id[task_id]
        for task_id in source_task_ids
        if task_id in snapshots_by_task_id
    )
    verified_terminal_snapshot_ids = {
        snapshot.id
        for snapshot in snapshots
        if snapshot.state != BrowserTaskState.SUCCEEDED
        and _v4_terminal_outcome_is_verified(
            initial,
            snapshot,
            now=now,
            maximum_quote_age=maximum_quote_age,
        )
    }
    successful = tuple(
        snapshot for snapshot in snapshots if snapshot.state == BrowserTaskState.SUCCEEDED
    )
    for snapshot in snapshots:
        if snapshot.claimed_by is None or snapshot.claimed_at is None:
            errors.append(f"{snapshot.id}: 缺少真实 Companion claim 证据")
        if snapshot.state == BrowserTaskState.SUCCEEDED:
            continue
        if snapshot.failure is None:
            errors.append(f"{snapshot.id}: 失败 Source 缺少结构化 failure receipt")
        elif (
            snapshot.id not in verified_terminal_snapshot_ids
            and snapshot.id not in recovered_failed_snapshot_ids
        ):
            errors.append(
                f"{snapshot.id}: 失败 Source 未形成严格四态终态，且不属于受审计 "
                "dom_drift→retry lineage"
            )
    if len(snapshots) != len(source_task_ids):
        errors.append(
            f"浏览器 snapshot 数量 {len(snapshots)} != Source 数量 {len(source_task_ids)}"
        )
    real = _check_real_browser_evidence(
        successful,
        (),
        now=now,
        maximum_quote_age=maximum_quote_age,
        check_id="real_v4_successful_browser_evidence",
    )
    passed = not errors and real.passed
    return LiveV4DoneGateCheck(
        name="real_v4_browser_source_evidence",
        passed=passed,
        summary=(
            "成功 Source 具有新鲜生产解析器报价；失败 Source 具有严格四态终态，"
            "或由同 provider/vertical/精确查询的真实 dom_drift retry 恢复"
            if passed
            else "浏览器四态终态、publication retry lineage、生产解析器或新鲜度证据不完整"
        ),
        evidence={
            "source_task_count": len(source_task_ids),
            "snapshot_count": len(snapshots),
            "successful_snapshot_count": len(successful),
            "bounded_or_empty_task_count": len(verified_terminal_snapshot_ids),
            "recovered_publication_primary_count": len(recovered_failed_snapshot_ids),
            "publication_retry_lineage_errors": retry_lineage_errors,
            "errors": errors,
            "successful_source_check": cast(
                JsonValue,
                real.model_dump(mode="json"),
            ),
        },
    )


def _check_v4_flight_search_outcomes(
    initial: LivePackageAgentRun | None,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
    require_fresh_quotes: bool = True,
) -> LiveV4DoneGateCheck:
    if initial is None:
        return LiveV4DoneGateCheck(
            name="flight_search_outcome_contract",
            passed=False,
            summary="缺少选中初始运行，无法验证机票搜索终态",
        )
    snapshots, parse_errors = _source_snapshots(initial)
    snapshots_by_id: dict[str, list[BrowserTaskSnapshot]] = {}
    for snapshot in snapshots:
        snapshots_by_id.setdefault(snapshot.id, []).append(snapshot)
    errors = list(parse_errors)
    expected_providers = {"ctrip", "qunar", "tongcheng"}
    expected_source_ids = {f"source-{provider}-flight" for provider in expected_providers}
    source_results_by_task = {
        task_id: tuple(result for result in initial.scheduler.results if result.task_id == task_id)
        for task_id in expected_source_ids
    }
    for task_id, results in source_results_by_task.items():
        if len(results) != 1:
            errors.append(f"{task_id}: 必须唯一关联一个 scheduler Source result")
    outcomes = initial.flight_search_outcomes
    if len(outcomes) != 3:
        errors.append(f"机票搜索终态数量 {len(outcomes)} != 三平台 3")
    if {outcome.provider.value for outcome in outcomes} != expected_providers:
        errors.append("机票搜索终态未精确覆盖携程、去哪儿、同程")
    if {outcome.source_task_id for outcome in outcomes} != expected_source_ids:
        errors.append("机票搜索终态未精确绑定三条 flight Source task")
    if len({outcome.raw_snapshot_id for outcome in outcomes}) != len(outcomes):
        errors.append("机票搜索终态复用了同一个原始 snapshot")

    normalizer = BrowserQuoteNormalizer()
    exact_provider_count = 0
    comparison_provider_count = 0
    exact_quote_ids: list[str] = []
    outcome_states: dict[str, str] = {}
    for outcome in outcomes:
        provider = outcome.provider
        outcome_states[provider.value] = outcome.state.value
        expected_task_id = f"source-{provider.value}-flight"
        matching_results = source_results_by_task.get(outcome.source_task_id, ())
        matching_snapshots = snapshots_by_id.get(outcome.raw_snapshot_id, [])
        if (
            outcome.source_task_id != expected_task_id
            or len(matching_results) != 1
            or len(matching_snapshots) != 1
        ):
            errors.append(
                f"{outcome.source_task_id}: 机票终态未唯一关联对应 Source result 和 snapshot"
            )
            continue
        source_result = matching_results[0]
        raw_result_snapshot = source_result.output.get("snapshot")
        if raw_result_snapshot is None:
            errors.append(f"{outcome.source_task_id}: Source result 缺少 snapshot 输出")
            continue
        try:
            snapshot = BrowserTaskSnapshot.model_validate(raw_result_snapshot)
        except ValueError:
            errors.append(f"{outcome.source_task_id}: Source result snapshot 结构无效")
            continue
        evidence = source_result.evidence
        if (
            snapshot != matching_snapshots[0]
            or snapshot.id != outcome.raw_snapshot_id
            or len(evidence) != 1
            or evidence[0].id != f"evidence:{outcome.source_task_id}"
            or evidence[0].topic != "browser_result"
            or evidence[0].subject != outcome.source_task_id
            or evidence[0].payload != source_result.output
            or evidence[0].source != "tripchord-live-agent-system"
        ):
            errors.append(
                f"{outcome.source_task_id}: scheduler result、EvidenceRecord、"
                "snapshot 与 outcome 链接不一致"
            )
            continue
        query = snapshot.query
        try:
            trusted_contract = trusted_search_url_contract(
                provider,
                BrowserVertical.FLIGHT,
                query,
            )
        except ValueError:
            trusted_contract = None
        if (
            trusted_contract is None
            or snapshot.provider != provider
            or snapshot.kind != BrowserVertical.FLIGHT
            or query.origin is None
            or query.end_date is None
            or query.origin_code is None
            or query.destination_code is None
            or f"browser-task:{snapshot.id}" not in outcome.evidence_refs
        ):
            errors.append(f"{outcome.source_task_id}: 平台、精确往返查询或受信 URL 契约不匹配")
            continue

        if outcome.state == FlightSearchOutcomeState.QUOTE_FOUND:
            exact_provider_count += 1
            if snapshot.state != BrowserTaskState.SUCCEEDED or snapshot.failure is not None:
                errors.append(f"{outcome.source_task_id}: QUOTE_FOUND 未关联成功 snapshot")
                continue
            for quote_id, normalization_ref, raw_sha in zip(
                outcome.quote_ids,
                outcome.normalization_result_refs,
                outcome.raw_quote_evidence_sha256s,
                strict=True,
            ):
                expected_ref = f"normalization-result:{outcome.source_task_id}:{quote_id}"
                normalized_matches = tuple(
                    result
                    for result in initial.normalization_results
                    if result.usable
                    and result.provider == provider.value
                    and result.kind == BrowserVertical.FLIGHT
                    and isinstance(result.quote, NormalizedFlightQuote)
                    and result.quote.id == quote_id
                )
                raw_matches = tuple(
                    raw
                    for raw in snapshot.quotes
                    if raw.provider == provider
                    and raw.kind == BrowserVertical.FLIGHT
                    and raw.evidence_sha256 == raw_sha
                )
                if (
                    normalization_ref != expected_ref
                    or len(normalized_matches) != 1
                    or len(raw_matches) != 1
                ):
                    errors.append(
                        f"{outcome.source_task_id}: 报价未形成唯一 raw→normalization→quote 交叉链接"
                    )
                    continue
                result = normalized_matches[0]
                raw = raw_matches[0]
                quote = result.quote
                assert isinstance(quote, NormalizedFlightQuote)
                visible_sha = hashlib.sha256(raw.visible_evidence.encode()).hexdigest()
                if (
                    visible_sha != raw.evidence_sha256
                    or normalizer.normalize(raw, query) != result
                    or quote.provider != provider.value
                    or quote.currency != query.currency
                    or quote.origin != query.origin
                    or quote.destination != query.destination
                    or quote.adults != query.adults
                    or quote.outbound_depart_at.date() != query.start_date
                    or quote.return_depart_at.date() != query.end_date
                    or quote.captured_at != raw.captured_at
                    or quote.captured_at > now
                    or (
                        require_fresh_quotes
                        and (not quote.is_fresh(now) or now - quote.captured_at > maximum_quote_age)
                    )
                ):
                    errors.append(
                        f"{outcome.source_task_id}: 报价 SHA、查询、日期、人数、"
                        "币种或新鲜度交叉验证失败"
                    )
                    continue
                exact_quote_ids.append(quote.id)
        else:
            failure = snapshot.failure
            raw_receipt = (
                failure.details.get("flight_search_receipt") if failure is not None else None
            )
            sealed_sha = (
                failure.details.get("flight_search_receipt_sha256") if failure is not None else None
            )
            if (
                snapshot.state != BrowserTaskState.FAILED
                or failure is None
                or failure.code != BrowserFailureCode.EXTRACTION_ERROR
                or failure.retryable
                or not isinstance(raw_receipt, dict)
                or not isinstance(sealed_sha, str)
            ):
                errors.append(f"{outcome.source_task_id}: 非精确终态不是合法、不可重试的语义回执")
                continue
            try:
                receipt = FlightSearchReceipt.model_validate(raw_receipt)
            except ValueError:
                errors.append(
                    f"{outcome.source_task_id}: tripchord-flight-search-receipt-v1 结构无效"
                )
                continue
            confirmed = receipt.confirmed_query
            expected_receipt_state = (
                FlightSearchReceiptState.COMPARISON_PRICE_ONLY
                if outcome.state == FlightSearchOutcomeState.COMPARISON_PRICE_ONLY
                else FlightSearchReceiptState.BOUNDED_NO_EXACT_QUOTE
            )
            if (
                flight_search_receipt_sha256(raw_receipt) != sealed_sha
                or outcome.flight_search_receipt_sha256 != sealed_sha
                or receipt.provider != provider
                or receipt.state != expected_receipt_state
                or receipt.scan_limit != outcome.scan_limit
                or receipt.scanned_count != outcome.scanned_count
                or receipt.price_bearing_candidate_count != outcome.price_bearing_candidate_count
                or confirmed.origin != query.origin
                or confirmed.destination != query.destination
                or confirmed.start_date != query.start_date
                or confirmed.end_date != query.end_date
                or confirmed.adults != query.adults
                or confirmed.origin_code != query.origin_code
                or confirmed.destination_code != query.destination_code
                or any(
                    candidate.currency != query.currency
                    for candidate in receipt.candidate_summaries
                    if candidate.price_bearing
                )
                or receipt.page_url != failure.page_url
                or receipt.captured_at != failure.captured_at
                or receipt.captured_at > now
                or (require_fresh_quotes and now - receipt.captured_at > maximum_quote_age)
            ):
                errors.append(
                    f"{outcome.source_task_id}: 非精确回执 SHA、分类、查询、"
                    "代码、日期、人数或新鲜度失败"
                )
                continue
            if outcome.state == FlightSearchOutcomeState.COMPARISON_PRICE_ONLY:
                comparison_provider_count += 1

    inventory_flight_ids = tuple(quote.id for quote in initial.inventory.flights)
    if len(inventory_flight_ids) != len(set(inventory_flight_ids)) or set(
        inventory_flight_ids
    ) != set(exact_quote_ids):
        errors.append(
            "Planner inventory 未精确等于 QUOTE_FOUND 的可用 NormalizedFlightQuote；"
            "比较价或有界未命中可能被混入"
        )
    final_flight_id = (
        initial.package.final_candidate.flight.id if initial.package is not None else None
    )
    if final_flight_id is not None and final_flight_id not in set(exact_quote_ids):
        errors.append("Planner/主控最终整包选择了非 QUOTE_FOUND 机票")
    if exact_provider_count < 1:
        errors.append("三平台均未形成 QUOTE_FOUND，不能生成可发布的机票规划")
    price_bearing_provider_count = exact_provider_count + comparison_provider_count
    if price_bearing_provider_count < 2:
        errors.append("少于两家平台具有 QUOTE_FOUND 或合法比较价证据")

    passed = not errors
    return LiveV4DoneGateCheck(
        name="flight_search_outcome_contract",
        passed=passed,
        summary=(
            "三平台均形成合法机票搜索终态，至少一家最终报价、至少两家含价格证据；"
            "比较价不进入 Planner、预算或最终整包"
            if passed
            else "；".join(errors)
        ),
        evidence={
            "provider_outcome_states": outcome_states,
            "exact_provider_count": exact_provider_count,
            "comparison_provider_count": comparison_provider_count,
            "price_bearing_provider_count": price_bearing_provider_count,
            "inventory_flight_ids": list(inventory_flight_ids),
            "exact_quote_ids": exact_quote_ids,
            "final_flight_id": final_flight_id,
            "booking_boundary": ("只读搜索证据；没有下单、支付、可预订承诺或库存锁定"),
            "errors": errors,
        },
    )


def _check_v4_observed_overlap(
    initial: LivePackageAgentRun | None,
) -> LiveV4DoneGateCheck:
    if initial is None:
        return LiveV4DoneGateCheck(
            name="observed_cross_platform_overlap",
            passed=False,
            summary="缺少选中初始运行，无法验证三平台真实并发重叠",
        )
    snapshots, parse_errors = _source_snapshots(initial)
    overlap = _check_actual_overlap(snapshots)
    adapted = _adapt_v3_check(overlap)
    if parse_errors:
        return adapted.model_copy(
            update={
                "passed": False,
                "summary": "Source snapshot 解析失败，不能声明真实并发重叠",
                "evidence": {
                    **adapted.evidence,
                    "snapshot_parse_errors": list(parse_errors),
                },
            }
        )
    return adapted


def _check_v4_strict_selected_coverage(
    initial: LivePackageAgentRun | None,
    candidate_set: StayPlanCandidateSet,
) -> LiveV4DoneGateCheck:
    errors: list[str] = []
    if initial is None:
        errors.append("缺少选中初始运行")
    elif initial.selected_stay_plan_id is None:
        errors.append("选中运行缺少冻结住宿方案身份")
    else:
        if initial.mode != LiveCoverageMode.STRICT:
            errors.append("选中运行不是 strict 模式")
        if not initial.all_platforms_complete:
            errors.append("选中方案未形成三平台完成态")
        providers = {item.provider.value for item in initial.coverage}
        if providers != {"ctrip", "qunar", "tongcheng"}:
            errors.append("覆盖回执不是携程、去哪儿、同程三平台")
        for item in initial.coverage:
            lodging_enabled = item.provider in {
                BrowserProvider.CTRIP,
                BrowserProvider.QUNAR,
            }
            expected_per_provider = 1 + (
                len(candidate_set.candidate(initial.selected_stay_plan_id).segments)
                if lodging_enabled
                else 0
            )
            if item.selected_stay_plan_id != initial.selected_stay_plan_id:
                errors.append(f"{item.provider.value}: 覆盖回执方案身份不一致")
            selected_segments = candidate_set.candidate(initial.selected_stay_plan_id).segments
            expected_source_ids = {
                f"source-{item.provider.value}-flight",
                *(
                    f"source-{item.provider.value}-lodging-{segment.query_segment}"
                    for segment in selected_segments
                    if lodging_enabled
                ),
            }
            provider_outcomes = tuple(
                outcome
                for outcome in initial.flight_search_outcomes
                if outcome.provider == item.provider
            )
            if (
                not item.complete
                or item.failed_source_ids
                or len(item.terminal_outcome_source_ids) != expected_per_provider
                or set(item.terminal_outcome_source_ids) != expected_source_ids
                or item.usable_quote_source_ids != item.successful_source_ids
                or not set(item.usable_quote_source_ids) <= set(item.terminal_outcome_source_ids)
                or set(item.completed_search_verticals)
                != (
                    {BrowserVertical.FLIGHT, BrowserVertical.LODGING}
                    if lodging_enabled
                    else {BrowserVertical.FLIGHT}
                )
                or len(provider_outcomes) != 1
                or item.flight_outcome_state != provider_outcomes[0].state
            ):
                errors.append(f"{item.provider.value}: 选中方案未形成已启用能力的完整终态")
    return LiveV4DoneGateCheck(
        name="strict_selected_plan_platform_coverage",
        passed=not errors,
        summary=(
            "三平台机票完成，且携程、去哪儿完成选中住宿方案逐分段有界搜索"
            if not errors
            else "；".join(errors)
        ),
    )


def _check_v4_event_chain(
    initial: LivePackageAgentRun | None,
    event: LiveEventReplanRun | None,
    candidate_set: StayPlanCandidateSet,
    *,
    now: datetime,
    maximum_quote_age: timedelta,
) -> LiveV4DoneGateCheck:
    if initial is None or event is None:
        return LiveV4DoneGateCheck(
            name="event_injection_repair_reverify_master",
            passed=False,
            summary="缺少真实事件注入后的局部重查与重规划运行",
        )
    event_snapshots, event_snapshot_errors = _event_source_snapshots(event)
    dynamic = _check_event_replan(
        initial,
        event,
        event_snapshots,
        event_snapshot_errors,
        now=now,
        maximum_quote_age=maximum_quote_age,
    )
    read_only = _check_read_only_graph(initial, event)
    party = _check_selected_party_availability(initial, event)
    errors: list[str] = []
    if not dynamic.passed:
        errors.append("事件重查、Repair、EVENT_REVERIFICATION 或主控裁决未通过")
    if not read_only.passed:
        errors.append("事件 DAG 超出只读工具边界")
    if not party.passed:
        errors.append("事件前后请求人数库存证据不完整")
    target_errors = _v4_event_target_errors(
        initial,
        event,
        candidate_set,
        event_snapshots,
        event_snapshot_errors,
        now=now,
        maximum_quote_age=maximum_quote_age,
    )
    errors.extend(target_errors)
    initial_package = initial.package
    event_package = event.package
    initial_plan = initial.selected_stay_plan_id
    final_plan = (
        stay_plan_for_candidate(
            candidate_set,
            initial.intent,
            event_package.final_candidate,
        )
        if event_package is not None
        else None
    )
    if initial_package is None or event_package is None:
        errors.append("事件前后缺少可审计整包")
    elif initial_plan is None or final_plan != initial_plan:
        errors.append("事件 Repair 改变了冻结住宿方案或精确地点")
    return LiveV4DoneGateCheck(
        name="event_injection_repair_reverify_master",
        passed=not errors,
        summary=(
            "事件仅重查受影响平台组件，Repair 经 ReVerifier 后由主控裁决且保持冻结住宿方案"
            if not errors
            else "；".join(errors)
        ),
        evidence={
            "dynamic_replan": cast(JsonValue, dynamic.model_dump(mode="json")),
            "read_only_graph": cast(JsonValue, read_only.model_dump(mode="json")),
            "party_availability": cast(JsonValue, party.model_dump(mode="json")),
            "initial_stay_plan_id": (initial_plan.value if initial_plan is not None else None),
            "event_final_stay_plan_id": (final_plan.value if final_plan is not None else None),
            "exact_event_target_errors": list(target_errors),
        },
    )


def _v4_event_target_errors(
    initial: LivePackageAgentRun,
    event: LiveEventReplanRun,
    candidate_set: StayPlanCandidateSet,
    snapshots: tuple[BrowserTaskSnapshot, ...],
    snapshot_errors: tuple[str, ...],
    *,
    now: datetime | None = None,
    maximum_quote_age: timedelta | None = None,
) -> tuple[str, ...]:
    errors = list(snapshot_errors)
    expected_provider = event.event.affected_provider
    if event.requeried_providers != (expected_provider,):
        errors.append("事件重查 provider 必须与 affected_provider 完全一致")
    if len(event.source_task_ids) != 1:
        errors.append("事件必须且只能生成一个 Source task")
        return tuple(errors)
    if len(snapshots) != 1:
        errors.append("事件 Source task 必须且只能解析出一个原始 snapshot")
        return tuple(errors)
    source_task_id = event.source_task_ids[0]
    snapshot = snapshots[0]
    if (
        snapshot.provider.value != expected_provider.value
        or snapshot.kind != BrowserVertical.LODGING
    ):
        errors.append("事件 snapshot provider 或 vertical 与受影响住宿组件不一致")
    package = initial.package
    if package is None:
        errors.append("事件前缺少整包，无法解析精确住宿目标")
        return tuple(errors)
    target_matches = tuple(
        lodging
        for lodging in package.final_candidate.lodgings
        if lodging.id == event.event.target_component_id
    )
    if len(target_matches) != 1:
        errors.append("事件 target_component_id 必须唯一指向初始整包住宿")
        return tuple(errors)
    target = target_matches[0]
    if target.provider != expected_provider.value:
        errors.append("事件 affected_provider 不拥有目标住宿报价")
    segment_matches = tuple(
        segment
        for plan in candidate_set.candidates
        for segment in plan.segments
        if target.place_key == segment.exact_place_key
        and target.area == segment.area
        and target.check_in == segment.check_in.resolve(initial.intent)
        and target.check_out == segment.check_out.resolve(initial.intent)
    )
    if len(segment_matches) != 1:
        errors.append("事件目标未唯一匹配冻结住宿分段")
        return tuple(errors)
    segment = segment_matches[0]
    expected_task_id = f"event-source-{expected_provider.value}-lodging-{segment.query_segment}"
    if source_task_id != expected_task_id:
        errors.append("事件 source_task_id 未绑定目标冻结分段")
    options = snapshot.query.options
    if (
        snapshot.query.start_date != target.check_in
        or snapshot.query.end_date != target.check_out
        or snapshot.query.adults != target.adults
        or snapshot.query.rooms != target.rooms
        or options.get("expected_lodging_place_key") != segment.exact_place_key.value
        or options.get("expected_package_area") != segment.area.value
        or options.get("segment") != segment.query_segment
    ):
        errors.append("事件 snapshot 未精确复用目标分段的日期、地点、人数或房间数")
    errors.extend(
        _v4_event_quote_binding_errors(
            event,
            snapshot,
            now=now,
            maximum_quote_age=maximum_quote_age,
        )
    )
    return tuple(errors)


def _v4_event_quote_binding_errors(
    event: LiveEventReplanRun,
    snapshot: BrowserTaskSnapshot,
    *,
    now: datetime | None,
    maximum_quote_age: timedelta | None,
) -> tuple[str, ...]:
    errors: list[str] = []
    if snapshot.state != BrowserTaskState.SUCCEEDED or not snapshot.quotes:
        return ("事件 snapshot 必须成功且至少包含一个原始住宿报价",)
    if any(
        raw.kind != BrowserVertical.LODGING or raw.provider != snapshot.provider
        for raw in snapshot.quotes
    ):
        return ("事件原始报价的 provider 或 vertical 与 snapshot 不一致",)
    try:
        recomputed_results = BrowserQuoteNormalizer().normalize_many(
            snapshot.quotes,
            snapshot.query,
        )
    except Exception:
        return ("事件原始住宿报价全量重新归一化时结构校验失败",)
    if event.normalization_results != recomputed_results:
        errors.append("事件 normalization_results 未按顺序完整等于全部原始报价重算结果")
    package = event.package
    handoff = package.event_handoff if package is not None else None
    diff = package.diff if package is not None else None
    if package is None or handoff is None or diff is None:
        errors.append("事件整包缺少 replacement handoff 或组件差异")
        return tuple(errors)
    package_event = handoff.repair.event
    replacement_matches = tuple(
        result.quote
        for result in recomputed_results
        if result.usable
        and isinstance(result.quote, NormalizedLodgingQuote)
        and result.quote.id == package_event.replacement_component_id
    )
    if len(replacement_matches) != 1:
        errors.append("handoff replacement_component_id 未唯一定位一条可用原始住宿报价")
        return tuple(errors)
    normalized = replacement_matches[0]
    if (
        normalized.provider != snapshot.provider.value
        or normalized.check_in != snapshot.query.start_date
        or normalized.check_out != snapshot.query.end_date
        or normalized.adults != snapshot.query.adults
        or normalized.rooms != snapshot.query.rooms
        or normalized.place_key is None
        or normalized.place_key.value != snapshot.query.options.get("expected_lodging_place_key")
        or normalized.area.value != snapshot.query.options.get("expected_package_area")
        or (
            now is not None
            and maximum_quote_age is not None
            and (not normalized.is_fresh(now) or now - normalized.captured_at > maximum_quote_age)
        )
    ):
        errors.append("事件归一化报价的 provider、分段、地点、日期、人数或新鲜度不匹配")
    added_ids = diff.added_component_ids
    if (
        len(added_ids) != 1
        or added_ids[0] != normalized.id
        or package_event.replacement_component_id != normalized.id
    ):
        errors.append("事件归一化报价 ID 未唯一绑定 handoff 与新增组件")
    final_matches = tuple(
        lodging for lodging in package.final_candidate.lodgings if lodging.id == normalized.id
    )
    inventory_matches = tuple(
        lodging for lodging in event.inventory.lodgings if lodging.id == normalized.id
    )
    if (
        len(final_matches) != 1
        or final_matches[0] != normalized
        or len(inventory_matches) != 1
        or inventory_matches[0] != normalized
    ):
        errors.append("事件最终替换住宿或事件库存未与原始报价重算结果完全一致")
    return tuple(errors)
