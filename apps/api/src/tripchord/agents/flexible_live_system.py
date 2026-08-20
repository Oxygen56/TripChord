from __future__ import annotations

import asyncio
import hashlib
import json
import os
import resource
import sys
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, cast

from pydantic import Field, JsonValue, model_validator

from tripchord.agents.adaptive_control import (
    DIRECT_DATE_PAIR_LIMIT,
    AdaptiveControlInput,
    AdaptiveModelConcurrencyGate,
    AdaptiveStopReason,
    ProviderHealth,
    ProviderHealthStatus,
    ScaleDirective,
    derive_scale_directive,
)
from tripchord.agents.agent_budget import (
    AgentBudgetAudit,
    current_agent_budget,
    request_agent_budgeted,
)
from tripchord.agents.agent_templates import AgentTemplatePlan, build_agent_template_plan
from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.context_budget import BudgetedAgentContextBuilder, ContextPurpose
from tripchord.agents.live_advisory import (
    AgenticRunSummary,
    QueryStrategyProposal,
    StructuredLiveModelAgent,
    proposal_from_result,
)
from tripchord.agents.live_jobs import (
    LivePlanningPairCheckpoint,
    LivePlanningPairCheckpointState,
)
from tripchord.agents.live_system import (
    ExactQuoteComparisonCoverage,
    LiveCoverageMode,
    LiveEvidenceScope,
    LiveFinalizationState,
    LivePackageAgentRun,
    LivePackageAgentSystem,
    LiveRunPurpose,
    SourceExecutionCompleteness,
)
from tripchord.agents.memory import MemoryAccessContext, MemoryStore
from tripchord.agents.model_gateway import ModelRouter
from tripchord.agents.models import AgentRole, AgentTask, PreferenceMode, ToolPermission
from tripchord.agents.stay_area import (
    StayAreaSearchProfile,
    system_stay_area_search_profile,
)
from tripchord.agents.tools import ToolCall, ToolRegistry, ToolSpec
from tripchord.domain.common import DomainModel
from tripchord.planning.adaptive_dates import (
    AdaptiveRefinementDecision,
    DatePairRefiner,
    ExactDatePairObservation,
    RankedTopKDateRefiner,
)
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORMS,
    AdmissibleCostBound,
    AuditableDatePair,
    DateExplorationResult,
    DateOptimalityStatus,
    FlexibleDateExplorer,
    FlexibleQueryPlan,
    FlexibleQueryPlanBuilder,
    FlexibleQueryTask,
    FlexibleTravelWindow,
    PlatformFareCalendar,
    PlatformRatePolicy,
    QueryPlanPolicy,
    QueryTaskKind,
    TravelPlatform,
    canonical_acquisition_fingerprint,
    effective_platform_interval_ms,
)
from tripchord.planning.multiobjective import (
    DecisionVector,
    ObjectiveDirection,
    ObjectiveWeight,
    ParetoTopKSelector,
)
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageDecision,
    PackageDecisionState,
    PackageIntent,
    PackagePlaceKey,
    TransferOption,
)
from tripchord.planning.stay_plans import (
    StayPlanCandidateSet,
    StayPlanId,
    system_stay_plan_candidate_set,
)
from tripchord.platform.terminal import SearchRun
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserSearchQuery,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserTaskSubmission,
)

_KIND_SUFFIX = {
    QueryTaskKind.FLIGHT: "flight",
    QueryTaskKind.LODGING_FULL_STAY: "lodging-full",
    QueryTaskKind.LODGING_FIRST_NIGHT: "lodging-first",
    QueryTaskKind.LODGING_MIDDLE_STAY: "lodging-middle",
    QueryTaskKind.LODGING_LAST_NIGHT: "lodging-last",
    QueryTaskKind.LODGING_HULHUMALE_FULL_STAY: "lodging-hulhumale-full",
}

_PUBLICATION_REFRESH_PIPELINE_TASK_IDS = (
    "plan-travel-package",
    "verify-travel-package",
    "repair-travel-package",
    "reverify-travel-package",
    "orchestrate-travel-package",
    "explain-final-decision",
    "curate-run-memory",
    "publish-live-run",
)
_EXPLORATION_DEFERRED_STAGE_IDS = (
    "explain-final-decision",
    "curate-run-memory",
    "publish-live-run",
)
_QUERY_STRATEGY_FRONTIER_LIMIT = 12
_PUBLICATION_REFRESH_MODEL_AGENT_COUNT = 8
_MAX_SOURCE_START_DELAY_MS = 900_000
_FULL_WINDOW_FINALIZATION_BUFFER_SECONDS = 60
INTERNAL_BENCHMARK_TOTAL_TIMEOUT_SECONDS = 530

PairCheckpointReporter = Callable[[LivePlanningPairCheckpoint], Awaitable[None]]


@dataclass
class _FlexibleAcquisitionEntry:
    task_id: str
    initial_snapshot: BrowserTaskSnapshot
    outcome: asyncio.Future[BrowserTaskSnapshot]
    consumer_count: int = 1
    watcher_task: asyncio.Task[None] | None = None
    terminal_snapshot: BrowserTaskSnapshot | None = None
    cleanup_forwarded: bool = False


class _FlexibleAcquisitionLedger:
    """Run-scoped singleflight for exact browser acquisitions.

    The browser bridge coalesces active work and reuses successful recent
    quotes, but a terminal failure is intentionally not reusable.  A complete
    flexible window must nevertheless treat a failed acquisition as a terminal
    outcome for every duplicate fingerprint in this same run; otherwise later
    date pairs silently re-query the same failed source.
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self._state: ContextVar[
            tuple[
                asyncio.Lock,
                dict[str, _FlexibleAcquisitionEntry],
                dict[str, _FlexibleAcquisitionEntry],
                dict[str, int],
                dict[str, BrowserTaskSnapshot],
                set[str],
            ]
            | None
        ] = ContextVar("flexible_acquisition_ledger_state", default=None)

    def reset(self) -> None:
        self._state.set(
            (
                asyncio.Lock(),
                {},
                {},
                {
                    "delegate_submit_call_count": 0,
                    "exploration_delegated_acquisitions": 0,
                    "ledger_shared_return_count": 0,
                    "publication_refresh_delegated_acquisitions": 0,
                },
                {},
                set(),
            )
        )

    def metrics(self) -> dict[str, int]:
        state = self._state.get()
        if state is None:
            return {
                "delegate_submit_call_count": 0,
                "exploration_delegated_acquisitions": 0,
                "ledger_shared_return_count": 0,
                "publication_refresh_delegated_acquisitions": 0,
            }
        return dict(state[3])

    def detailed_metrics(self) -> dict[str, int]:
        """Return scheduler counts plus terminal browser-attempt semantics."""
        state = self._state.get()
        if state is None:
            return {
                **self.metrics(),
                "platform_acquisition_attempt_count": 0,
                "publication_refresh_platform_acquisition_attempt_count": 0,
                "recent_quote_reuse_count": 0,
                "inflight_coalesced_count": 0,
                "unclaimed_cancelled_count": 0,
            }
        _, _, _, base, terminal_snapshots, publication_task_ids = state
        exploration_snapshots = tuple(
            snapshot
            for task_id, snapshot in terminal_snapshots.items()
            if task_id not in publication_task_ids
        )
        publication_snapshots = tuple(
            snapshot
            for task_id, snapshot in terminal_snapshots.items()
            if task_id in publication_task_ids
        )
        return {
            **base,
            "platform_acquisition_attempt_count": sum(
                snapshot.attempt_count for snapshot in exploration_snapshots
            ),
            "publication_refresh_platform_acquisition_attempt_count": sum(
                snapshot.attempt_count for snapshot in publication_snapshots
            ),
            "recent_quote_reuse_count": sum(
                snapshot.reused_from_task_id is not None
                for snapshot in exploration_snapshots
            ),
            "inflight_coalesced_count": sum(
                snapshot.inflight_coalesced for snapshot in exploration_snapshots
            ),
            "unclaimed_cancelled_count": sum(
                snapshot.state == BrowserTaskState.CANCELLED
                and snapshot.attempt_count == 0
                for snapshot in exploration_snapshots
            ),
        }

    @staticmethod
    def _key(submission: BrowserTaskSubmission) -> str:
        payload = submission.query.model_dump(mode="json")
        options = dict(payload.get("options") or {})
        payload["options"] = {
            key: value
            for key, value in options.items()
            if not key.startswith("__tripchord_")
        }
        canonical = {
            "provider": submission.provider.value,
            "kind": submission.kind.value,
            "partition": submission.reuse_partition_sha256,
            "query": payload,
        }
        return hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    async def submit_many(
        self,
        submissions: Iterable[BrowserTaskSubmission],
    ) -> tuple[BrowserTaskSnapshot, ...]:
        values = tuple(submissions)
        if not values:
            raise ValueError("at least one browser task is required")
        state = self._state.get()
        if state is None:
            return cast(tuple[BrowserTaskSnapshot, ...], await self._delegate.submit_many(values))
        (
            lock,
            entries_by_key,
            entries_by_task_id,
            metrics,
            _terminal_snapshots,
            publication_task_ids,
        ) = state
        async with lock:
            snapshots: list[BrowserTaskSnapshot] = []
            for submission in values:
                # Publication refreshes deliberately disable recent reuse and
                # must bypass the exploration ledger as well. They are a new
                # exact evidence acquisition even when the query fingerprint
                # matches an earlier exploration task.
                if (
                    submission.query.options.get("__tripchord_allow_recent_quote_reuse")
                    is False
                ):
                    (snapshot,) = await self._delegate.submit_many((submission,))
                    metrics["publication_refresh_delegated_acquisitions"] += 1
                    publication_task_ids.add(snapshot.id)
                    snapshots.append(snapshot)
                    continue
                metrics["delegate_submit_call_count"] += 1
                key = self._key(submission)
                entry = entries_by_key.get(key)
                if entry is None:
                    ledger_submission = submission.model_copy(
                        update={
                            "query": submission.query.model_copy(
                                update={
                                    "options": {
                                        **submission.query.options,
                                        "__tripchord_ledger_terminal_retention": True,
                                    }
                                }
                            )
                        }
                    )
                    (snapshot,) = await self._delegate.submit_many((ledger_submission,))
                    metrics["exploration_delegated_acquisitions"] += 1
                    outcome: asyncio.Future[BrowserTaskSnapshot] = (
                        asyncio.get_running_loop().create_future()
                    )
                    outcome.add_done_callback(self._consume_outcome_exception)
                    entry = _FlexibleAcquisitionEntry(snapshot.id, snapshot, outcome)
                    entries_by_key[key] = entry
                    entries_by_task_id[snapshot.id] = entry
                    entry.watcher_task = asyncio.create_task(
                        self._collect_outcome(entry, submission.timeout_seconds)
                    )
                    snapshots.append(snapshot)
                    continue
                entry.consumer_count += 1
                metrics["ledger_shared_return_count"] += 1
                snapshots.append(entry.initial_snapshot)
            return tuple(snapshots)

    async def _collect_outcome(self, entry: _FlexibleAcquisitionEntry, timeout: int) -> None:
        try:
            (snapshot,) = await self._delegate.wait_many(
                (entry.task_id,),
                timeout_seconds=timeout,
            )
        except BaseException as exc:
            if not entry.outcome.done():
                entry.outcome.set_exception(exc)
            return
        entry.terminal_snapshot = snapshot
        state = self._state.get()
        if state is not None:
            state[4][snapshot.id] = snapshot
        if not entry.outcome.done():
            entry.outcome.set_result(snapshot)

    @staticmethod
    def _consume_outcome_exception(future: asyncio.Future[BrowserTaskSnapshot]) -> None:
        if not future.cancelled():
            future.exception()

    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: int,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        state = self._state.get()
        if state is None:
            return cast(
                tuple[BrowserTaskSnapshot, ...],
                await self._delegate.wait_many(task_ids, timeout_seconds=timeout_seconds),
            )
        _, _, entries_by_task_id, _, terminal_snapshots, _ = state
        results: list[BrowserTaskSnapshot] = []
        for task_id in task_ids:
            entry = entries_by_task_id.get(task_id)
            if entry is None:
                (direct_snapshot,) = await self._delegate.wait_many(
                    (task_id,),
                    timeout_seconds=timeout_seconds,
                )
                terminal_snapshots[direct_snapshot.id] = direct_snapshot
                results.append(direct_snapshot)
                continue
            try:
                results.append(await asyncio.shield(entry.outcome))
            except asyncio.CancelledError:
                # LiveSystem's cancellation cleanup explicitly calls
                # ``cancel_many`` after the wait task is cancelled. Releasing
                # here as well would double-decrement a consumer and cancel a
                # still-live duplicate.
                raise
        return tuple(results)

    async def cancel_many(self, task_ids: Iterable[str], *, reason: str) -> Any:
        state = self._state.get()
        if state is None:
            return await self._delegate.cancel_many(task_ids, reason=reason)
        _, _, entries_by_task_id, _, _, _ = state
        for task_id in task_ids:
            entry = entries_by_task_id.get(task_id)
            if entry is not None:
                await self._consumer_cancel(entry, reason=reason)
            else:
                await self._delegate.cancel_many((task_id,), reason=reason)
        return None

    async def _consumer_cancel(self, entry: _FlexibleAcquisitionEntry, *, reason: str) -> None:
        entry.consumer_count = max(0, entry.consumer_count - 1)
        if (
            entry.consumer_count != 0
            or entry.terminal_snapshot is not None
            or entry.cleanup_forwarded
        ):
            return
        entry.cleanup_forwarded = True
        try:
            await self._delegate.cancel_many((entry.task_id,), reason=reason)
        except BaseException:
            entry.cleanup_forwarded = False
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class FlexiblePairState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class SourceScheduleBudgetExceeded(TimeoutError):
    """A server-owned absolute provider lane cannot start inside its budget."""

    def __init__(self, delays: dict[str, int]) -> None:
        self.delays = delays
        over_limit = max(delays.values(), default=0)
        super().__init__(
            "provider lane reservation exceeds the 900000ms source start budget: "
            f"max_delay_ms={over_limit}"
        )


class FullWindowDeadlineInfeasible(ValueError):
    """The server can prove a full date universe misses its hard deadline."""

    def __init__(
        self,
        *,
        pair_count: int,
        last_source_offset_ms: int,
        conservative_execution_budget_ms: int,
        total_timeout_seconds: int,
    ) -> None:
        self.pair_count = pair_count
        self.last_source_offset_ms = last_source_offset_ms
        self.conservative_execution_budget_ms = conservative_execution_budget_ms
        self.total_timeout_seconds = total_timeout_seconds
        required_ms = last_source_offset_ms + conservative_execution_budget_ms
        super().__init__(
            "full_window_deadline_infeasible: 完整日期全集的最后必需 provider source "
            f"offset={last_source_offset_ms}ms，加保守执行预算="
            f"{conservative_execution_budget_ms}ms，总需求={required_ms}ms，"
            f"超过请求硬上限 {total_timeout_seconds}s；未确认 provider 的 "
            "calendar/range/batch acquisition 能力，不能把已评估子集发布为全集最优。"
        )


class FlexibleObjectiveWeights(DomainModel):
    price: Decimal = Field(default=Decimal("0.45"), ge=0, le=1)
    evidence: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    robustness: Decimal = Field(default=Decimal("0.15"), ge=0, le=1)
    convenience: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    schedule_quality: Decimal = Field(default=Decimal("0.05"), ge=0, le=1)
    breakfast: Decimal = Field(default=Decimal("0.025"), ge=0, le=1)
    baggage: Decimal = Field(default=Decimal("0.025"), ge=0, le=1)

    @model_validator(mode="after")
    def require_positive_total(self) -> FlexibleObjectiveWeights:
        if sum(self.model_dump().values(), start=Decimal(0)) <= 0:
            raise ValueError("at least one flexible-search objective weight must be positive")
        return self

    def objective_specs(self) -> tuple[ObjectiveWeight, ...]:
        return (
            ObjectiveWeight(
                name="price",
                direction=ObjectiveDirection.MINIMIZE,
                weight=self.price,
            ),
            ObjectiveWeight(name="evidence", weight=self.evidence),
            ObjectiveWeight(name="robustness", weight=self.robustness),
            ObjectiveWeight(name="convenience", weight=self.convenience),
            ObjectiveWeight(name="schedule_quality", weight=self.schedule_quality),
            ObjectiveWeight(name="breakfast", weight=self.breakfast),
            ObjectiveWeight(name="baggage", weight=self.baggage),
        )


class FlexiblePackageConstraints(DomainModel):
    budget_cents: int | None = Field(default=None, ge=0)
    require_checked_baggage: bool | None = None
    allow_connections: bool | None = None
    require_breakfast: bool | None = None
    breakfast_preference_mode: PreferenceMode | None = None
    breakfast_preference_weight: float | None = Field(default=None, ge=0, le=1)
    minimum_arrival_to_boat_minutes: int = Field(default=120, ge=0, le=1440)
    minimum_airport_buffer_minutes: int = Field(default=180, ge=0, le=1440)
    maximum_quote_capture_skew_minutes: int = Field(default=20, ge=1, le=180)
    objective_weights: FlexibleObjectiveWeights = Field(default_factory=FlexibleObjectiveWeights)

    @model_validator(mode="after")
    def validate_preference_weights(self) -> FlexiblePackageConstraints:
        mode = self.breakfast_preference_mode
        weight = self.breakfast_preference_weight
        if mode is None:
            if weight is not None:
                raise ValueError("breakfast_preference_weight requires breakfast_preference_mode")
        elif weight is None:
            raise ValueError("breakfast_preference_mode requires breakfast_preference_weight")
        else:
            canonical_weight = {
                PreferenceMode.REQUIRED: 1.0,
                PreferenceMode.FORBIDDEN: 1.0,
                PreferenceMode.INDIFFERENT: 0.0,
            }.get(mode)
            if canonical_weight is not None and weight != canonical_weight:
                raise ValueError(
                    f"{mode.value} breakfast mode requires canonical weight {canonical_weight:g}"
                )
            hard_value = {
                PreferenceMode.REQUIRED: True,
                PreferenceMode.FORBIDDEN: False,
            }.get(mode)
            if hard_value is not None and self.require_breakfast is not hard_value:
                raise ValueError(f"{mode.value} breakfast mode conflicts with require_breakfast")
            if (
                mode in {PreferenceMode.WEIGHTED, PreferenceMode.INDIFFERENT}
                and self.require_breakfast is not None
            ):
                raise ValueError(f"{mode.value} breakfast mode must not create a hard constraint")
        if (
            sum(
                (item.weight for item in self.objective_specs()),
                start=Decimal(0),
            )
            <= 0
        ):
            raise ValueError("at least one effective flexible-search weight must be positive")
        return self

    def objective_specs(self) -> tuple[ObjectiveWeight, ...]:
        """Resolve generic ranking weights against explicit trip preferences.

        Breakfast and baggage are not implicit quality bonuses. They only
        participate when the user selected the corresponding required/forbidden
        or weighted semantics. Hard enforcement remains in ``PackageVerifier``;
        this method only aligns the soft ranking vector with the same contract.
        """

        breakfast_direction = ObjectiveDirection.MAXIMIZE
        mode = self.breakfast_preference_mode
        if mode == PreferenceMode.WEIGHTED:
            breakfast_weight = Decimal(str(self.breakfast_preference_weight))
        elif mode == PreferenceMode.REQUIRED:
            breakfast_weight = Decimal(1)
        elif mode == PreferenceMode.FORBIDDEN:
            breakfast_weight = Decimal(1)
            breakfast_direction = ObjectiveDirection.MINIMIZE
        elif mode == PreferenceMode.INDIFFERENT:
            breakfast_weight = Decimal(0)
        elif self.require_breakfast is True:
            breakfast_weight = Decimal(1)
        elif self.require_breakfast is False:
            breakfast_weight = Decimal(1)
            breakfast_direction = ObjectiveDirection.MINIMIZE
        else:
            breakfast_weight = Decimal(0)

        baggage_weight = Decimal(1) if self.require_checked_baggage is True else Decimal(0)
        configured = self.objective_weights
        return (
            ObjectiveWeight(
                name="price",
                direction=ObjectiveDirection.MINIMIZE,
                weight=configured.price,
            ),
            ObjectiveWeight(name="evidence", weight=configured.evidence),
            ObjectiveWeight(name="robustness", weight=configured.robustness),
            ObjectiveWeight(name="convenience", weight=configured.convenience),
            ObjectiveWeight(name="schedule_quality", weight=configured.schedule_quality),
            ObjectiveWeight(
                name="breakfast",
                direction=breakfast_direction,
                weight=breakfast_weight,
            ),
            ObjectiveWeight(name="baggage", weight=baggage_weight),
        )


class PublicationRefreshAudit(DomainModel):
    """Evidence that one publishable option came from a fresh, non-reused run."""

    schema_version: str = Field(
        default="tripchord-publication-refresh-v1",
        pattern="^tripchord-publication-refresh-v1$",
    )
    refresh_started_at: datetime
    refresh_completed_at: datetime
    refresh_slot_index: int = Field(ge=0, le=1)
    refresh_slot_count: int = Field(ge=1, le=2)
    recent_quote_reuse_disabled: bool
    source_start_delays_ms: dict[str, int]
    source_task_ids: tuple[str, ...] = Field(min_length=1)
    browser_task_ids: tuple[str, ...] = ()
    reused_browser_task_ids: tuple[str, ...] = ()
    pipeline_task_ids: tuple[str, ...] = ()
    previous_candidate_id: str | None = None
    refreshed_candidate_id: str | None = None
    refreshed_option_id: str | None = None
    browser_candidate_component_ids: tuple[str, ...] = ()
    fresh_browser_component_ids: tuple[str, ...] = ()
    fresh_evidence_refs: tuple[str, ...] = ()
    binding_passed: bool
    failure_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_binding(self) -> PublicationRefreshAudit:
        for value in (self.refresh_started_at, self.refresh_completed_at):
            if value.tzinfo is None:
                raise ValueError("publication refresh timestamps must be timezone-aware")
        if self.refresh_completed_at < self.refresh_started_at:
            raise ValueError("publication refresh cannot complete before it starts")
        if self.refresh_slot_index >= self.refresh_slot_count:
            raise ValueError("publication refresh slot index must be inside its slot count")
        if any(delay < 0 or delay > 900_000 for delay in self.source_start_delays_ms.values()):
            raise ValueError("publication refresh delays must stay inside the live source budget")
        if len(set(self.source_task_ids)) != len(self.source_task_ids):
            raise ValueError("publication refresh source task ids must be unique")
        if len(set(self.browser_task_ids)) != len(self.browser_task_ids):
            raise ValueError("publication refresh browser task ids must be unique")
        if self.binding_passed:
            if self.failure_reasons:
                raise ValueError("a passed publication refresh cannot carry failure reasons")
            if not self.recent_quote_reuse_disabled:
                raise ValueError("a passed publication refresh must disable recent quote reuse")
            if self.reused_browser_task_ids:
                raise ValueError("a passed publication refresh cannot reuse browser tasks")
            if len(self.browser_task_ids) != len(self.source_task_ids):
                raise ValueError("every publication source task must bind one browser task")
            if set(self.pipeline_task_ids) != set(_PUBLICATION_REFRESH_PIPELINE_TASK_IDS):
                raise ValueError(
                    "publication refresh must complete the full deterministic pipeline"
                )
            if self.refreshed_candidate_id is None or self.refreshed_option_id is None:
                raise ValueError("publication refresh must bind a refreshed candidate and option")
            if not self.browser_candidate_component_ids:
                raise ValueError("publication refresh must bind browser-backed components")
            if set(self.fresh_browser_component_ids) != set(self.browser_candidate_component_ids):
                raise ValueError("every browser-backed component must come from fresh evidence")
            if not self.fresh_evidence_refs:
                raise ValueError("publication refresh must expose fresh evidence references")
        elif not self.failure_reasons:
            raise ValueError("a failed publication refresh binding requires typed reasons")
        return self


class FlexiblePairExecution(DomainModel):
    date_pair: AuditableDatePair
    query_tasks: tuple[FlexibleQueryTask, ...] = Field(min_length=11, max_length=18)
    source_start_delays_ms: dict[str, int]
    state: FlexiblePairState
    run: LivePackageAgentRun | None = None
    exploration_run: LivePackageAgentRun | None = None
    failure_class: str | None = None
    failure_message: str | None = None
    publication_refresh_audit: PublicationRefreshAudit | None = None
    publication_refresh_failure_class: str | None = None
    publication_refresh_failure_message: str | None = None

    @model_validator(mode="after")
    def validate_result_shape(self) -> FlexiblePairExecution:
        if self.state == FlexiblePairState.COMPLETED:
            if self.run is None or self.failure_class is not None:
                raise ValueError("completed date pair requires a run and no failure")
        elif self.run is not None or not self.failure_class or not self.failure_message:
            raise ValueError("failed date pair requires typed failure details and no run")
        if self.publication_refresh_audit is not None and self.run is None:
            raise ValueError("publication refresh audit requires the refreshed live run")
        if self.publication_refresh_audit is not None:
            if self.exploration_run is None:
                raise ValueError("publication refresh must preserve its exploration run")
            if self.run is None or self.run.evidence_scope != (
                LiveEvidenceScope.PUBLICATION_COMPONENT_REFRESH
            ):
                raise ValueError("publication refresh run must declare component-refresh scope")
            if (
                self.exploration_run.run_purpose != LiveRunPurpose.EXPLORATION_SELECTION
                or self.exploration_run.finalization_state
                != LiveFinalizationState.EXPLORATION_SEALED
                or not self.exploration_run.exploration_seal_passed
            ):
                raise ValueError("publication refresh must preserve a sealed exploration run")
            if (
                self.run.run_purpose != LiveRunPurpose.FINAL_PUBLICATION
                or self.run.finalization_state != LiveFinalizationState.FINAL_PUBLISHED
            ):
                raise ValueError("publication refresh must expose a final publication run")
        refresh_failure = self.publication_refresh_failure_class is not None
        if refresh_failure != (self.publication_refresh_failure_message is not None):
            raise ValueError("publication refresh failure requires class and message")
        if refresh_failure and self.publication_refresh_audit is not None:
            raise ValueError("publication refresh cannot both fail and expose a completed audit")
        return self


class FlexiblePerformanceReport(DomainModel):
    """Run-level accounting for the internal flexible-search scheduler.

    These counters describe TripChord's own orchestration, not provider
    latency or a claim about how quickly an external platform responds.  The
    report is attached to the durable run result so a benchmark and the
    product API consume the same accounting rather than reconstructing it
    from logs.
    """

    schema_version: str = Field(
        default="tripchord-flexible-performance-v1",
        pattern="^tripchord-flexible-performance-v1$",
    )
    measurement_basis: str = Field(pattern="^(observed|planned_only)$")
    wall_time_seconds: float = Field(ge=0)
    internal_benchmark_budget_seconds: int | None = Field(default=None, ge=1, le=600)
    planned_logical_query_count: int = Field(ge=0)
    planned_unique_acquisition_count: int = Field(ge=0)
    planned_deduplicated_query_count: int = Field(ge=0)
    executed_logical_query_count: int | None = Field(default=None, ge=0)
    delegate_submit_call_count: int | None = Field(default=None, ge=0)
    delegated_acquisition_count: int | None = Field(default=None, ge=0)
    executed_deduplicated_query_count: int | None = Field(default=None, ge=0)
    ledger_shared_return_count: int | None = Field(default=None, ge=0)
    platform_acquisition_attempt_count: int | None = Field(default=None, ge=0)
    model_call_count: int = Field(ge=0)
    model_cost_usd: float = Field(ge=0)
    publication_refresh_delegated_acquisition_count: int | None = Field(default=None, ge=0)
    publication_refresh_platform_acquisition_attempt_count: int | None = Field(
        default=None, ge=0
    )
    recent_quote_reuse_count: int | None = Field(default=None, ge=0)
    inflight_coalesced_count: int | None = Field(default=None, ge=0)
    unclaimed_cancelled_count: int | None = Field(default=None, ge=0)
    process_peak_rss_bytes: int = Field(ge=0)
    cpu_time_seconds: float = Field(ge=0)
    max_source_start_delay_ms: int = Field(ge=0, le=_MAX_SOURCE_START_DELAY_MS)
    date_pair_count: int = Field(ge=0)
    completed_date_pair_count: int = Field(ge=0)
    failed_date_pair_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> FlexiblePerformanceReport:
        runtime_fields = (
            self.executed_logical_query_count,
            self.delegate_submit_call_count,
            self.delegated_acquisition_count,
            self.executed_deduplicated_query_count,
            self.ledger_shared_return_count,
            self.platform_acquisition_attempt_count,
            self.publication_refresh_delegated_acquisition_count,
            self.publication_refresh_platform_acquisition_attempt_count,
            self.recent_quote_reuse_count,
            self.inflight_coalesced_count,
            self.unclaimed_cancelled_count,
        )
        if self.measurement_basis == "planned_only":
            if any(value is not None for value in runtime_fields):
                raise ValueError("planned-only report cannot contain runtime acquisition metrics")
        elif any(value is None for value in runtime_fields):
            raise ValueError("observed report requires all runtime acquisition metrics")
        else:
            assert self.executed_logical_query_count is not None
            assert self.delegate_submit_call_count is not None
            assert self.delegated_acquisition_count is not None
            assert self.executed_deduplicated_query_count is not None
            assert self.ledger_shared_return_count is not None
            if self.delegated_acquisition_count > self.executed_logical_query_count:
                raise ValueError("delegated acquisitions cannot exceed executed queries")
            if self.delegate_submit_call_count < self.delegated_acquisition_count:
                raise ValueError("delegate submit calls cannot be below acquisitions")
            if self.ledger_shared_return_count != (
                self.delegate_submit_call_count - self.delegated_acquisition_count
            ):
                raise ValueError("ledger shared returns must reconcile with delegate calls")
            if self.executed_deduplicated_query_count + self.delegated_acquisition_count != (
                self.executed_logical_query_count
            ):
                raise ValueError("executed query counts must reconcile")
        if self.completed_date_pair_count + self.failed_date_pair_count > self.date_pair_count:
            raise ValueError("date-pair result counts exceed planned date pairs")
        if (
            self.executed_logical_query_count is not None
            and self.executed_logical_query_count > self.planned_logical_query_count
        ):
            raise ValueError("executed logical queries exceed the plan")
        if (
            self.delegated_acquisition_count is not None
            and self.delegated_acquisition_count > self.planned_unique_acquisition_count
        ):
            raise ValueError("delegated acquisitions exceed the unique-acquisition plan")
        return self


class FlexibleRankedOption(DomainModel):
    rank: int = Field(ge=1)
    date_pair_id: str = Field(min_length=1)
    departure_date: date
    return_date: date
    decision_state: PackageDecisionState
    recommendable: bool
    complete_cny_party_total: bool = False
    total_budget_cents: int | None = Field(default=None, ge=0)
    evidence_completeness: Decimal = Field(ge=0, le=1)
    all_platforms_complete: bool
    source_execution_completeness: SourceExecutionCompleteness | None = None
    exact_quote_comparison_coverage: ExactQuoteComparisonCoverage | None = None
    final_candidate_id: str | None = None
    stay_plan_id: StayPlanId | None = None
    option_id: str = Field(min_length=1)
    objective_values: dict[str, Decimal] = Field(default_factory=dict)
    weighted_score: Decimal = Decimal(0)
    pareto_front: int = Field(default=1, ge=1)
    diversity_tags: tuple[str, ...] = ()
    score_explanation: str = "尚未执行多目标排序"


class DynamicCandidateAgentAddition(DomainModel):
    """Audited model-Agent additions discovered only after an exact-date Planner runs."""

    date_pair_id: str = Field(min_length=1)
    evidence_scope: LiveEvidenceScope
    run_purpose: LiveRunPurpose
    scale_state_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")
    pool_candidate_count: int = Field(gt=32)
    candidate_scout_count: int = Field(ge=2)
    additional_model_agent_count: int = Field(ge=2)
    scout_task_ids: tuple[str, ...] = Field(min_length=2)
    merger_task_id: str = Field(pattern="^curate-travel-candidates$")
    merger_agent_template_id: str = Field(pattern="^candidate_merger$")
    merger_agent_admitted: bool

    @model_validator(mode="after")
    def validate_candidate_addition(self) -> DynamicCandidateAgentAddition:
        if self.candidate_scout_count != len(self.scout_task_ids):
            raise ValueError("dynamic candidate Scout count does not match task IDs")
        if self.additional_model_agent_count != self.candidate_scout_count:
            raise ValueError(
                "dynamic candidate additions count only Scouts; the final Merger "
                "already occupies the frozen pipeline's curator slot"
            )
        if len(self.scout_task_ids) != len(set(self.scout_task_ids)):
            raise ValueError("dynamic candidate Scout task IDs must be unique")
        if not self.merger_agent_admitted:
            raise ValueError("dynamic candidate additions require the final Merger admission")
        return self


class FlexibleLiveAgentRun(DomainModel):
    requested_window: FlexibleTravelWindow
    effective_window: FlexibleTravelWindow
    exploration: DateExplorationResult
    query_plan: FlexibleQueryPlan
    pair_runs: tuple[FlexiblePairExecution, ...] = Field(min_length=1)
    ranked_options: tuple[FlexibleRankedOption, ...] = Field(min_length=1)
    refinement_trace: tuple[AdaptiveRefinementDecision, ...] = ()
    recommended_option_ids: tuple[str, ...] = ()
    final_decision: PackageDecision
    query_strategy: QueryStrategyProposal | None = None
    query_agentic: AgenticRunSummary = Field(
        default_factory=lambda: AgenticRunSummary(enabled=False, required=False)
    )
    adaptive_scaling_enabled: bool = False
    scale_directive: ScaleDirective | None = None
    agent_template_plan: AgentTemplatePlan | None = None
    agent_budget_audit: AgentBudgetAudit | None = None
    agent_budget_scope_start_admitted_count: int = Field(default=0, ge=0, le=96)
    dynamic_candidate_agent_additions: tuple[DynamicCandidateAgentAddition, ...] = ()
    stay_area_search_profile: StayAreaSearchProfile | None = None
    stay_plan_candidate_set: StayPlanCandidateSet | None = None
    publication_refresh_minimum_options: int = Field(default=0, ge=0, le=2)
    publication_refreshed_option_ids: tuple[str, ...] = ()
    sampled_not_exhaustive: bool
    optimality_status: DateOptimalityStatus = DateOptimalityStatus.BEST_VERIFIED_IN_EVALUATED_SET
    admissible_bounds: tuple[AdmissibleCostBound, ...] = ()
    claim_boundary: str = Field(min_length=1)
    performance_report: FlexiblePerformanceReport | None = None

    @model_validator(mode="after")
    def validate_adaptive_control_audit(self) -> FlexibleLiveAgentRun:
        expected_candidate_additions = self._candidate_agent_additions(self.pair_runs)
        if self.dynamic_candidate_agent_additions != expected_candidate_additions:
            raise ValueError(
                "dynamic candidate Agent additions do not reconcile with exact-date runs"
            )
        audit_present = self.scale_directive is not None or self.agent_template_plan is not None
        if audit_present != self.adaptive_scaling_enabled:
            raise ValueError(
                "adaptive scaling requires both a scale directive and template plan audit"
            )
        if self.adaptive_scaling_enabled:
            assert self.scale_directive is not None
            assert self.agent_template_plan is not None
            if self.scale_directive.state_fingerprint != self.agent_template_plan.state_fingerprint:
                raise ValueError("adaptive scale directive and template plan must share a state")
            if self.agent_budget_audit is None:
                raise ValueError("adaptive scaling requires the request-wide Agent budget audit")
            if (
                self.agent_budget_scope_start_admitted_count
                > self.agent_budget_audit.admitted_count
            ):
                raise ValueError("planning Agent budget scope starts after the final audit")
            planning_admissions = (
                self.agent_budget_audit.admitted_count
                - self.agent_budget_scope_start_admitted_count
            )
            audited_dynamic_additions = sum(
                item.additional_model_agent_count for item in self.dynamic_candidate_agent_additions
            )
            reconciled_admission_cap = min(
                96,
                self.scale_directive.logical_agent_cap + audited_dynamic_additions,
            )
            if planning_admissions > reconciled_admission_cap:
                raise ValueError("actual model Agent admissions exceeded the frozen directive")
            if self.agent_budget_audit.rejected_count:
                raise ValueError("a completed adaptive run cannot hide rejected model Agents")
        return self

    @model_validator(mode="after")
    def validate_optimality_certificate(self) -> FlexibleLiveAgentRun:
        if (
            self.final_decision.state == PackageDecisionState.HUMAN_BLOCK
            and self.optimality_status == DateOptimalityStatus.OPTIMALITY_PROVEN
        ):
            raise ValueError("HUMAN_BLOCK runs cannot claim proven optimality")
        if self.optimality_status == DateOptimalityStatus.OPTIMALITY_PROVEN and (
            not self.admissible_bounds
            or not all(bound.proven for bound in self.admissible_bounds)
        ):
                raise ValueError(
                    "optimality_proven requires non-empty proven admissible bounds"
                )
        return self

    @staticmethod
    def _candidate_agent_additions(
        pair_runs: tuple[FlexiblePairExecution, ...],
    ) -> tuple[DynamicCandidateAgentAddition, ...]:
        additions: list[DynamicCandidateAgentAddition] = []
        for execution in pair_runs:
            scoped_runs = tuple(
                run for run in (execution.exploration_run, execution.run) if run is not None
            )
            seen_run_keys: set[tuple[LiveEvidenceScope, LiveRunPurpose, str]] = set()
            for live_run in scoped_runs:
                shard_audit = live_run.candidate_shard_merge_audit
                scale_directive = live_run.candidate_scale_directive
                if shard_audit is None:
                    continue
                if scale_directive is None:  # pragma: no cover - LiveRun invariant
                    raise ValueError("candidate Scout audit is missing its refined directive")
                run_key = (
                    live_run.evidence_scope,
                    live_run.run_purpose,
                    shard_audit.scale_state_fingerprint,
                )
                if run_key in seen_run_keys:
                    continue
                seen_run_keys.add(run_key)
                additions.append(
                    DynamicCandidateAgentAddition(
                        date_pair_id=execution.date_pair.id,
                        evidence_scope=live_run.evidence_scope,
                        run_purpose=live_run.run_purpose,
                        scale_state_fingerprint=shard_audit.scale_state_fingerprint,
                        pool_candidate_count=shard_audit.pool_candidate_count,
                        candidate_scout_count=shard_audit.requested_shard_count,
                        additional_model_agent_count=(shard_audit.requested_shard_count),
                        scout_task_ids=tuple(item.task_id for item in shard_audit.shards),
                        merger_task_id=shard_audit.merger_task_id,
                        merger_agent_template_id=(shard_audit.merger_agent_template_id),
                        merger_agent_admitted=shard_audit.merger_agent_admitted,
                    )
                )
        return tuple(additions)

    @model_validator(mode="after")
    def validate_recommendation_quote_coverage(self) -> FlexibleLiveAgentRun:
        if len(self.recommended_option_ids) > 1:
            raise ValueError("a production run may expose at most one recommended option")
        if self.recommended_option_ids:
            selected = next(
                (
                    option
                    for option in self.ranked_options
                    if option.option_id == self.recommended_option_ids[0]
                ),
                None,
            )
            if selected is None or not selected.complete_cny_party_total:
                raise ValueError(
                    "the final recommendation must be a complete CNY party total"
                )
        unsafe = tuple(
            option.option_id
            for option in self.ranked_options
            if option.recommendable
            and option.exact_quote_comparison_coverage is not None
            and not option.exact_quote_comparison_coverage.complete
            and not option.exact_quote_comparison_coverage.single_source_publishable
        )
        if unsafe:
            raise ValueError(
                "recommendable options require complete exact lodging quote comparison "
                f"coverage: {unsafe}"
            )
        return self

    @model_validator(mode="after")
    def validate_publication_refresh(self) -> FlexibleLiveAgentRun:
        if self.publication_refresh_minimum_options == 0:
            if self.publication_refreshed_option_ids:
                raise ValueError("disabled publication refresh cannot expose refreshed options")
            return self
        passed_audits = tuple(
            audit
            for execution in self.pair_runs
            if (audit := execution.publication_refresh_audit) is not None
            and audit.binding_passed
            and audit.refreshed_option_id is not None
        )
        passed_by_option = {audit.refreshed_option_id: audit for audit in passed_audits}
        if len(passed_by_option) != len(passed_audits):
            raise ValueError("publication refresh audits must bind unique option ids")
        all_refreshed_browser_task_ids = tuple(
            browser_task_id for audit in passed_audits for browser_task_id in audit.browser_task_ids
        )
        if len(all_refreshed_browser_task_ids) != len(set(all_refreshed_browser_task_ids)):
            raise ValueError(
                "publication refresh options must bind globally distinct browser tasks"
            )
        if tuple(self.publication_refreshed_option_ids) != tuple(
            option.option_id
            for option in self.ranked_options
            if option.option_id in passed_by_option
        ):
            raise ValueError(
                "publication refreshed option ids must follow the final ranked option order"
            )
        if any(option_id not in passed_by_option for option_id in self.recommended_option_ids):
            raise ValueError("every recommended option must bind a passed publication refresh")
        if (
            self.final_decision.state == PackageDecisionState.ACCEPT
            and not self.recommended_option_ids
        ):
            raise ValueError("ACCEPT requires at least one refreshed recommendation")
        return self


class LiveDatePairRunner(Protocol):
    async def run(
        self,
        intent: PackageIntent,
        query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        timeout_seconds: int = 120,
        source_start_delays_ms: dict[str, int] | None = None,
    ) -> LivePackageAgentRun: ...


class SearchRunRecorder(Protocol):
    """Async persistence hook for a completed pair :class:`SearchRun`.

    The tenant is captured by the caller's closure so the flexible system never
    needs to know who is running the search.
    """

    async def __call__(
        self,
        run: LivePackageAgentRun,
    ) -> SearchRun: ...


class FlexibleLiveAgentSystem:
    """Bounded flexible-date controller over the strict fifteen-source live system."""

    def __init__(
        self,
        live_system: LiveDatePairRunner,
        *,
        explorer: FlexibleDateExplorer | None = None,
        query_planner: FlexibleQueryPlanBuilder | None = None,
        now: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        minimum_departure_lead_days: int = 0,
        date_refiner: DatePairRefiner | None = None,
        model_router: ModelRouter | None = None,
        model_agents_required: bool = False,
        context_builder: BudgetedAgentContextBuilder | None = None,
        memory_store: MemoryStore | None = None,
        adaptive_agent_scaling_enabled: bool = False,
    ) -> None:
        if minimum_departure_lead_days < 0:
            raise ValueError("minimum departure lead days cannot be negative")
        self._live = live_system
        self._explorer = explorer or FlexibleDateExplorer(
            platforms=LIVE_V5_PLATFORMS,
        )
        self._query_planner = query_planner or FlexibleQueryPlanBuilder(
            platforms=LIVE_V5_PLATFORMS,
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic_clock or monotonic
        self._minimum_departure_lead_days = minimum_departure_lead_days
        self._date_refiner = date_refiner or RankedTopKDateRefiner()
        self._date_acquisition_policy = str(
            getattr(self._date_refiner, "policy_id", "custom_injected_refiner")
        )
        self._model_router = model_router
        self._model_agents_required = model_agents_required
        self._context_builder = context_builder
        self._memory_store = memory_store
        self._adaptive_agent_scaling_enabled = adaptive_agent_scaling_enabled
        self._acquisition_ledger: _FlexibleAcquisitionLedger | None = None
        if isinstance(live_system, LivePackageAgentSystem):
            bridge = getattr(live_system, "_bridge", None)
            if bridge is not None:
                self._acquisition_ledger = _FlexibleAcquisitionLedger(bridge)
                cast(Any, live_system)._bridge = self._acquisition_ledger

    @staticmethod
    def _process_peak_rss_bytes() -> int:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # macOS reports bytes; Linux and most other Unix platforms report KiB.
        return value if sys.platform == "darwin" else value * 1024

    @staticmethod
    def _process_cpu_seconds() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return max(0.0, float(usage.ru_utime + usage.ru_stime))

    @staticmethod
    def _performance_model_metrics(
        query_agentic: AgenticRunSummary,
        executions: tuple[FlexiblePairExecution, ...],
    ) -> tuple[int, float]:
        """Aggregate model facts without counting an execution twice."""

        model_calls = query_agentic.logical_request_count
        model_cost = query_agentic.total_estimated_cost_usd
        seen: set[tuple[str, LiveRunPurpose, LiveEvidenceScope, object]] = set()
        for execution in executions:
            for live_run in (execution.exploration_run, execution.run):
                if live_run is None:
                    continue
                # A publication refresh keeps the exploration run on the
                # execution for auditability.  Source task IDs are the stable
                # run receipt identity; unlike object ids, they survive
                # serialization and distinguish separate date-pair runs that
                # happen to share a trip id.
                source_task_ids = tuple(getattr(live_run, "source_task_ids", ()))
                run_key = (
                    live_run.intent.trip_id,
                    live_run.run_purpose,
                    live_run.evidence_scope,
                    source_task_ids
                    if source_task_ids
                    else live_run.intent.trip_id,
                )
                if run_key in seen:
                    continue
                seen.add(run_key)
                model_calls += live_run.agentic.logical_request_count
                model_cost += live_run.agentic.total_estimated_cost_usd
        return model_calls, model_cost

    @request_agent_budgeted
    async def run(
        self,
        window: FlexibleTravelWindow,
        calendars: tuple[PlatformFareCalendar, ...] = (),
        *,
        mode: LiveCoverageMode = LiveCoverageMode.STRICT,
        max_pairs: int = 400,
        policy: QueryPlanPolicy | None = None,
        constraints: FlexiblePackageConstraints | None = None,
        timeout_seconds: int = 120,
        total_timeout_seconds: int = 600,
        stay_plan_candidate_set: StayPlanCandidateSet | None = None,
        memory_access: MemoryAccessContext | None = None,
        publication_refresh_minimum_options: int = 0,
        pair_checkpoint_reporter: PairCheckpointReporter | None = None,
        checkpoint_request_sha256: str | None = None,
        search_run_recorder: SearchRunRecorder | None = None,
        reference_date: date | None = None,
        internal_benchmark: bool = False,
    ) -> FlexibleLiveAgentRun:
        run_started = self._monotonic()
        cpu_started = self._process_cpu_seconds()
        if self._acquisition_ledger is not None:
            self._acquisition_ledger.reset()
        budget_ledger = current_agent_budget()
        if budget_ledger is None:  # pragma: no cover - decorator invariant
            raise RuntimeError("flexible live run requires an Agent budget ledger")
        budget_scope_start_admitted_count = budget_ledger.audit().admitted_count
        if not 1 <= max_pairs <= 400:
            raise ValueError("flexible live search supports one to 400 exact date pairs")
        # Normalize the Malé gateway independently of whether the caller
        # supplied the internal candidate set. Explicit sets must not bypass
        # the IATA code required by the Arena official source lane.
        stay_profile = system_stay_area_search_profile(window.destination)
        if stay_profile is None and window.destination_code == "MLE":
            stay_profile = system_stay_area_search_profile("马累")
        if stay_profile is None and window.destination in {"马尔代夫", "Maldives"}:
            stay_profile = system_stay_area_search_profile("马累")
        if stay_profile is not None:
            window = window.model_copy(
                update={
                    "destination": stay_profile.gateway_destination,
                    "destination_code": "MLE",
                }
            )
        # Direct callers may omit the internal candidate set, but must still
        # receive the same frozen stay-plan contract as text callers.
        if stay_plan_candidate_set is None and stay_profile is not None:
            stay_plan_candidate_set = system_stay_plan_candidate_set(
                stay_profile.gateway_destination
            )
        if not 60 <= total_timeout_seconds <= 600:
            raise ValueError("flexible total timeout supports 60 to 600 seconds")
        if internal_benchmark and total_timeout_seconds > INTERNAL_BENCHMARK_TOTAL_TIMEOUT_SECONDS:
            raise ValueError(
                "internal flexible benchmark hard budget is "
                f"{INTERNAL_BENCHMARK_TOTAL_TIMEOUT_SECONDS}s"
            )
        if not 0 <= publication_refresh_minimum_options <= 2:
            raise ValueError("publication refresh supports zero to two final options")
        if pair_checkpoint_reporter is not None and (
            checkpoint_request_sha256 is None
            or len(checkpoint_request_sha256) != 64
            or any(character not in "0123456789abcdef" for character in checkpoint_request_sha256)
        ):
            raise ValueError("pair checkpoint reporting requires a lowercase request SHA-256")
        if window.min_nights < 3:
            raise ValueError("flexible live split-stay planning requires at least three nights")
        if policy is not None and not policy.include_split_stays:
            raise ValueError("flexible live planning requires split-stay query tasks")
        if reference_date is not None:
            # C-122 R44 (canonical pair-set authority): a frozen scenario carries
            # its committed ``reference_date``; pinning the run clock to it makes
            # the sealed ordered trio independently reproducible (the SAME
            # derivation as ``frozen_v4_canonical_pair_ids``), so the producer's
            # exact-trio check and the layer-6 consumer's item-by-item binding
            # cannot be broken by wall-clock drift.
            reference = datetime(
                reference_date.year, reference_date.month, reference_date.day, tzinfo=UTC
            )
        else:
            reference = self._utc_now()
        minimum_departure = reference.date() + timedelta(days=self._minimum_departure_lead_days)
        if window.latest_departure < minimum_departure:
            raise ValueError(
                "flexible departure window has no date meeting the minimum booking lead time"
            )
        effective_earliest = max(window.earliest_departure, minimum_departure)
        effective_window = window.model_copy(update={"earliest_departure": effective_earliest})
        # Enumerating date pairs is cheap: a 31-day window with four stay lengths
        # has only 124 combinations. We enumerate the full coarse universe up to
        # the public 400-pair support boundary. Any caller-selected lower exact
        # budget is diagnostic-only and must remain disclosed as partial.
        coarse_pair_budget = min(effective_window.universe_size, 400)
        effective_window = effective_window.model_copy(update={"max_pairs": coarse_pair_budget})
        exact_pair_budget = min(
            effective_window.universe_size,
            effective_window.max_pairs,
            max_pairs,
        )
        exploration = self._explorer.explore(
            effective_window,
            calendars,
            now=reference,
        )
        if effective_earliest != window.earliest_departure:
            exploration = exploration.model_copy(
                update={
                    "warnings": (
                        *exploration.warnings,
                        "已按执行时点排除提前期不足 "
                        f"{self._minimum_departure_lead_days} 天的出发日期；"
                        f"本轮最早查询 {effective_earliest.isoformat()}",
                    )
                }
            )
        scale_directive = self._adaptive_scale_directive(
            exploration,
            mode=mode,
            exact_pair_budget=exact_pair_budget,
            publication_refresh_minimum_options=publication_refresh_minimum_options,
        )
        remaining_request_agent_budget = budget_ledger.audit().remaining_count
        if (
            scale_directive is not None
            and scale_directive.raw_logical_agents > remaining_request_agent_budget
        ):
            raise ValueError(
                "自适应 Agent 准入门拒绝本轮执行："
                f"规划阶段需要 {scale_directive.raw_logical_agents} 个逻辑 Agent，"
                f"但全请求只剩 {remaining_request_agent_budget}/96 个名额；"
                "请缩小日期窗口/精查数量，或拆成多个独立请求"
            )
        if scale_directive is not None and (
            scale_directive.logical_saturated
            or scale_directive.stop_reason
            in {
                AdaptiveStopReason.STRICT_PROVIDER_COVERAGE_UNREACHABLE,
                AdaptiveStopReason.NO_SEARCH_PROVIDER_AVAILABLE,
                AdaptiveStopReason.LOGICAL_CAP_SATURATED_SPLIT_REQUIRED,
            }
        ):
            raise ValueError(
                "自适应 Agent 准入门拒绝本轮执行："
                f"raw={scale_directive.raw_logical_agents}, "
                f"cap={scale_directive.logical_agent_cap}, "
                f"reasons={list(scale_directive.diagnostic_reasons)}；"
                "请缩小日期窗口/精查数量，或拆成多个独立请求"
            )
        agent_template_plan = (
            build_agent_template_plan(scale_directive) if scale_directive is not None else None
        )
        deterministic_policy = self._execution_query_policy(
            effective_window,
            exploration,
            policy,
            exact_pair_budget,
            stay_plan_candidate_set=stay_plan_candidate_set,
        )
        if exact_pair_budget == exploration.universe_size:
            deterministic_query_plan = self._query_planner.build(
                effective_window,
                exploration,
                deterministic_policy,
                stay_plan_candidate_set=stay_plan_candidate_set,
            )
            self._preflight_full_window_deadline(
                deterministic_query_plan,
                pair_count=exact_pair_budget,
                timeout_seconds=timeout_seconds,
                total_timeout_seconds=total_timeout_seconds,
            )
        if exact_pair_budget == exploration.universe_size:
            # Once the server has admitted the complete deterministic universe,
            # a model cannot improve coverage by selecting a frontier.  Keep the
            # explorer's stable order and spend zero model budget on date
            # sharding/merging; only the final publication refresh may call a
            # model Agent.
            query_strategy = None
            query_agentic = AgenticRunSummary(enabled=False, required=False)
            exploration = exploration.model_copy(
                update={
                    "warnings": (
                        *exploration.warnings,
                        "完整日期全集按服务器确定性顺序执行；未调用 Query Strategist 模型",
                    )
                }
            )
        else:
            query_strategy, query_agentic, exploration = await self._query_strategy(
                effective_window,
                exploration,
                exact_pair_budget=exact_pair_budget,
                memory_access=memory_access,
                scale_directive=scale_directive,
            )
        # A model may reorder the deterministic universe, but it cannot shrink
        # the production search and thereby redefine "best". Explicit lower
        # budgets remain diagnostic-only through ``max_pairs``.
        effective_exact_pair_budget = exact_pair_budget
        effective_policy = (
            deterministic_policy
            if exact_pair_budget == exploration.universe_size
            else self._execution_query_policy(
                effective_window,
                exploration,
                policy,
                effective_exact_pair_budget,
                stay_plan_candidate_set=stay_plan_candidate_set,
            )
        )
        query_plan = self._query_planner.build(
            effective_window,
            exploration,
            effective_policy,
            stay_plan_candidate_set=stay_plan_candidate_set,
        )
        # The final merge can otherwise re-promote the unsafe 2026-09-10
        # departure target after the bounded strategy pass.  Re-apply the
        # actual-arrival boundary at the last server-owned selection point.
        if effective_window.return_date_targets and effective_window.latest_arrival_date:
            safe_return = max(effective_window.return_date_targets)
            if safe_return == effective_window.latest_arrival_date:
                safe_return -= timedelta(days=1)
            safe_pair = next(
                (
                    item
                    for item in exploration.candidates
                    if item.return_date == safe_return
                    and item.night_count == max(
                        effective_window.min_nights,
                        effective_window.max_nights - 1,
                    )
                ),
                None,
            )
            if safe_pair is not None and safe_pair.id not in query_plan.selected_pair_ids:
                selected_ids = (
                    safe_pair.id,
                    *(
                        item
                        for item in query_plan.selected_pair_ids
                        if item != safe_pair.id
                    ),
                )[:effective_exact_pair_budget]
                query_plan = query_plan.model_copy(update={"selected_pair_ids": selected_ids})
        pairs = {item.id: item for item in exploration.candidates}
        effective_constraints = constraints or FlexiblePackageConstraints()
        stay_area_search_profile = system_stay_area_search_profile(effective_window.destination)
        if stay_plan_candidate_set is not None:
            if stay_area_search_profile is None:
                raise ValueError("live-v4 stay plans currently require the Malé gateway profile")
            if (
                stay_plan_candidate_set.gateway_destination
                != stay_area_search_profile.gateway_destination
            ):
                raise ValueError(
                    "frozen stay-plan gateway must exactly match the interpreted destination"
                )
        schedule_started = self._monotonic()
        # The browser companion exposes six global read-only leases. Each pair
        # already saturates them with eleven (v3) or thirteen (v4) tool-bound Source workers.
        # Admitting three pairs together makes early flight receipts wait behind
        # unrelated lodging work until their ten-minute quote TTL expires. Keep
        # source Agents maximally concurrent inside a pair, but admit date pairs
        # one at a time so Verifier always sees a coherent fresh evidence window.
        # Three domain/date workers may overlap; the browser bridge and each
        # provider still enforce their own bounded leases and rate lanes.
        pair_worker_count = 3
        worker_pool_enabled = (
            scale_directive is not None
            and effective_exact_pair_budget > DIRECT_DATE_PAIR_LIMIT
        )
        # A large concrete live run is an exploration pass, not 66 independent
        # model-agent runs.  It uses the deterministic live DAG and reserves
        # the model budget for the single publication refresh below.
        bulk_exploration = (
            isinstance(self._live, LivePackageAgentSystem)
            and effective_exact_pair_budget == exploration.universe_size
        )
        provider_lane_lock = asyncio.Lock()
        provider_next_available_ms = {
            item.platform: 0 for item in effective_policy.platform_rates
        }
        # One absolute lane reservation per provider acquisition, not per
        # date-pair label.  The browser bridge's singleflight/recent-quote
        # reuse then sees the same identity and duplicate pairs inherit the
        # original schedule instead of consuming another provider slot.
        acquisition_offsets_ms: dict[str, int] = {}

        async def execute_pair(
            pair: AuditableDatePair,
            *,
            worker_index: int | None = None,
        ) -> FlexiblePairExecution:
            # Build only the selected pair's source tasks.  This is the dynamic
            # expansion point. The default consumes Query Strategist's audited
            # order; an explicitly injected experimental refiner may pick any
            # still-unqueried item from the full coarse universe.
            pair_exploration = exploration.model_copy(
                update={
                    "candidates": (pair.model_copy(update={"rank": 1}),),
                }
            )
            pair_policy = effective_policy.model_copy(update={"max_exact_pairs": 1})
            pair_plan = self._query_planner.build(
                effective_window,
                pair_exploration,
                pair_policy,
                stay_plan_candidate_set=stay_plan_candidate_set,
            )
            rate_by_platform = {
                item.platform: effective_platform_interval_ms(
                    item,
                    stay_plan_candidate_set=stay_plan_candidate_set,
                )
                for item in effective_policy.platform_rates
            }
            absolute_offsets: dict[str, int] = {}
            async with provider_lane_lock:
                for platform, interval_ms in rate_by_platform.items():
                    platform_tasks = tuple(
                        task for task in pair_plan.tasks if task.platform == platform
                    )
                    if not platform_tasks:
                        continue
                    for task in platform_tasks:
                        fingerprint = canonical_acquisition_fingerprint(task)
                        existing_offset = acquisition_offsets_ms.get(fingerprint)
                        if existing_offset is not None:
                            absolute_offsets[task.id] = existing_offset
                            continue
                        # The runtime ledger is authoritative for the provider
                        # lane.  A new acquisition always takes the next
                        # contiguous slot; pair-local planner offsets are only
                        # an ordering hint and must not create holes.  A
                        # duplicate fingerprint inherits its first slot above.
                        absolute_offset = provider_next_available_ms[platform]
                        absolute_offsets[task.id] = absolute_offset
                        acquisition_offsets_ms[fingerprint] = absolute_offset
                        provider_next_available_ms[platform] = absolute_offset + interval_ms
            tasks = tuple(
                task.model_copy(update={"scheduled_offset_ms": absolute_offsets[task.id]})
                for task in pair_plan.tasks
            )
            pair_intent = self._intent(
                effective_window,
                pair,
                effective_constraints,
                stay_area_search_profile=stay_area_search_profile,
                stay_plan_candidate_set=stay_plan_candidate_set,
            )
            pair_query = self._query(
                effective_window,
                pair,
                stay_area_search_profile=stay_area_search_profile,
                stay_plan_candidate_set=stay_plan_candidate_set,
            )
            elapsed_ms = max(
                0,
                int((self._monotonic() - schedule_started) * 1000),
            )
            delays: dict[str, int] = {}
            try:
                delays = self._source_delays(tasks, elapsed_ms)
                if worker_pool_enabled and worker_index is None:  # pragma: no cover
                    raise RuntimeError("full-date worker pool lost its worker slot")
                live_run = await self._run_live_pair(
                    pair_intent,
                    pair_query,
                    mode=mode,
                    timeout_seconds=timeout_seconds,
                    source_start_delays_ms=delays,
                    memory_access=memory_access,
                    publication_refresh_minimum_options=publication_refresh_minimum_options,
                    model_agents_enabled=not bulk_exploration,
                    exploration_only=bulk_exploration,
                )
            # A date-specific external/provider failure is isolatable.  Do
            # not turn programming errors (AssertionError/TypeError/etc.) or
            # required-model contract failures (ValueError) into an innocent
            # "one date failed" result; those must fail the whole run so the
            # defect is observable.
            except SourceScheduleBudgetExceeded as exc:
                return FlexiblePairExecution(
                    date_pair=pair,
                    query_tasks=tasks,
                    source_start_delays_ms=exc.delays,
                    state=FlexiblePairState.FAILED,
                    failure_class=type(exc).__name__,
                    failure_message=str(exc),
                )
            except TimeoutError as exc:
                return FlexiblePairExecution(
                    date_pair=pair,
                    query_tasks=tasks,
                    source_start_delays_ms=delays,
                    state=FlexiblePairState.FAILED,
                    failure_class=type(exc).__name__,
                    failure_message=str(exc),
                )
            except RuntimeError as exc:
                # The concrete live DAG uses RuntimeError for broken
                # invariants/seal failures; let TaskGroup cancel its sibling
                # workers.  Test/injected providers retain the historical
                # date-isolation contract for their typed RuntimeError.
                if isinstance(self._live, LivePackageAgentSystem):
                    raise
                return FlexiblePairExecution(
                    date_pair=pair,
                    query_tasks=tasks,
                    source_start_delays_ms=delays,
                    state=FlexiblePairState.FAILED,
                    failure_class=type(exc).__name__,
                    failure_message=str(exc),
                )
            return FlexiblePairExecution(
                date_pair=pair,
                query_tasks=tasks,
                source_start_delays_ms=delays,
                state=FlexiblePairState.COMPLETED,
                run=live_run,
            )

        execution_by_pair: dict[str, FlexiblePairExecution] = {}
        execution_order: list[str] = []
        exact_observations: list[ExactDatePairObservation] = []
        refinement_trace: list[AdaptiveRefinementDecision] = []
        planned_ids = set(query_plan.selected_pair_ids)
        planned_pairs = (
            *(pairs[pair_id] for pair_id in query_plan.selected_pair_ids),
            *(item for item in exploration.candidates if item.id not in planned_ids),
        )
        if effective_window.return_date_targets and effective_window.latest_arrival_date:
            safe_return = max(effective_window.return_date_targets)
            if safe_return == effective_window.latest_arrival_date:
                safe_return -= timedelta(days=1)
            safe_pair = next(
                (
                    item
                    for item in exploration.candidates
                    if item.return_date == safe_return
                    and item.night_count == max(
                        effective_window.min_nights,
                        effective_window.max_nights - 1,
                    )
                ),
                None,
            )
            if safe_pair is not None:
                planned_pairs = (
                    safe_pair,
                    *(item for item in planned_pairs if item.id != safe_pair.id),
                )
        # RankedTopKDateRefiner deliberately follows the audited ``rank`` field,
        # not tuple order.  Re-rank this server-owned execution frontier after
        # the boundary guard so the safe pair cannot be silently displaced by
        # an older coarse rank.  The final plan is rebuilt from actual execution
        # order below, so these transient ranks never become evidence of a
        # result that was not run.
        planned_pairs = tuple(
            item.model_copy(update={"rank": index})
            for index, item in enumerate(planned_pairs, start=1)
        )

        async def record_execution(
            pair: AuditableDatePair,
            execution: FlexiblePairExecution,
        ) -> None:
            execution_by_pair[pair.id] = execution
            execution_order.append(pair.id)
            if pair_checkpoint_reporter is not None:
                await pair_checkpoint_reporter(
                    self._pair_checkpoint(
                        execution,
                        sequence=len(execution_order),
                        request_sha256=cast(str, checkpoint_request_sha256),
                    )
                )
            if search_run_recorder is not None and execution.run is not None:
                await search_run_recorder(execution.run)
            live_run = execution.run
            package = live_run.package if live_run is not None else None
            exact_observations.append(
                ExactDatePairObservation(
                    date_pair_id=pair.id,
                    total_budget_cents=(
                        package.budget.total_cents if package is not None else None
                    ),
                    recommendable=(
                        live_run is not None
                        and package is not None
                        and package.budget.is_all_in_total
                        and live_run.decision.state == PackageDecisionState.ACCEPT
                        and (
                            mode != LiveCoverageMode.STRICT
                            or live_run.all_platforms_complete
                            or (
                                live_run.exact_quote_comparison_coverage is not None
                                and (
                                    live_run.exact_quote_comparison_coverage
                                    .single_source_publishable
                                )
                            )
                        )
                        and (
                            mode != LiveCoverageMode.STRICT
                            or (
                                live_run.exact_quote_comparison_coverage is not None
                                and (
                                    live_run.exact_quote_comparison_coverage.complete
                                    or (
                                        live_run.exact_quote_comparison_coverage
                                        .single_source_publishable
                                    )
                                )
                            )
                        )
                        and (
                            publication_refresh_minimum_options == 0
                            or self._is_sealed_exploration(live_run)
                        )
                    ),
                )
            )

        if effective_exact_pair_budget > 1 and isinstance(
            self._date_refiner, RankedTopKDateRefiner
        ):
            batch = planned_pairs[:effective_exact_pair_budget]
            pair_queue: asyncio.Queue[tuple[int, AuditableDatePair] | None] = asyncio.Queue()
            for schedule_index, pair in enumerate(batch):
                pair_queue.put_nowait((schedule_index, pair))
            for _ in range(pair_worker_count):
                pair_queue.put_nowait(None)

            async def worker(worker_index: int) -> None:
                while True:
                    item = await pair_queue.get()
                    try:
                        if item is None:
                            return
                        _, pair = item
                        execution = await execute_pair(
                            pair,
                            worker_index=worker_index if worker_pool_enabled else None,
                        )
                        await record_execution(pair, execution)
                    finally:
                        pair_queue.task_done()

            async with asyncio.TaskGroup() as task_group:
                for worker_index in range(pair_worker_count):
                    task_group.create_task(worker(worker_index))
        while len(execution_by_pair) < effective_exact_pair_budget:
            refinement = self._date_refiner.next_pair(
                planned_pairs,
                tuple(exact_observations),
                exact_pair_budget=effective_exact_pair_budget,
            )
            refinement_trace.append(refinement)
            if refinement.selected_pair_id is None:
                break
            pair = pairs[refinement.selected_pair_id]
            execution = await execute_pair(
                pair,
                worker_index=0 if worker_pool_enabled else None,
            )
            await record_execution(pair, execution)
        executions = tuple(
            execution_by_pair[pair.id]
            for pair in planned_pairs
            if pair.id in execution_by_pair
        )
        if not executions:
            raise RuntimeError("bounded exact-date acquisition produced no pair execution")
        # Rebuild the auditable final plan from the dates actually executed.
        # Its hash, task counts and omitted IDs therefore describe reality even
        # when an injected acquisition policy left the initial Query-Agent shortlist.
        executed_set = set(execution_by_pair)
        final_order = (
            *(item for item in planned_pairs if item.id in executed_set),
            *(item for item in exploration.candidates if item.id not in executed_set),
        )
        final_exploration = exploration.model_copy(
            update={
                "candidates": tuple(
                    item.model_copy(update={"rank": index})
                    for index, item in enumerate(final_order, start=1)
                )
            }
        )
        query_plan = self._query_planner.build(
            effective_window,
            final_exploration,
            self._execution_query_policy(
                effective_window,
                final_exploration,
                policy,
                len(executions),
                stay_plan_candidate_set=stay_plan_candidate_set,
            ),
            stay_plan_candidate_set=stay_plan_candidate_set,
        )
        ranked = self._rank(
            executions,
            mode,
            effective_constraints,
            require_exploration_seal=publication_refresh_minimum_options > 0,
        )
        publication_refresh_shortfall: str | None = None
        publication_attempt_count = 0

        def prepare_publication_attempt_budget(
            *,
            total_attempt_count: int,
            new_attempt_count: int,
        ) -> str | None:
            """Freeze the next publication budget before any refresh side effect."""

            nonlocal scale_directive, agent_template_plan
            if not 1 <= new_attempt_count <= total_attempt_count:
                raise ValueError("publication budget update requires positive attempt counts")
            if total_attempt_count > effective_exact_pair_budget:
                return (
                    "发布重搜预算拒绝继续尝试：累计尝试数 "
                    f"{total_attempt_count} 超过冻结精查上限 {effective_exact_pair_budget}"
                )

            required_new_admissions = _PUBLICATION_REFRESH_MODEL_AGENT_COUNT * new_attempt_count
            request_audit = budget_ledger.audit()
            if required_new_admissions > request_audit.remaining_count:
                return (
                    "发布重搜预算不足，已在浏览器和模型调用前停止："
                    f"本批 {new_attempt_count} 个方案需要预留 "
                    f"{required_new_admissions} 个逻辑 Agent，"
                    f"但全请求只剩 {request_audit.remaining_count}/96 个名额"
                )

            if scale_directive is None:
                return None
            next_directive = self._adaptive_scale_directive(
                exploration,
                mode=mode,
                exact_pair_budget=exact_pair_budget,
                publication_refresh_minimum_options=total_attempt_count,
            )
            if next_directive is None:  # pragma: no cover - guarded by current directive
                raise RuntimeError("adaptive publication budget unexpectedly disappeared")
            next_template_plan = build_agent_template_plan(next_directive)
            candidate_additions = FlexibleLiveAgentRun._candidate_agent_additions(executions)
            dynamic_candidate_agent_count = sum(
                item.additional_model_agent_count for item in candidate_additions
            )
            request_planning_capacity = 96 - budget_scope_start_admitted_count
            projected_request_agents = (
                next_directive.raw_logical_agents + dynamic_candidate_agent_count
            )
            if (
                next_directive.logical_saturated
                or next_template_plan.deferred_instance_count
                or projected_request_agents > request_planning_capacity
            ):
                return (
                    "发布重搜预算不足，已在浏览器和模型调用前停止："
                    f"累计 {total_attempt_count} 次发布尝试需要 "
                    f"{next_directive.raw_logical_agents} 个基础逻辑 Agent，"
                    f"已发现候选 Scout 增量 {dynamic_candidate_agent_count} 个，"
                    f"但本次规划作用域只剩 {request_planning_capacity}/96 个名额"
                )

            # Synchronous assignment makes the validated directive/template pair
            # visible together before the next await can start browser work.
            scale_directive = next_directive
            agent_template_plan = next_template_plan
            return None

        if publication_refresh_minimum_options:
            refresh_candidates = tuple(option for option in ranked if option.recommendable)
            refresh_targets = refresh_candidates[:publication_refresh_minimum_options]
            publication_budget_blocked = False
            if len(refresh_targets) == publication_refresh_minimum_options:
                if not isinstance(self._live, LivePackageAgentSystem):
                    raise ValueError(
                        "publication refresh requires the concrete live browser system"
                    )
                publication_refresh_shortfall = prepare_publication_attempt_budget(
                    total_attempt_count=len(refresh_targets),
                    new_attempt_count=len(refresh_targets),
                )
                publication_budget_blocked = publication_refresh_shortfall is not None
                if not publication_budget_blocked:
                    execution_by_id = {
                        execution.date_pair.id: execution for execution in executions
                    }
                    refresh_slot_count = len(refresh_targets)
                    refreshed = await asyncio.gather(
                        *(
                            self._refresh_execution_for_publication(
                                execution_by_id[option.date_pair_id],
                                effective_window,
                                effective_constraints,
                                effective_policy,
                                mode=mode,
                                timeout_seconds=timeout_seconds,
                                stay_area_search_profile=stay_area_search_profile,
                                stay_plan_candidate_set=stay_plan_candidate_set,
                                memory_access=memory_access,
                                refresh_slot_index=slot_index,
                                refresh_slot_count=refresh_slot_count,
                            )
                            for slot_index, option in enumerate(refresh_targets)
                        )
                    )
                    publication_attempt_count = len(refresh_targets)
                    refreshed_by_id = {execution.date_pair.id: execution for execution in refreshed}
                    executions = tuple(
                        refreshed_by_id.get(execution.date_pair.id, execution)
                        for execution in executions
                    )
            else:
                publication_refresh_shortfall = (
                    "探索阶段只有 "
                    f"{len(refresh_targets)} 个可进入发布重搜的独立日期方案，"
                    f"少于冻结下限 {publication_refresh_minimum_options}"
                )
            ranked = self._rank(
                executions,
                mode,
                effective_constraints,
                require_publication_refresh=True,
            )
            attempted_pair_ids = {item.date_pair_id for item in refresh_targets}
            for fallback in refresh_candidates[publication_refresh_minimum_options:]:
                if publication_budget_blocked:
                    break
                if sum(item.recommendable for item in ranked) >= (
                    publication_refresh_minimum_options
                ):
                    break
                if publication_attempt_count >= effective_exact_pair_budget:
                    break
                if fallback.date_pair_id in attempted_pair_ids:
                    continue
                attempted_pair_ids.add(fallback.date_pair_id)
                budget_shortfall = prepare_publication_attempt_budget(
                    total_attempt_count=publication_attempt_count + 1,
                    new_attempt_count=1,
                )
                if budget_shortfall is not None:
                    publication_refresh_shortfall = budget_shortfall
                    publication_budget_blocked = True
                    break
                execution_by_id = {execution.date_pair.id: execution for execution in executions}
                refreshed_fallback = await self._refresh_execution_for_publication(
                    execution_by_id[fallback.date_pair_id],
                    effective_window,
                    effective_constraints,
                    effective_policy,
                    mode=mode,
                    timeout_seconds=timeout_seconds,
                    stay_area_search_profile=stay_area_search_profile,
                    stay_plan_candidate_set=stay_plan_candidate_set,
                    memory_access=memory_access,
                    refresh_slot_index=0,
                    refresh_slot_count=1,
                )
                publication_attempt_count += 1
                executions = tuple(
                    refreshed_fallback
                    if execution.date_pair.id == fallback.date_pair_id
                    else execution
                    for execution in executions
                )
                ranked = self._rank(
                    executions,
                    mode,
                    effective_constraints,
                    require_publication_refresh=True,
                )
            refreshed_count = sum(item.recommendable for item in ranked)
            binding_passed_count = sum(
                1
                for execution in executions
                if execution.publication_refresh_audit is not None
                and execution.publication_refresh_audit.binding_passed
            )
            if refreshed_count < publication_refresh_minimum_options:
                typed_failures = tuple(
                    reason
                    for execution in executions
                    for reason in (
                        execution.publication_refresh_failure_message,
                        *(
                            execution.publication_refresh_audit.failure_reasons
                            if execution.publication_refresh_audit is not None
                            else ()
                        ),
                    )
                    if reason
                )
                publication_refresh_shortfall = (
                    publication_refresh_shortfall
                    or self._publication_refresh_shortfall_summary(
                        binding_passed_count=binding_passed_count,
                        recommendable_count=refreshed_count,
                        minimum_options=publication_refresh_minimum_options,
                    )
                )
                if typed_failures:
                    publication_refresh_shortfall += "；" + "；".join(typed_failures)
        # The diagnostics retain every ranked option, but the production result
        # carries exactly one final recommendation.  Publication refresh may
        # still re-check an explicitly requested second option for diagnostics.
        recommended = tuple(option.option_id for option in ranked if option.recommendable)[:1]
        publication_refreshed = tuple(
            option.option_id
            for option in ranked
            if any(
                execution.publication_refresh_audit is not None
                and execution.publication_refresh_audit.binding_passed
                and execution.publication_refresh_audit.refreshed_option_id == option.option_id
                for execution in executions
            )
        )
        completed_exploration_count = sum(
            execution.state == FlexiblePairState.COMPLETED for execution in executions
        )
        failed_exploration_count = sum(
            execution.state == FlexiblePairState.FAILED for execution in executions
        )
        sampled = (
            exploration.sampled_not_exhaustive
            or len(query_plan.selected_pair_ids) < effective_window.universe_size
            or bool(query_plan.omitted_pair_ids)
        )
        claim_boundary = self._claim_boundary(
            effective_window,
            query_plan,
            sampled,
            executed_pair_count=len(executions),
            stopped_early=bool(refinement_trace and refinement_trace[-1].stopped_early),
            stay_area_search_profile=stay_area_search_profile,
            stay_plan_candidate_set=stay_plan_candidate_set,
            date_acquisition_policy=self._date_acquisition_policy,
        )
        if scale_directive is not None and agent_template_plan is not None:
            budget_audit = budget_ledger.audit()
            planning_admitted_count = (
                budget_audit.admitted_count - budget_scope_start_admitted_count
            )
            claim_boundary += (
                "本轮启用受控动态 Agent："
                f"日期分片={scale_directive.date_shards}，"
                f"树形归并={scale_directive.date_mergers}，"
                f"请求级模型并发上限={scale_directive.health_adjusted_model_concurrency}，"
                f"计划逻辑实例={agent_template_plan.logical_agent_count}，"
                f"规划阶段实际获准实例={planning_admitted_count}/"
                f"{scale_directive.logical_agent_cap}，"
                f"请求累计实例={budget_audit.admitted_count}/96，"
                f"拒绝实例={budget_audit.rejected_count}；"
                "扩缩容只作用于白名单模型分片，不提高 Chrome、去哪儿住宿或日期对"
                "执行并发，也不改变 Verifier 与发布门。"
            )
        if publication_refresh_minimum_options:
            sealed_exploration_count = sum(
                (exploration_run := execution.exploration_run or execution.run) is not None
                and exploration_run.run_purpose == LiveRunPurpose.EXPLORATION_SELECTION
                and exploration_run.finalization_state == LiveFinalizationState.EXPLORATION_SEALED
                and exploration_run.exploration_seal_passed
                for execution in executions
            )
            claim_boundary += (
                f"本轮请求执行 {len(executions)} 个精确日期探索，其中 "
                f"{sealed_exploration_count} 个完成封存、{failed_exploration_count} 个失败；"
                "只有已封存运行才完成 Search/Planner/Evidence/Curator/Verifier/Critic/"
                "Repair/ReVerifier/ReCritic/Orchestrator 与确定性安全门；"
                "探索阶段显式延后 Explanation、Memory、Publish 且不持久化 memory。"
                "最终推荐只允许来自发布前禁用近时报价复用的限定范围重新搜索；"
                "范围冻结为探索候选的 provider、vertical、日期、人数、房间与地点/"
                "segment，但不要求命中原 offer、航班 provider ID 或唯一酒店 rate；"
                "Planner 可在该范围的新观测中重新选产品。刷新运行必须重新绑定真实 "
                "browser task、报价时间戳以及 "
                "Planner-Verifier-Repair-ReVerifier-主控链，并完整执行 Explanation、"
                "Memory 与 Publish；未刷新探索结果不进入推荐。"
            )
        if recommended:
            final_decision = PackageDecision(
                state=PackageDecisionState.ACCEPT,
                summary=(
                    f"主控在实际完成的 {completed_exploration_count} 个精确日期对中"
                    f"筛出 {len(recommended)} 个可推荐整包"
                    + (
                        f"，其中 {len(publication_refreshed)} 个已完成发布前集中重新核价"
                        if publication_refresh_minimum_options
                        else ""
                    )
                    + (
                        "；注入式 acquisition 的停止条件提前终止了剩余外部查询"
                        if refinement_trace and refinement_trace[-1].stopped_early
                        else ""
                    )
                ),
                evidence_refs=self._recommended_evidence(executions, recommended),
            )
        else:
            final_decision = PackageDecision(
                state=PackageDecisionState.HUMAN_BLOCK,
                summary=(
                    publication_refresh_shortfall
                    or "所有抽样日期对均失败、被验证拒绝或未满足严格三平台覆盖"
                ),
            )
        optimality_status = DateOptimalityStatus.BEST_VERIFIED_IN_EVALUATED_SET
        # No strong admissible-bound certificate is produced by this bounded
        # runner today; without one, the public status must remain non-proven.
        admissible_bounds: tuple[AdmissibleCostBound, ...] = ()
        if len(executions) == effective_window.universe_size and all(
            execution.state == FlexiblePairState.COMPLETED
            and execution.run is not None
            and execution.run.all_platforms_complete
            and execution.run.package is not None
            and execution.run.package.budget.is_all_in_total
            and execution.run.package.final_candidate.currency == "CNY"
            and execution.run.package.final_candidate.flight.party_total_known
            and execution.run.decision.state != PackageDecisionState.HUMAN_BLOCK
            and execution.run.exact_quote_comparison_coverage is not None
            and execution.run.exact_quote_comparison_coverage.complete
            and admissible_bounds
            and all(bound.proven for bound in admissible_bounds)
            for execution in executions
        ):
            optimality_status = DateOptimalityStatus.OPTIMALITY_PROVEN
        claim_boundary += (
            "；日期最优性状态="
            f"{optimality_status.value}。没有同时证明本次旅客总价和其余必要成本均为非负"
            "的 admissible bound 时，不进行日期分支剪枝。"
        )
        model_call_count, model_cost_usd = self._performance_model_metrics(
            query_agentic,
            executions,
        )
        source_delays = tuple(
            delay
            for execution in executions
            for delay in execution.source_start_delays_ms.values()
        )
        planned_logical_query_count = query_plan.logical_task_count
        planned_unique_acquisition_count = query_plan.unique_acquisition_count
        acquisition_metrics = (
            self._acquisition_ledger.detailed_metrics()
            if self._acquisition_ledger is not None
            else {}
        )
        observed = bool(acquisition_metrics)
        # The public logical-query denominator is the frozen execution plan.
        # A retryable terminal failure may call submit_many again; that
        # diagnostic submission count must not inflate the plan's logical
        # query count or make an otherwise valid report self-invalidate.
        logical_query_count = planned_logical_query_count
        delegated_acquisition_count = acquisition_metrics.get(
            "exploration_delegated_acquisitions", 0
        )
        if not observed:
            delegated_acquisition_count = planned_unique_acquisition_count
        if delegated_acquisition_count > logical_query_count:
            raise RuntimeError(
                "acquisition ledger observed more delegated acquisitions than logical submissions"
            )
        deduplicated_query_count = logical_query_count - delegated_acquisition_count
        platform_acquisition_attempt_count = (
            acquisition_metrics.get("platform_acquisition_attempt_count")
            if observed
            else None
        )
        publication_refresh_platform_acquisition_attempt_count = (
            acquisition_metrics.get(
                "publication_refresh_platform_acquisition_attempt_count"
            )
            if observed
            else None
        )
        performance_report = FlexiblePerformanceReport(
            wall_time_seconds=max(0.0, self._monotonic() - run_started),
            internal_benchmark_budget_seconds=(
                INTERNAL_BENCHMARK_TOTAL_TIMEOUT_SECONDS if internal_benchmark else None
            ),
            measurement_basis="observed" if observed else "planned_only",
            planned_logical_query_count=planned_logical_query_count,
            planned_unique_acquisition_count=planned_unique_acquisition_count,
            planned_deduplicated_query_count=(
                planned_logical_query_count - planned_unique_acquisition_count
            ),
            executed_logical_query_count=logical_query_count if observed else None,
            delegate_submit_call_count=(
                acquisition_metrics.get("delegate_submit_call_count") if observed else None
            ),
            delegated_acquisition_count=delegated_acquisition_count if observed else None,
            executed_deduplicated_query_count=(
                deduplicated_query_count if observed else None
            ),
            ledger_shared_return_count=(
                acquisition_metrics.get("ledger_shared_return_count") if observed else None
            ),
            platform_acquisition_attempt_count=platform_acquisition_attempt_count,
            model_call_count=model_call_count,
            model_cost_usd=model_cost_usd,
            publication_refresh_delegated_acquisition_count=(
                acquisition_metrics.get("publication_refresh_delegated_acquisitions")
                if observed
                else None
            ),
            publication_refresh_platform_acquisition_attempt_count=(
                publication_refresh_platform_acquisition_attempt_count
            ),
            recent_quote_reuse_count=(
                acquisition_metrics.get("recent_quote_reuse_count") if observed else None
            ),
            inflight_coalesced_count=(
                acquisition_metrics.get("inflight_coalesced_count") if observed else None
            ),
            unclaimed_cancelled_count=(
                acquisition_metrics.get("unclaimed_cancelled_count") if observed else None
            ),
            process_peak_rss_bytes=self._process_peak_rss_bytes(),
            cpu_time_seconds=max(0.0, self._process_cpu_seconds() - cpu_started),
            max_source_start_delay_ms=max(source_delays, default=0),
            date_pair_count=len(query_plan.selected_pair_ids),
            completed_date_pair_count=completed_exploration_count,
            failed_date_pair_count=failed_exploration_count,
        )
        if (
            internal_benchmark
            and performance_report.wall_time_seconds
            > INTERNAL_BENCHMARK_TOTAL_TIMEOUT_SECONDS
        ):
            raise TimeoutError(
                "internal flexible benchmark exceeded its 530s wall-clock budget: "
                f"{performance_report.wall_time_seconds:.3f}s"
            )
        return FlexibleLiveAgentRun(
            requested_window=window,
            effective_window=effective_window,
            exploration=exploration,
            query_plan=query_plan,
            pair_runs=executions,
            ranked_options=ranked,
            refinement_trace=tuple(refinement_trace),
            recommended_option_ids=recommended,
            final_decision=final_decision,
            query_strategy=query_strategy,
            query_agentic=query_agentic,
            adaptive_scaling_enabled=scale_directive is not None,
            scale_directive=scale_directive,
            agent_template_plan=agent_template_plan,
            agent_budget_audit=budget_ledger.audit(),
            agent_budget_scope_start_admitted_count=budget_scope_start_admitted_count,
            dynamic_candidate_agent_additions=(
                FlexibleLiveAgentRun._candidate_agent_additions(executions)
            ),
            stay_area_search_profile=stay_area_search_profile,
            stay_plan_candidate_set=stay_plan_candidate_set,
            publication_refresh_minimum_options=(publication_refresh_minimum_options),
            publication_refreshed_option_ids=publication_refreshed,
            sampled_not_exhaustive=sampled,
            optimality_status=optimality_status,
            admissible_bounds=admissible_bounds,
            claim_boundary=claim_boundary,
            performance_report=performance_report,
        )

    async def _refresh_execution_for_publication(
        self,
        previous: FlexiblePairExecution,
        window: FlexibleTravelWindow,
        constraints: FlexiblePackageConstraints,
        policy: QueryPlanPolicy,
        *,
        mode: LiveCoverageMode,
        timeout_seconds: int,
        stay_area_search_profile: StayAreaSearchProfile | None,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
        memory_access: MemoryAccessContext | None,
        refresh_slot_index: int,
        refresh_slot_count: int,
    ) -> FlexiblePairExecution:
        if previous.run is None:
            raise ValueError("publication refresh requires a completed exploration run")
        if not self._is_sealed_exploration(previous.run):
            raise ValueError("publication refresh requires a sealed exploration-selection run")
        if not isinstance(self._live, LivePackageAgentSystem):
            raise ValueError("publication refresh requires the concrete live browser system")
        pair = previous.date_pair
        del window, constraints, mode, stay_area_search_profile, stay_plan_candidate_set
        intervals = {
            item.platform.value: item.minimum_interval_ms for item in policy.platform_rates
        }
        started_at = self._utc_now()
        try:
            refreshed_run = await self._live.refresh_selected_components_for_publication(
                previous.run,
                timeout_seconds=timeout_seconds,
                memory_access=memory_access,
                provider_minimum_intervals_ms=intervals,
                refresh_slot_index=refresh_slot_index,
                refresh_slot_count=refresh_slot_count,
            )
        except (TimeoutError, RuntimeError) as exc:
            return previous.model_copy(
                update={
                    "publication_refresh_failure_class": type(exc).__name__,
                    "publication_refresh_failure_message": str(exc),
                }
            )
        completed_at = self._utc_now()
        delays: dict[str, int] = {}
        for task in refreshed_run.scheduler.graph.tasks:
            if task.id not in refreshed_run.source_task_ids:
                continue
            raw_delay = task.input.get("start_delay_ms", 0)
            if not isinstance(raw_delay, int) or isinstance(raw_delay, bool):
                raise RuntimeError("publication refresh returned an invalid source delay")
            delays[task.id] = raw_delay
        audit = self._publication_refresh_audit(
            previous.run,
            refreshed_run,
            refresh_started_at=started_at,
            refresh_completed_at=completed_at,
            source_start_delays_ms=delays,
            refresh_slot_index=refresh_slot_index,
            refresh_slot_count=refresh_slot_count,
        )
        return FlexiblePairExecution(
            date_pair=pair,
            query_tasks=previous.query_tasks,
            source_start_delays_ms=previous.source_start_delays_ms,
            state=FlexiblePairState.COMPLETED,
            run=refreshed_run,
            exploration_run=previous.run,
            publication_refresh_audit=audit,
        )

    def _publication_refresh_audit(
        self,
        previous: LivePackageAgentRun,
        refreshed: LivePackageAgentRun,
        *,
        refresh_started_at: datetime,
        refresh_completed_at: datetime,
        source_start_delays_ms: dict[str, int],
        refresh_slot_index: int,
        refresh_slot_count: int,
    ) -> PublicationRefreshAudit:
        failures: list[str] = []
        if not self._is_sealed_exploration(previous):
            failures.append("publication refresh input is not a sealed exploration run")
        if (
            refreshed.run_purpose != LiveRunPurpose.FINAL_PUBLICATION
            or refreshed.finalization_state != LiveFinalizationState.FINAL_PUBLISHED
            or refreshed.deferred_stage_ids
            or refreshed.exploration_seal_passed
        ):
            failures.append("publication refresh output is not a complete final publication run")
        result_by_id = {result.task_id: result for result in refreshed.scheduler.results}
        graph_by_id = {task.id: task for task in refreshed.scheduler.graph.tasks}
        browser_task_ids: list[str] = []
        reused_browser_task_ids: list[str] = []
        reuse_disabled = True
        for source_task_id in refreshed.source_task_ids:
            graph_task = graph_by_id.get(source_task_id)
            if graph_task is None:
                failures.append(f"{source_task_id}: refreshed graph is missing the source task")
                continue
            submission_raw = graph_task.input.get("submission")
            try:
                submission = BrowserTaskSubmission.model_validate(submission_raw)
            except (TypeError, ValueError):
                failures.append(f"{source_task_id}: refreshed source submission is invalid")
                reuse_disabled = False
                continue
            if submission.query.options.get("__tripchord_allow_recent_quote_reuse") is not False:
                failures.append(f"{source_task_id}: recent quote reuse was not disabled")
                reuse_disabled = False
            result = result_by_id.get(source_task_id)
            snapshot_raw = result.output.get("snapshot") if result is not None else None
            try:
                snapshot = BrowserTaskSnapshot.model_validate(snapshot_raw)
            except (TypeError, ValueError):
                failures.append(f"{source_task_id}: no real browser task snapshot is bound")
                continue
            browser_task_ids.append(snapshot.id)
            if snapshot.reused_from_task_id is not None:
                reused_browser_task_ids.append(snapshot.id)
                failures.append(
                    f"{source_task_id}: browser task reused {snapshot.reused_from_task_id}"
                )
            if snapshot.created_at < refresh_started_at:
                failures.append(f"{source_task_id}: browser task predates refresh start")

        completed_pipeline = tuple(
            task_id
            for task_id in _PUBLICATION_REFRESH_PIPELINE_TASK_IDS
            if task_id in result_by_id and result_by_id[task_id].success
        )
        missing_pipeline = set(_PUBLICATION_REFRESH_PIPELINE_TASK_IDS) - set(completed_pipeline)
        if missing_pipeline:
            failures.append(
                "refreshed evidence did not complete pipeline stages: "
                + ",".join(sorted(missing_pipeline))
            )

        normalized_components: dict[
            str,
            NormalizedFlightQuote | NormalizedLodgingQuote | TransferOption,
        ] = {}
        for normalization in refreshed.normalization_results:
            if normalization.usable and normalization.quote is not None:
                normalized_components[normalization.quote.id] = normalization.quote
                normalized_components.update({item.id: item for item in normalization.transfers})
        previous_candidate = (
            previous.package.final_candidate if previous.package is not None else None
        )
        refreshed_candidate = (
            refreshed.package.final_candidate if refreshed.package is not None else None
        )
        if refreshed.selected_stay_plan_id != previous.selected_stay_plan_id:
            failures.append("publication refresh changed the pre-frozen stay-plan scope")
        if (
            refreshed.coverage != previous.coverage
            or refreshed.all_platforms_complete != previous.all_platforms_complete
            or refreshed.source_execution_completeness != previous.source_execution_completeness
            or refreshed.exact_quote_comparison_coverage != previous.exact_quote_comparison_coverage
        ):
            failures.append(
                "publication refresh did not preserve the sealed exploration source/quote "
                "coverage receipt"
            )
        browser_component_ids: list[str] = []
        fresh_component_ids: list[str] = []
        fresh_evidence_refs: list[str] = []
        if refreshed_candidate is None:
            failures.append("refreshed run did not produce a package candidate")
        else:
            candidate_components = (
                refreshed_candidate.flight,
                *refreshed_candidate.lodgings,
                *refreshed_candidate.transfers,
            )
            browser_providers = {item.value for item in BrowserProvider}
            for component in candidate_components:
                if component.provider not in browser_providers:
                    continue
                browser_component_ids.append(component.id)
                normalized = normalized_components.get(component.id)
                if normalized is None:
                    failures.append(
                        f"{component.id}: refreshed candidate is not bound to normalization output"
                    )
                    continue
                if normalized.captured_at < refresh_started_at:
                    failures.append(f"{component.id}: normalized quote predates refresh start")
                    continue
                if not normalized.is_fresh(refresh_completed_at):
                    failures.append(f"{component.id}: normalized quote expired before refresh end")
                    continue
                fresh_component_ids.append(component.id)
                fresh_evidence_refs.extend(component.evidence_refs)

        stay_plan_id = refreshed.selected_stay_plan_id
        refreshed_option_id = (
            f"{refreshed.intent.trip_id.removeprefix('flexible:')}:{stay_plan_id.value}"
            if refreshed_candidate is not None and stay_plan_id is not None
            else refreshed.intent.trip_id.removeprefix("flexible:")
            if refreshed_candidate is not None
            else None
        )
        binding_passed = not failures
        return PublicationRefreshAudit(
            refresh_started_at=refresh_started_at,
            refresh_completed_at=refresh_completed_at,
            refresh_slot_index=refresh_slot_index,
            refresh_slot_count=refresh_slot_count,
            recent_quote_reuse_disabled=reuse_disabled,
            source_start_delays_ms=source_start_delays_ms,
            source_task_ids=refreshed.source_task_ids,
            browser_task_ids=tuple(browser_task_ids),
            reused_browser_task_ids=tuple(reused_browser_task_ids),
            pipeline_task_ids=completed_pipeline,
            previous_candidate_id=(
                previous_candidate.id if previous_candidate is not None else None
            ),
            refreshed_candidate_id=(
                refreshed_candidate.id if refreshed_candidate is not None else None
            ),
            refreshed_option_id=refreshed_option_id,
            browser_candidate_component_ids=tuple(browser_component_ids),
            fresh_browser_component_ids=tuple(fresh_component_ids),
            fresh_evidence_refs=tuple(dict.fromkeys(fresh_evidence_refs)),
            binding_passed=binding_passed,
            failure_reasons=tuple(failures),
        )

    def _exact_query_policy(
        self,
        policy: QueryPlanPolicy | None,
        exact_pair_budget: int,
    ) -> QueryPlanPolicy:
        if policy is None:
            return QueryPlanPolicy(
                max_exact_pairs=exact_pair_budget,
                platform_rates=tuple(
                    PlatformRatePolicy(platform=platform) for platform in LIVE_V5_PLATFORMS
                ),
            )
        configured_cap = policy.max_exact_pairs or exact_pair_budget
        return policy.model_copy(update={"max_exact_pairs": min(configured_cap, exact_pair_budget)})

    def _execution_query_policy(
        self,
        window: FlexibleTravelWindow,
        exploration: DateExplorationResult,
        policy: QueryPlanPolicy | None,
        exact_pair_budget: int,
        *,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> QueryPlanPolicy:
        effective = self._exact_query_policy(policy, exact_pair_budget)
        if (
            exact_pair_budget <= DIRECT_DATE_PAIR_LIMIT
            or effective.max_exact_pairs != exact_pair_budget
        ):
            return effective
        if not exploration.candidates:  # pragma: no cover - explorer invariant
            raise ValueError("full-date execution requires at least one explored pair")
        probe_policy = effective.model_copy(
            update={
                "max_total_tasks": 10_000,
                "max_exact_pairs": 1,
                "platform_rates": tuple(
                    item.model_copy(update={"max_tasks": 10_000})
                    for item in effective.platform_rates
                ),
            }
        )
        probe = self._query_planner.build(
            window,
            exploration.model_copy(update={"candidates": exploration.candidates[:1]}),
            probe_policy,
            stay_plan_candidate_set=stay_plan_candidate_set,
        )
        tasks_per_platform = {
            platform: sum(task.platform == platform for task in probe.tasks)
            for platform in (item.platform for item in effective.platform_rates)
        }
        return effective.model_copy(
            update={
                "max_total_tasks": max(
                    effective.max_total_tasks,
                    len(probe.tasks) * exact_pair_budget,
                ),
                "platform_rates": tuple(
                    item.model_copy(
                        update={
                            "max_tasks": max(
                                item.max_tasks,
                                tasks_per_platform[item.platform] * exact_pair_budget,
                            )
                        }
                    )
                    for item in effective.platform_rates
                ),
            }
        )

    async def _run_live_pair(
        self,
        pair_intent: PackageIntent,
        pair_query: BrowserSearchQuery,
        *,
        mode: LiveCoverageMode,
        timeout_seconds: int,
        source_start_delays_ms: dict[str, int],
        memory_access: MemoryAccessContext | None,
        publication_refresh_minimum_options: int,
        model_agents_enabled: bool = True,
        exploration_only: bool = False,
    ) -> LivePackageAgentRun:
        if isinstance(self._live, LivePackageAgentSystem):
            return await self._live.run(
                pair_intent,
                pair_query,
                mode=mode,
                purpose=(
                    LiveRunPurpose.EXPLORATION_SELECTION
                    if exploration_only or publication_refresh_minimum_options > 0
                    else LiveRunPurpose.FINAL_PUBLICATION
                ),
                model_agents_enabled=model_agents_enabled,
                timeout_seconds=timeout_seconds,
                source_start_delays_ms=source_start_delays_ms,
                memory_access=memory_access,
            )
        return await self._live.run(
            pair_intent,
            pair_query,
            mode=mode,
            timeout_seconds=timeout_seconds,
            source_start_delays_ms=source_start_delays_ms,
        )

    def _adaptive_scale_directive(
        self,
        exploration: DateExplorationResult,
        *,
        mode: LiveCoverageMode,
        exact_pair_budget: int = 1,
        publication_refresh_minimum_options: int = 0,
    ) -> ScaleDirective | None:
        if not self._adaptive_agent_scaling_enabled:
            return None

        # Before live search, quote-source health is unknown rather than healthy.
        # Missing/stale coarse calendars are workload evidence, not a provider or
        # model-endpoint health signal. Exact Source/coverage gates remain
        # authoritative after execution; model-call failures are handled by the
        # runtime additive-increase/halve controller.
        provider_health = (
            ProviderHealth(
                provider=TravelPlatform.CTRIP.value,
                vertical="lodging",
                required=True,
                status=ProviderHealthStatus.UNKNOWN,
            ),
            ProviderHealth(
                provider=TravelPlatform.QUNAR.value,
                vertical="lodging",
                required=True,
                status=ProviderHealthStatus.UNKNOWN,
            ),
            ProviderHealth(
                provider=TravelPlatform.TONGCHENG.value,
                vertical="flight",
                required=False,
                status=ProviderHealthStatus.UNKNOWN,
            ),
        )
        # ``AdaptiveControlInput`` counts bounded model-pipeline slots, not the
        # complete browser date universe.  The exact pair budget remains the
        # server-owned execution budget below; feeding a 66-pair universe into
        # the per-stage slot field would fail validation before any date pair
        # can run and would incorrectly turn a full search into a pre-browser
        # rejection.
        adaptive_pair_slots = min(exact_pair_budget, DIRECT_DATE_PAIR_LIMIT)
        return derive_scale_directive(
            AdaptiveControlInput(
                D=len(exploration.candidates),
                C=0,
                G=0,
                R=False,
                E=False,
                exploration_pair_count=(
                    adaptive_pair_slots if publication_refresh_minimum_options else 0
                ),
                publication_pair_count=publication_refresh_minimum_options,
                direct_final_pair_count=(
                    adaptive_pair_slots if publication_refresh_minimum_options == 0 else 0
                ),
                provider_health=provider_health,
                strict_mode=mode == LiveCoverageMode.STRICT,
            )
        )

    async def _query_strategy(
        self,
        window: FlexibleTravelWindow,
        exploration: DateExplorationResult,
        *,
        exact_pair_budget: int,
        memory_access: MemoryAccessContext | None,
        scale_directive: ScaleDirective | None = None,
        allow_adaptive_shards: bool = True,
        task_id: str = "select-flexible-date-query-strategy",
    ) -> tuple[
        QueryStrategyProposal | None,
        AgenticRunSummary,
        DateExplorationResult,
    ]:
        if (
            allow_adaptive_shards
            and scale_directive is not None
            and scale_directive.date_shards > 1
        ):
            return await self._query_strategy_with_shards(
                window,
                exploration,
                exact_pair_budget=exact_pair_budget,
                memory_access=memory_access,
                scale_directive=scale_directive,
            )
        task = AgentTask(
            id=task_id,
            role=AgentRole.QUERY_STRATEGIST,
            goal=(
                "从确定性生成的粗排日期池中，为跨平台精确核价选择最有信息量的"
                f"恰好 {exact_pair_budget} 个日期对"
            ),
            allowed_tools=("inspect_date_search_space",),
            input={
                "risk_level": 1,
                "exact_pair_budget": exact_pair_budget,
                "coarse_candidate_count": len(exploration.candidates),
                "sampled_not_exhaustive": exploration.sampled_not_exhaustive,
            },
        )
        frontier = self._query_strategy_frontier(
            exploration.candidates,
            exact_pair_budget=exact_pair_budget,
        )
        frontier_ids = tuple(item.id for item in frontier)
        full_pool_ids_sha256 = hashlib.sha256(
            "\n".join(item.id for item in exploration.candidates).encode("utf-8")
        ).hexdigest()
        tools = ToolRegistry()

        # A formal evidence run may deliberately admit one real model role so
        # the recorded candidate change is attributable to an actual model
        # call, while keeping all other advisory stages bounded.  The setting
        # is process-local and absent in ordinary product runs.
        formal_model_role = os.environ.get("TRIPCHORD_FORMAL_MODEL_ROLE", "").strip()
        if formal_model_role and formal_model_role != AgentRole.QUERY_STRATEGIST.value:
            result = StructuredLiveModelAgent(
                AgentRole.QUERY_STRATEGIST,
                self._model_router,
                system_prompt="formal bounded run: query selection is server-deterministic",
                output_model=QueryStrategyProposal,
                required=self._model_agents_required,
            ).unavailable_result(task, "formal_model_role_limited")
            return (
                None,
                AgenticRunSummary.from_results(
                    (result,), enabled=self._model_router is not None, required=False
                ),
                exploration,
            )

        async def inspect_date_search_space(_: ToolCall) -> dict[str, JsonValue]:
            return cast(
                dict[str, JsonValue],
                {
                    "window": {
                        "origin": window.origin,
                        "destination": window.destination,
                        "earliest_departure": window.earliest_departure.isoformat(),
                        "latest_departure": window.latest_departure.isoformat(),
                        "min_nights": window.min_nights,
                        "max_nights": window.max_nights,
                    },
                    "exact_pair_budget": exact_pair_budget,
                    "universe_size": window.universe_size,
                    "full_pool_count": len(exploration.candidates),
                    "full_pool_ids_sha256": full_pool_ids_sha256,
                    "frontier_count": len(frontier),
                    "sampled_not_exhaustive": exploration.sampled_not_exhaustive,
                    "prior_observed_pair_count": (
                        exploration.search_metrics.prior_observed_pair_count
                    ),
                    "prior_coverage": str(exploration.search_metrics.prior_coverage),
                    "metric_status": exploration.search_metrics.metric_status.value,
                    "selection_frontier_boundary": (
                        "确定性保留粗排头部锚点，再对完整候选顺序等距覆盖；"
                        "Agent 必须且只能从 frontier_rows 选择 exact_pair_budget 个唯一 ID"
                    ),
                    "frontier_columns": [
                        "id",
                        "rank",
                        "departure_date",
                        "return_date",
                        "night_count",
                        "platform_coverage",
                        "complete_calendar_support",
                        "best_total_for_party_cents",
                    ],
                    "frontier_rows": [
                        [
                            item.id,
                            item.rank,
                            item.departure_date.isoformat(),
                            item.return_date.isoformat(),
                            item.night_count,
                            str(item.platform_coverage),
                            item.complete_calendar_support,
                            item.best_total_for_party_cents,
                        ]
                        for item in frontier
                    ],
                    "missing_platforms": [item.value for item in exploration.missing_platforms],
                    "stale_platforms": [item.value for item in exploration.stale_platforms],
                },
            )

        tools.register(
            ToolSpec(
                name="inspect_date_search_space",
                description=(
                    "读取确定性日期全集大小、粗价证据覆盖、缺失平台和候选日期；"
                    "工具只读且不发起外部查询"
                ),
                permission=ToolPermission.PURE_COMPUTE,
                allowed_roles=(AgentRole.QUERY_STRATEGIST,),
            ),
            inspect_date_search_space,
        )
        budgeted_context = None
        if self._context_builder is not None and memory_access is not None:
            try:
                budgeted_context = self._context_builder.build(
                    role=AgentRole.QUERY_STRATEGIST,
                    purpose=ContextPurpose.QUERY,
                    goal=task.goal,
                    access=memory_access.model_copy(
                        update={"agent_role": AgentRole.QUERY_STRATEGIST}
                    ),
                    current_request=cast(
                        dict[str, JsonValue],
                        {
                            "window": window.model_dump(mode="json"),
                            "exact_pair_budget": exact_pair_budget,
                        },
                    ),
                    rag_text=(f"{window.origin} {window.destination} 自由行 日期 偏好 平台能力"),
                    rag_topics=("user_preference", "provider_capability"),
                    rag_tags=("travel",),
                )
            except (PermissionError, ValueError) as exc:
                agent = StructuredLiveModelAgent(
                    AgentRole.QUERY_STRATEGIST,
                    self._model_router,
                    system_prompt="上下文构建失败时不得猜测查询策略。",
                    output_model=QueryStrategyProposal,
                    required=self._model_agents_required,
                )
                result = agent.unavailable_result(
                    task,
                    f"context_build_failed:{type(exc).__name__}:{exc}",
                )
                if self._model_agents_required:
                    raise ValueError("必需的查询策略 Agent 上下文构建失败") from exc
                return (
                    None,
                    AgenticRunSummary.from_results(
                        (result,), enabled=self._model_router is not None, required=False
                    ),
                    exploration,
                )
        agent = StructuredLiveModelAgent(
            AgentRole.QUERY_STRATEGIST,
            self._model_router,
            system_prompt=(
                "你是自由行日期查询策略 Agent。必须先调用工具观察候选池，再在给定"
                "ID 中做探索/利用权衡：兼顾低价先验、平台覆盖不确定性、日期与停留"
                "时长多样性。必须选择恰好 exact_pair_budget 个唯一 ID，不能声称全月"
                "最低价，不能生成新日期、缩小或扩大硬查询预算。工具返回后必须输出"
                "完整 JSON 对象，五个必需字段一个都不能省略：summary、selected_pair_ids、"
                "selection_reasons、stop_condition、query_budget_pairs；其中 stop_condition"
                "必须是非空字符串，query_budget_pairs 必须是整数且等于 exact_pair_budget。"
                "即使没有额外理由，也要输出 selection_reasons 数组（可为空）。输出形状示例："
                "{\"summary\":\"...\",\"selected_pair_ids\":[\"id\"],"
                "\"selection_reasons\":[],\"stop_condition\":\"达到固定精查预算\","
                "\"query_budget_pairs\":1,\"uncertainty_flags\":[]}。"
            ),
            output_model=QueryStrategyProposal,
            required=self._model_agents_required,
        )
        result = await agent.execute(
            task,
            ContextEngine(EvidenceBlackboard()),
            tools,
            budgeted_context=budgeted_context,
            proposal_policy=lambda proposal: self._query_strategy_proposal_failure(
                proposal,
                allowed_frontier_ids=frontier_ids,
                exact_pair_budget=exact_pair_budget,
            ),
            proposal_policy_name="query_strategy_frontier_and_budget_v1",
            proposal_policy_context=cast(
                dict[str, JsonValue],
                {
                    "allowed_frontier_ids": list(frontier_ids),
                    "exact_pair_budget": exact_pair_budget,
                    "requirements": [
                        "selected_pair_ids 数量必须等于 exact_pair_budget",
                        "selected_pair_ids 必须唯一且全部来自 allowed_frontier_ids",
                        "query_budget_pairs 必须等于 exact_pair_budget",
                    ],
                },
            ),
        )
        agentic = AgenticRunSummary.from_results(
            (result,),
            enabled=self._model_router is not None,
            required=self._model_agents_required,
        )
        if result.output.get("agent_required_failed"):
            trace_payload = result.output.get("agentic_trace")
            failure = "unknown"
            logical_requests = 0
            proposal_repairs = 0
            protocol_repairs = 0
            if isinstance(trace_payload, dict):
                raw_failure = trace_payload.get("failure")
                if isinstance(raw_failure, str) and raw_failure.strip():
                    # The trace never stores prompts or credentials.  Still cap and
                    # flatten the model/schema diagnostic before exposing it through
                    # the loopback API so one malformed answer cannot bloat logs.
                    failure = " ".join(raw_failure.split())[:480]
                raw_logical_requests = trace_payload.get("logical_request_count")
                if isinstance(raw_logical_requests, int):
                    logical_requests = raw_logical_requests
                raw_proposal_repairs = trace_payload.get("proposal_repair_count")
                if isinstance(raw_proposal_repairs, int):
                    proposal_repairs = raw_proposal_repairs
                raw_protocol_repairs = trace_payload.get("tool_protocol_repair_count")
                if isinstance(raw_protocol_repairs, int):
                    protocol_repairs = raw_protocol_repairs
            raise ValueError(
                "必需的查询策略 Agent 未能完成结构化决策："
                f"failure={failure}; logical_requests={logical_requests}; "
                f"proposal_repairs={proposal_repairs}; "
                f"tool_protocol_repairs={protocol_repairs}"
            )
        raw = proposal_from_result(result, QueryStrategyProposal)
        proposal = cast(QueryStrategyProposal | None, raw)
        if proposal is None:
            return None, agentic, exploration

        known = set(frontier_ids)
        proposed = proposal.selected_pair_ids
        if len(proposed) != len(set(proposed)) or any(item not in known for item in proposed):
            if self._model_agents_required:
                raise ValueError("查询策略 Agent 返回了重复或未知的日期对 ID")
            return None, agentic, exploration
        effective_query_budget = exact_pair_budget
        # Explicit return-date targets are user constraints, not model-ranking
        # hints.  OTA return_date is the departure date, while the user's
        # boundary is actual arrival home.  For the MLE→HGH overnight route,
        # reserve one day for that arrival so a bounded run cannot publish a
        # flight departing on 2026-09-10 and arriving after the deadline.
        safe_return_target = (
            max(window.return_date_targets) if window.return_date_targets else None
        )
        target_nights = window.max_nights
        if (
            safe_return_target is not None
            and window.latest_arrival_date is not None
            and safe_return_target == window.latest_arrival_date
        ):
            safe_return_target -= timedelta(days=1)
            target_nights = max(window.min_nights, window.max_nights - 1)
        required_target = next(
            (
                item
                for item in exploration.candidates
                if safe_return_target is not None
                and item.return_date == safe_return_target
                and item.night_count == target_nights
            ),
            None,
        )
        selected = tuple(
            dict.fromkeys(
                (
                    *((required_target.id,) if required_target is not None else ()),
                    *proposed,
                )
            )
        )[:effective_query_budget]
        if len(selected) < effective_query_budget:
            selected = tuple(
                dict.fromkeys(
                    (
                        *selected,
                        *(item.id for item in exploration.candidates),
                    )
                )
            )[:effective_query_budget]
        if not selected:
            if self._model_agents_required:
                raise ValueError("查询策略 Agent 未返回可执行日期对")
            return None, agentic, exploration
        sanitized = proposal.model_copy(
            update={
                "selected_pair_ids": selected,
                "query_budget_pairs": effective_query_budget,
            }
        )
        by_id = {item.id: item for item in exploration.candidates}
        ordered_ids = (
            *selected,
            *(item.id for item in exploration.candidates if item.id not in selected),
        )
        reordered = tuple(
            by_id[pair_id].model_copy(update={"rank": index})
            for index, pair_id in enumerate(ordered_ids, start=1)
        )
        return (
            sanitized,
            agentic,
            exploration.model_copy(
                update={
                    "candidates": reordered,
                    "warnings": (
                        *exploration.warnings,
                        "查询策略 Agent 仅重排确定性候选；最终精查数量仍由硬预算门裁剪",
                    ),
                }
            ),
        )

    async def _query_strategy_with_shards(
        self,
        window: FlexibleTravelWindow,
        exploration: DateExplorationResult,
        *,
        exact_pair_budget: int,
        memory_access: MemoryAccessContext | None,
        scale_directive: ScaleDirective,
    ) -> tuple[
        QueryStrategyProposal | None,
        AgenticRunSummary,
        DateExplorationResult,
    ]:
        candidates = exploration.candidates
        shards = tuple(
            candidates[index : index + _QUERY_STRATEGY_FRONTIER_LIMIT]
            for index in range(0, len(candidates), _QUERY_STRATEGY_FRONTIER_LIMIT)
        )
        if len(shards) != scale_directive.date_shards:
            raise ValueError("adaptive date shards conflict with the frozen scale directive")
        local_budget = max(1, (exact_pair_budget + len(shards) - 1) // len(shards))
        concurrency_gate = AdaptiveModelConcurrencyGate(
            max(
                1,
                min(len(shards), scale_directive.health_adjusted_model_concurrency),
            ),
        )

        async def inspect_shard(
            index: int,
            shard: tuple[AuditableDatePair, ...],
        ) -> tuple[
            QueryStrategyProposal | None,
            AgenticRunSummary,
            DateExplorationResult,
        ]:
            await concurrency_gate.acquire()
            successful = False
            try:
                shard_budget = min(local_budget, len(shard), 8)
                shard_exploration = exploration.model_copy(
                    update={
                        "candidates": tuple(
                            item.model_copy(update={"rank": rank})
                            for rank, item in enumerate(shard, start=1)
                        ),
                        "warnings": (
                            *exploration.warnings,
                            f"动态日期审计分片 {index + 1}/{len(shards)}；"
                            "分片结论必须由最终合并 Agent 再裁决",
                        ),
                    }
                )
                result = await self._query_strategy(
                    window,
                    shard_exploration,
                    exact_pair_budget=shard_budget,
                    memory_access=memory_access,
                    scale_directive=None,
                    allow_adaptive_shards=False,
                    task_id=f"select-flexible-date-shard-{index + 1}",
                )
                successful = all(stage.failure is None for stage in result[1].stages)
                return result
            finally:
                await concurrency_gate.release(successful=successful)

        shard_results = await asyncio.gather(
            *(inspect_shard(index, shard) for index, shard in enumerate(shards))
        )
        concurrency_audit = concurrency_gate.audit()
        concurrency_audits = [concurrency_audit]
        selected_ids: list[str] = []
        summaries: list[AgenticRunSummary] = []
        for shard, (proposal, summary, reordered_shard) in zip(
            shards,
            shard_results,
            strict=True,
        ):
            summaries.append(summary)
            proposed = (
                proposal.selected_pair_ids
                if proposal is not None
                else tuple(item.id for item in reordered_shard.candidates[:local_budget])
            )
            for pair_id in proposed:
                if pair_id not in selected_ids:
                    selected_ids.append(pair_id)
            if not proposed and shard:
                selected_ids.append(shard[0].id)
        for candidate in candidates:
            if len(selected_ids) >= exact_pair_budget:
                break
            if candidate.id not in selected_ids:
                selected_ids.append(candidate.id)

        by_id = {item.id: item for item in candidates}
        if len(selected_ids) > _QUERY_STRATEGY_FRONTIER_LIMIT:
            merge_groups = tuple(
                tuple(selected_ids[index : index + _QUERY_STRATEGY_FRONTIER_LIMIT])
                for index in range(0, len(selected_ids), _QUERY_STRATEGY_FRONTIER_LIMIT)
            )
            merge_gate = AdaptiveModelConcurrencyGate(
                max(
                    1,
                    min(
                        len(merge_groups),
                        scale_directive.health_adjusted_model_concurrency,
                    ),
                )
            )
            per_group_capacity = _QUERY_STRATEGY_FRONTIER_LIMIT // len(merge_groups)
            remainder = _QUERY_STRATEGY_FRONTIER_LIMIT % len(merge_groups)

            async def merge_group(
                index: int,
                group_ids: tuple[str, ...],
            ) -> tuple[
                QueryStrategyProposal | None,
                AgenticRunSummary,
                DateExplorationResult,
            ]:
                await merge_gate.acquire()
                successful = False
                try:
                    group_budget = min(
                        len(group_ids),
                        per_group_capacity + int(index < remainder),
                    )
                    group_exploration = exploration.model_copy(
                        update={
                            "candidates": tuple(
                                by_id[pair_id].model_copy(update={"rank": rank})
                                for rank, pair_id in enumerate(group_ids, start=1)
                            ),
                            "warnings": (
                                *exploration.warnings,
                                f"日期分片树形归并 {index + 1}/{len(merge_groups)}；"
                                "每个归并 Agent 最多观察 12 个合法分片胜者",
                            ),
                        }
                    )
                    result = await self._query_strategy(
                        window,
                        group_exploration,
                        exact_pair_budget=group_budget,
                        memory_access=memory_access,
                        scale_directive=None,
                        allow_adaptive_shards=False,
                        task_id=f"merge-flexible-date-group-{index + 1}",
                    )
                    successful = all(stage.failure is None for stage in result[1].stages)
                    return result
                finally:
                    await merge_gate.release(successful=successful)

            group_results = await asyncio.gather(
                *(merge_group(index, group) for index, group in enumerate(merge_groups))
            )
            concurrency_audits.append(merge_gate.audit())
            intermediate_ids: list[str] = []
            for index, (group_ids, (proposal, summary, reordered_group)) in enumerate(
                zip(merge_groups, group_results, strict=True)
            ):
                summaries.append(summary)
                fallback_budget = min(
                    len(group_ids),
                    per_group_capacity + int(index < remainder),
                )
                nominated = (
                    proposal.selected_pair_ids
                    if proposal is not None
                    else tuple(item.id for item in reordered_group.candidates[:fallback_budget])
                )
                for pair_id in nominated:
                    if pair_id in group_ids and pair_id not in intermediate_ids:
                        intermediate_ids.append(pair_id)
            selected_ids = intermediate_ids

        merger_candidates = tuple(
            by_id[pair_id].model_copy(update={"rank": rank})
            for rank, pair_id in enumerate(selected_ids, start=1)
        )
        merger_exploration = exploration.model_copy(
            update={
                "candidates": merger_candidates,
                "warnings": (
                    *exploration.warnings,
                    f"{len(shards)} 个日期分片 Agent 产生 "
                    f"{len(merger_candidates)} 个合法候选，等待合并 Agent 裁决",
                ),
            }
        )
        proposal, merger_summary, reordered_merger = await self._query_strategy(
            window,
            merger_exploration,
            exact_pair_budget=exact_pair_budget,
            memory_access=memory_access,
            scale_directive=None,
            allow_adaptive_shards=False,
            task_id="merge-flexible-date-query-strategy",
        )
        summaries.append(merger_summary)
        combined_summary = AgenticRunSummary.combine(tuple(summaries)).model_copy(
            update={"model_concurrency_audits": tuple(concurrency_audits)}
        )
        final_selected = (
            proposal.selected_pair_ids
            if proposal is not None
            else tuple(item.id for item in reordered_merger.candidates[:exact_pair_budget])
        )
        final_order = (
            *final_selected,
            *(item.id for item in candidates if item.id not in final_selected),
        )
        reordered_candidates = tuple(
            by_id[pair_id].model_copy(update={"rank": rank})
            for rank, pair_id in enumerate(final_order, start=1)
        )
        return (
            proposal,
            combined_summary,
            exploration.model_copy(
                update={
                    "candidates": reordered_candidates,
                    "warnings": (
                        *exploration.warnings,
                        "动态日期分片已由最终 Query Strategist 合并；"
                        "Agent 数和并发受确定性 ScaleDirective 限制；"
                        f"模型并发运行审计=start:{concurrency_audit.initial_limit},"
                        f"ceiling:{concurrency_audit.ceiling},"
                        f"peak:{concurrency_audit.peak_in_flight},"
                        f"final:{concurrency_audit.final_limit},"
                        f"failures:{concurrency_audit.failure_count}",
                    ),
                }
            ),
        )

    @staticmethod
    def _publication_refresh_shortfall_summary(
        *,
        binding_passed_count: int,
        recommendable_count: int,
        minimum_options: int,
    ) -> str:
        return (
            f"发布重搜有 {binding_passed_count} 个独立日期方案通过新鲜证据绑定审计，"
            f"但只有 {recommendable_count} 个完成最终 ACCEPT，"
            f"少于冻结下限 {minimum_options}"
        )

    @staticmethod
    def _query_strategy_frontier(
        candidates: tuple[AuditableDatePair, ...],
        *,
        exact_pair_budget: int,
    ) -> tuple[AuditableDatePair, ...]:
        """Build a compact, auditable frontier without pretending it is exhaustive.

        The deterministic explorer already ranks calendar evidence and inserts
        early/late/midpoint stay-length anchors.  Preserve enough of that head
        for the exact budget, then cover the remaining ordered pool at stable
        intervals so the model can make a genuine diversity trade-off without
        receiving a truncated 10k-token receipt.
        """

        limit = min(
            len(candidates),
            max(_QUERY_STRATEGY_FRONTIER_LIMIT, exact_pair_budget),
        )
        if len(candidates) <= limit:
            return candidates
        head_count = min(exact_pair_budget, limit)
        selected = list(candidates[:head_count])
        remaining = candidates[head_count:]
        slots = limit - len(selected)
        for index in range(slots):
            position = 0 if slots == 1 else round(index * (len(remaining) - 1) / (slots - 1))
            candidate = remaining[position]
            if candidate not in selected:
                selected.append(candidate)
        if len(selected) < limit:
            selected.extend(item for item in candidates if item not in selected)
        return tuple(selected[:limit])

    @staticmethod
    def _query_strategy_proposal_failure(
        proposal: object,
        *,
        allowed_frontier_ids: tuple[str, ...],
        exact_pair_budget: int,
    ) -> str | None:
        if not isinstance(proposal, QueryStrategyProposal):
            return "proposal is not a QueryStrategyProposal"
        selected = proposal.selected_pair_ids
        if len(selected) != exact_pair_budget:
            return "selected_pair_ids count must equal the hard exact_pair_budget"
        if proposal.query_budget_pairs != exact_pair_budget:
            return "query_budget_pairs must equal the hard exact_pair_budget"
        if len(selected) != len(set(selected)):
            return "selected_pair_ids must be unique"
        unknown = set(selected) - set(allowed_frontier_ids)
        if unknown:
            return "selected_pair_ids contains IDs outside the visible selection frontier"
        return None

    def _source_delays(
        self,
        tasks: tuple[FlexibleQueryTask, ...],
        elapsed_ms: int,
    ) -> dict[str, int]:
        if len(tasks) not in {11, 13, 15, 18}:
            raise ValueError(
                "each flexible date pair must match an audited provider capability profile"
            )
        delays: dict[str, int] = {}
        if len(tasks) in {13, 18}:
            platforms = tuple(dict.fromkeys(task.platform for task in tasks))
            if not platforms:
                raise ValueError("live-v4/v5 requires at least one provider")
            for platform in platforms:
                platform_offsets = [
                    task.scheduled_offset_ms for task in tasks if task.platform == platform
                ]
                expected_count = (
                    1 if platform == TravelPlatform.TONGCHENG and len(tasks) == 13 else 6
                )
                if len(platform_offsets) != expected_count:
                    raise ValueError("live-v4 task count does not match provider capabilities")
        for task in tasks:
            source_id = f"source-{task.platform.value}-{_KIND_SUFFIX[task.kind]}"
            if source_id in delays:
                raise ValueError(f"duplicate source schedule: {source_id}")
            delays[source_id] = max(0, task.scheduled_offset_ms - elapsed_ms)
        if any(delay > _MAX_SOURCE_START_DELAY_MS for delay in delays.values()):
            raise SourceScheduleBudgetExceeded(delays)
        return delays

    @staticmethod
    def _preflight_full_window_deadline(
        query_plan: FlexibleQueryPlan,
        *,
        pair_count: int,
        timeout_seconds: int,
        total_timeout_seconds: int,
    ) -> None:
        last_source_offset_ms = max(
            (task.scheduled_offset_ms for task in query_plan.tasks),
            default=0,
        )
        conservative_execution_budget_ms = (
            max(timeout_seconds, 120) + _FULL_WINDOW_FINALIZATION_BUFFER_SECONDS
        ) * 1000
        if (
            last_source_offset_ms + conservative_execution_budget_ms
            > total_timeout_seconds * 1000
        ):
            raise FullWindowDeadlineInfeasible(
                pair_count=pair_count,
                last_source_offset_ms=last_source_offset_ms,
                conservative_execution_budget_ms=conservative_execution_budget_ms,
                total_timeout_seconds=total_timeout_seconds,
            )

    def _intent(
        self,
        window: FlexibleTravelWindow,
        pair: AuditableDatePair,
        constraints: FlexiblePackageConstraints,
        *,
        stay_area_search_profile: StayAreaSearchProfile | None,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> PackageIntent:
        return PackageIntent(
            trip_id=f"flexible:{pair.id}",
            origin=window.origin,
            destination=window.destination,
            destination_place_key=(
                PackagePlaceKey.MAAFUSHI
                if stay_area_search_profile is not None and stay_plan_candidate_set is None
                else None
            ),
            start_date=pair.departure_date,
            end_date=pair.return_date,
            latest_arrival_date=window.latest_arrival_date,
            adults=window.adults,
            children=window.children,
            infants=window.infants,
            rooms=window.rooms,
            currency=window.currency,
            budget_cents=constraints.budget_cents,
            require_checked_baggage=constraints.require_checked_baggage,
            allow_connections=constraints.allow_connections,
            require_breakfast=constraints.require_breakfast,
            breakfast_preference_mode=constraints.breakfast_preference_mode,
            breakfast_preference_weight=constraints.breakfast_preference_weight,
            minimum_arrival_to_boat_minutes=(constraints.minimum_arrival_to_boat_minutes),
            minimum_airport_buffer_minutes=(constraints.minimum_airport_buffer_minutes),
            maximum_quote_capture_skew_minutes=(constraints.maximum_quote_capture_skew_minutes),
        )

    def _query(
        self,
        window: FlexibleTravelWindow,
        pair: AuditableDatePair,
        *,
        stay_area_search_profile: StayAreaSearchProfile | None,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
    ) -> BrowserSearchQuery:
        options = (
            {
                "gateway_destination": stay_area_search_profile.gateway_destination,
                "stay_area_search_profile": stay_area_search_profile.model_dump(mode="json"),
            }
            if stay_area_search_profile is not None
            else {}
        )
        if stay_plan_candidate_set is not None:
            options = {
                **options,
                "stay_plan_candidate_set": stay_plan_candidate_set.model_dump(mode="json"),
            }
        return BrowserSearchQuery(
            origin=window.origin,
            destination=window.destination,
            start_date=pair.departure_date,
            end_date=pair.return_date,
            adults=window.adults,
            children=window.children,
            infants=window.infants,
            party_shape_supported=(window.children == 0 and window.infants == 0),
            party_shape_failure=(
                "unsupported_party_shape: provider adapter lacks child/infant fare contract"
                if window.children or window.infants
                else None
            ),
            rooms=window.rooms,
            currency=window.currency,
            origin_code=window.origin_code,
            destination_code=window.destination_code,
            options=options,
        )

    @staticmethod
    def _is_sealed_exploration(live_run: LivePackageAgentRun) -> bool:
        return (
            live_run.evidence_scope == LiveEvidenceScope.FULL_SEARCH
            and live_run.run_purpose == LiveRunPurpose.EXPLORATION_SELECTION
            and live_run.finalization_state == LiveFinalizationState.EXPLORATION_SEALED
            and live_run.exploration_seal_passed
            and live_run.deferred_stage_ids == _EXPLORATION_DEFERRED_STAGE_IDS
        )

    def _rank(
        self,
        executions: tuple[FlexiblePairExecution, ...],
        mode: LiveCoverageMode,
        constraints: FlexiblePackageConstraints,
        *,
        require_exploration_seal: bool = False,
        require_publication_refresh: bool = False,
    ) -> tuple[FlexibleRankedOption, ...]:
        if require_exploration_seal and require_publication_refresh:
            raise ValueError("ranking cannot require exploration and publication simultaneously")
        provisional: list[FlexibleRankedOption] = []
        complete_cny_ids: set[str] = set()
        for execution in executions:
            live_run = execution.run
            if live_run is None:
                provisional.append(
                    FlexibleRankedOption(
                        rank=1,
                        date_pair_id=execution.date_pair.id,
                        departure_date=execution.date_pair.departure_date,
                        return_date=execution.date_pair.return_date,
                        decision_state=PackageDecisionState.HUMAN_BLOCK,
                        recommendable=False,
                        complete_cny_party_total=False,
                        evidence_completeness=Decimal(0),
                        all_platforms_complete=False,
                        option_id=execution.date_pair.id,
                        objective_values={
                            "price": Decimal(10**18),
                            "evidence": Decimal(0),
                            "robustness": Decimal(0),
                            "convenience": Decimal(0),
                            "schedule_quality": Decimal(0),
                            "breakfast": Decimal(0),
                            "baggage": Decimal(0),
                        },
                        diversity_tags=(
                            f"departure-week:{execution.date_pair.departure_date.isocalendar().week}",
                            f"nights:{execution.date_pair.night_count}",
                        ),
                    )
                )
                continue
            package = live_run.package
            total = package.budget.total_cents if package is not None else None
            completed_sources_by_platform = tuple(
                (platform.terminal_outcome_source_ids or platform.successful_source_ids)
                for platform in live_run.coverage
            )
            source_count = sum(
                len(completed_sources) + len(platform.failed_source_ids)
                for platform, completed_sources in zip(
                    live_run.coverage,
                    completed_sources_by_platform,
                    strict=True,
                )
            )
            completeness = (
                Decimal(sum(len(items) for items in completed_sources_by_platform))
                / Decimal(source_count)
                if source_count
                else Decimal(0)
            )
            stay_plan_id = live_run.selected_stay_plan_id
            option_id = (
                f"{execution.date_pair.id}:{stay_plan_id.value}"
                if stay_plan_id is not None
                else execution.date_pair.id
            )
            complete_cny_party_total = bool(
                package is not None
                and package.budget.is_all_in_total
                and package.final_candidate.currency == "CNY"
                and package.final_candidate.flight.party_total_known
            )
            recommendable = (
                live_run.decision.state == PackageDecisionState.ACCEPT
                and complete_cny_party_total
                and (
                    mode != LiveCoverageMode.STRICT
                    or live_run.all_platforms_complete
                    or (
                        live_run.exact_quote_comparison_coverage is not None
                        and live_run.exact_quote_comparison_coverage.single_source_publishable
                    )
                )
                and (
                    mode != LiveCoverageMode.STRICT
                    or (
                        live_run.exact_quote_comparison_coverage is not None
                        and (
                            live_run.exact_quote_comparison_coverage.complete
                            or live_run.exact_quote_comparison_coverage.single_source_publishable
                        )
                    )
                )
                and (not require_exploration_seal or self._is_sealed_exploration(live_run))
                and (
                    not require_publication_refresh
                    or (
                        live_run.run_purpose == LiveRunPurpose.FINAL_PUBLICATION
                        and live_run.finalization_state == LiveFinalizationState.FINAL_PUBLISHED
                        and not live_run.deferred_stage_ids
                        and not live_run.exploration_seal_passed
                        and execution.exploration_run is not None
                        and self._is_sealed_exploration(execution.exploration_run)
                        and execution.publication_refresh_audit is not None
                        and execution.publication_refresh_audit.binding_passed
                    )
                )
            )
            if recommendable and complete_cny_party_total:
                complete_cny_ids.add(option_id)
            objective_values, diversity_tags = self._objective_values(
                execution,
                live_run,
                total,
                completeness,
            )
            provisional.append(
                FlexibleRankedOption(
                    rank=1,
                    date_pair_id=execution.date_pair.id,
                    departure_date=execution.date_pair.departure_date,
                    return_date=execution.date_pair.return_date,
                    decision_state=live_run.decision.state,
                    recommendable=recommendable,
                    complete_cny_party_total=complete_cny_party_total,
                    total_budget_cents=total,
                    evidence_completeness=completeness,
                    all_platforms_complete=live_run.all_platforms_complete,
                    source_execution_completeness=(live_run.source_execution_completeness),
                    exact_quote_comparison_coverage=(live_run.exact_quote_comparison_coverage),
                    final_candidate_id=(
                        package.final_candidate.id if package is not None else None
                    ),
                    stay_plan_id=stay_plan_id,
                    option_id=option_id,
                    objective_values=objective_values,
                    diversity_tags=diversity_tags,
                )
            )
        selections = ParetoTopKSelector().select(
            tuple(
                DecisionVector(
                    candidate_id=item.option_id,
                    values=item.objective_values,
                    diversity_tags=item.diversity_tags,
                    feasible=item.recommendable,
                )
                for item in provisional
            ),
            constraints.objective_specs(),
        )
        by_id = {item.option_id: item for item in provisional}
        selected_options = tuple(
            by_id[selected.candidate_id].model_copy(
                update={
                    "rank": selected.rank,
                    "weighted_score": selected.weighted_score,
                    "pareto_front": selected.pareto_front,
                    "score_explanation": selected.explanation,
                }
            )
            for selected in selections
        )
        # A publishable, complete CNY party total is the only cross-date
        # comparable primary objective.  Pareto/comfort preferences may retain
        # their diagnostics and break exact-price ties, but must never let a
        # more expensive complete option outrank a cheaper one or let an
        # incomplete/non-CNY observation lead the recommendation frontier.
        ordered = tuple(
            sorted(
                selected_options,
                key=lambda item: (
                    0 if item.option_id in complete_cny_ids else 1,
                    item.total_budget_cents
                    if item.option_id in complete_cny_ids
                    and item.total_budget_cents is not None
                    else 10**18,
                    item.rank,
                    item.option_id,
                ),
            )
        )
        return tuple(
            item.model_copy(update={"rank": rank})
            for rank, item in enumerate(ordered, start=1)
        )

    def _objective_values(
        self,
        execution: FlexiblePairExecution,
        live_run: LivePackageAgentRun,
        total_cents: int | None,
        completeness: Decimal,
    ) -> tuple[dict[str, Decimal], tuple[str, ...]]:
        package = live_run.package
        if package is None:
            return (
                {
                    "price": Decimal(10**18),
                    "evidence": completeness,
                    "robustness": Decimal(0),
                    "convenience": Decimal(0),
                    "schedule_quality": Decimal(0),
                    "breakfast": Decimal(0),
                    "baggage": Decimal(0),
                },
                (
                    f"departure-week:{execution.date_pair.departure_date.isocalendar().week}",
                    f"nights:{execution.date_pair.night_count}",
                ),
            )
        candidate = package.final_candidate
        extra_stays = max(0, len(candidate.lodgings) - 1)
        convenience = Decimal(1) / Decimal(1 + extra_stays + len(candidate.transfers))
        warning_count = len(package.final_violations)
        robustness = Decimal(1 if live_run.all_platforms_complete else 0) / Decimal(
            1 + warning_count
        )
        total_nights = max(1, sum(item.night_count for item in candidate.lodgings))
        breakfast_nights = sum(
            item.night_count for item in candidate.lodgings if item.breakfast_included is True
        )
        breakfast = Decimal(breakfast_nights) / Decimal(total_nights)
        baggage_kg = candidate.flight.checked_baggage_per_adult_kg
        baggage = Decimal("0.5") if baggage_kg is None else Decimal(1 if baggage_kg > 0 else 0)
        outbound_hour = candidate.flight.outbound_depart_at.hour
        return_hour = candidate.flight.return_depart_at.hour
        schedule_quality = Decimal(
            int(6 <= outbound_hour < 23) + int(6 <= return_hour < 23)
        ) / Decimal(2)
        tags = (
            f"departure-week:{execution.date_pair.departure_date.isocalendar().week}",
            f"nights:{execution.date_pair.night_count}",
            f"package-kind:{candidate.kind.value}",
            f"stay-plan:{live_run.selected_stay_plan_id.value}"
            if live_run.selected_stay_plan_id is not None
            else "stay-plan:none",
        )
        return (
            {
                "price": Decimal(total_cents if total_cents is not None else 10**18),
                "evidence": completeness,
                "robustness": robustness,
                "convenience": convenience,
                "schedule_quality": schedule_quality,
                "breakfast": breakfast,
                "baggage": baggage,
            },
            tags,
        )

    def _recommended_evidence(
        self,
        executions: tuple[FlexiblePairExecution, ...],
        recommended_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        selected = set(recommended_ids)
        return tuple(
            dict.fromkeys(
                evidence_ref
                for execution in executions
                if execution.run is not None
                and execution.run.package is not None
                and (
                    f"{execution.date_pair.id}:{execution.run.selected_stay_plan_id.value}"
                    if execution.run.selected_stay_plan_id is not None
                    else execution.date_pair.id
                )
                in selected
                for evidence_ref in execution.run.package.evidence_refs
            )
        )

    def _claim_boundary(
        self,
        window: FlexibleTravelWindow,
        plan: FlexibleQueryPlan,
        sampled: bool,
        executed_pair_count: int,
        stopped_early: bool,
        stay_area_search_profile: StayAreaSearchProfile | None,
        stay_plan_candidate_set: StayPlanCandidateSet | None,
        date_acquisition_policy: str,
    ) -> str:
        if not sampled:
            qualifier = "全量日期对精确查询"
        elif date_acquisition_policy == RankedTopKDateRefiner.policy_id:
            qualifier = "Query Strategist 排序后的 bounded Top-K 精确查询"
        elif date_acquisition_policy.startswith("adaptive_experimental"):
            qualifier = "显式注入的实验性 adaptive 精确查询"
        else:
            qualifier = f"显式注入策略 {date_acquisition_policy} 的有界精确查询"
        coarse_scope = (
            "粗阶段已枚举完整日期组合"
            if plan.search_metrics.shortlist_pair_count == window.universe_size
            else f"粗阶段保留 {plan.search_metrics.shortlist_pair_count} 个日期候选"
        )
        boundary = (
            f"{coarse_scope}；本轮对 {window.universe_size} 个可行日期组合中的 "
            f"{executed_pair_count} 个日期对执行{qualifier}"
            + ("并由校准下界提前停止；" if stopped_early else "；")
            + "排序只在这些日期对及其当次可见报价内有效，不得声称全月最低价、"
            + "全网最低价、库存锁定或可订承诺。"
        )
        if date_acquisition_policy == RankedTopKDateRefiner.policy_id:
            boundary = (
                f"{boundary}默认 Top-K 是当前冻结 synthetic 基线下的保守 fallback；"
                "Query Strategist 仍可在硬预算内真实重排，且该选择不证明真实 OTA 上"
                "Top-K 优于 adaptive。"
            )
        if stay_area_search_profile is None:
            return boundary
        boundary = f"{boundary}{stay_area_search_profile.assumption_zh}"
        if stay_plan_candidate_set is None:
            return boundary
        return (
            f"{boundary}住宿方案在搜索前冻结为 "
            f"{len(stay_plan_candidate_set.candidates)} 个候选，"
            f"SHA256={stay_plan_candidate_set.candidate_set_sha256}；"
            "Planner 只能在该集合内基于精确地点库存与接驳合同裁决。"
        )

    def _pair_checkpoint(
        self,
        execution: FlexiblePairExecution,
        *,
        sequence: int,
        request_sha256: str,
    ) -> LivePlanningPairCheckpoint:
        pair = execution.date_pair
        query_task_ids = tuple(task.id for task in execution.query_tasks)
        live_run = execution.run
        if live_run is None:
            assert execution.failure_class is not None
            return LivePlanningPairCheckpoint.create(
                sequence=sequence,
                request_sha256=request_sha256,
                date_pair_id=pair.id,
                departure_date=pair.departure_date,
                return_date=pair.return_date,
                state=LivePlanningPairCheckpointState.FAILED,
                query_task_ids=query_task_ids,
                failure_class=execution.failure_class,
                captured_at=self._utc_now(),
            )
        return LivePlanningPairCheckpoint.create(
            sequence=sequence,
            request_sha256=request_sha256,
            date_pair_id=pair.id,
            departure_date=pair.departure_date,
            return_date=pair.return_date,
            state=LivePlanningPairCheckpointState.COMPLETED,
            query_task_ids=query_task_ids,
            run_purpose=live_run.run_purpose.value,
            finalization_state=live_run.finalization_state.value,
            decision_state=live_run.decision.state.value,
            source_task_count=len(live_run.source_task_ids),
            exploration_seal_passed=live_run.exploration_seal_passed,
            all_platforms_complete=live_run.all_platforms_complete,
            captured_at=self._utc_now(),
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("flexible live clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
