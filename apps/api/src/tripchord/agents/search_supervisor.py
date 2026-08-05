from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from tripchord.agents.models import AgentTask, ToolPermission
from tripchord.domain.common import DomainModel


class SearchCacheDisposition(StrEnum):
    RECENT_REUSE_ALLOWED = "recent_reuse_allowed"
    FRESH_READ_REQUIRED = "fresh_read_required"
    PUBLIC_ENDPOINT = "public_endpoint"


class SearchTaskCapability(DomainModel):
    """Deterministic allow-list entry shown to the Search Supervisor.

    ``tenant_authorized`` means the server admitted the task to this tenant's
    read-only search scope.  It is deliberately not a claim that Chrome still
    has a valid login or host grant; the browser companion verifies those facts
    at execution time and may return a typed permission/login challenge.
    """

    task_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    vertical: str = Field(min_length=1)
    required: bool
    tenant_authorized: bool
    permission: ToolPermission
    budget_units: int = Field(default=1, ge=1, le=10)
    cache_disposition: SearchCacheDisposition
    current_start_delay_ms: int = Field(default=0, ge=0, le=900_000)
    capability_version: str = Field(min_length=1)


class SearchScheduleWave(DomainModel):
    id: str = Field(min_length=1, max_length=80)
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=32)


class SearchSupervisorProposal(DomainModel):
    """A model proposal only; it has no authority until deterministic validation."""

    summary: str = Field(min_length=1)
    waves: tuple[SearchScheduleWave, ...] = Field(min_length=1, max_length=32)
    skipped_task_ids: tuple[str, ...] = ()
    declared_budget_units: int = Field(ge=1, le=1_000)
    strategy_reasons: tuple[str, ...] = ()
    uncertainty_flags: tuple[str, ...] = ()


class AppliedSearchSchedule(DomainModel):
    policy_version: str = "search-supervisor-safety-envelope-v2"
    coverage_mode: str = Field(pattern="^(strict|degraded)$")
    proposal_source: str = Field(pattern="^(model_agent|scripted_fallback)$")
    proposal_accepted: bool
    waves: tuple[SearchScheduleWave, ...] = Field(min_length=1)
    ordered_task_ids: tuple[str, ...] = Field(min_length=1)
    skipped_task_ids: tuple[str, ...] = ()
    applied_budget_units: int = Field(ge=1)
    hard_budget_units: int = Field(ge=1)
    max_browser_tasks_per_wave: int = Field(ge=1, le=32)
    browser_companion_lease_cap: int = Field(default=32, ge=1, le=32)
    minimum_browser_lease_batches: int = Field(default=0, ge=0)
    applied_browser_barrier_batches: int = Field(default=0, ge=0)
    rejected_reasons: tuple[str, ...] = ()
    safety_boundary: str = (
        "Search Supervisor 只能重排已授权的只读 Source ID 并在显式 degraded "
        "模式跳过预先声明的可选任务；任务能力、租户边界、预算、最小覆盖、"
        "浏览器租约关键路径、同平台节流顺序、浏览器权限、报价归一化与最终"
        "发布门均由确定性代码掌控。"
    )

    @model_validator(mode="after")
    def validate_reconciliation(self) -> AppliedSearchSchedule:
        flattened = tuple(task_id for wave in self.waves for task_id in wave.task_ids)
        if flattened != self.ordered_task_ids:
            raise ValueError("ordered task ids must exactly match flattened waves")
        if len(set(flattened)) != len(flattened):
            raise ValueError("applied schedule cannot contain duplicate task ids")
        if set(flattened) & set(self.skipped_task_ids):
            raise ValueError("applied and skipped task ids must be disjoint")
        if self.proposal_accepted and self.rejected_reasons:
            raise ValueError("an accepted proposal cannot carry rejection reasons")
        if not self.proposal_accepted and not self.rejected_reasons:
            raise ValueError("a rejected proposal requires recorded reasons")
        if self.applied_budget_units > self.hard_budget_units:
            raise ValueError("applied schedule exceeds its hard budget")
        if self.applied_browser_barrier_batches != self.minimum_browser_lease_batches:
            raise ValueError(
                "applied browser barrier batches must match the minimum safe lease batches"
            )
        return self


class SearchScheduleSafetyError(ValueError):
    """The deterministic source-task envelope itself is unsafe or infeasible."""


def _browser_barrier_batches(
    waves: tuple[SearchScheduleWave, ...],
    by_id: dict[str, SearchTaskCapability],
    *,
    browser_companion_lease_cap: int,
) -> int:
    return sum(
        (
            sum(
                by_id[task_id].vertical != "public-transfer"
                for task_id in wave.task_ids
                if task_id in by_id
            )
            + browser_companion_lease_cap
            - 1
        )
        // browser_companion_lease_cap
        for wave in waves
    )


def _minimum_browser_lease_batches(
    browser_task_count: int,
    *,
    max_browser_tasks_per_wave: int,
    browser_companion_lease_cap: int,
) -> int:
    effective_wave_capacity = min(
        max_browser_tasks_per_wave,
        browser_companion_lease_cap,
    )
    return (browser_task_count + effective_wave_capacity - 1) // effective_wave_capacity


def _provider_delay_regressions(
    task_ids: tuple[str, ...],
    by_id: dict[str, SearchTaskCapability],
) -> tuple[str, ...]:
    previous_by_provider: dict[str, SearchTaskCapability] = {}
    regressions: list[str] = []
    for task_id in task_ids:
        capability = by_id.get(task_id)
        if capability is None or capability.vertical == "public-transfer":
            continue
        previous = previous_by_provider.get(capability.provider)
        if (
            previous is not None
            and capability.current_start_delay_ms < previous.current_start_delay_ms
        ):
            regressions.append(
                "provider_delay_order_regression:"
                f"{capability.provider}:"
                f"{previous.task_id}@{previous.current_start_delay_ms}>"
                f"{capability.task_id}@{capability.current_start_delay_ms}"
            )
        previous_by_provider[capability.provider] = capability
    return tuple(regressions)


def apply_search_supervisor_proposal(
    capabilities: tuple[SearchTaskCapability, ...],
    proposal: SearchSupervisorProposal | None,
    *,
    coverage_mode: str,
    hard_budget_units: int,
    max_browser_tasks_per_wave: int,
    browser_companion_lease_cap: int | None = None,
) -> AppliedSearchSchedule:
    """Validate a model schedule or produce an explicit deterministic fallback.

    Invalid model output never partially applies.  A fallback is allowed only
    when the complete deterministic allow-list itself fits the hard budget;
    otherwise there is no safe schedule and the call fails closed.
    """

    if coverage_mode not in {"strict", "degraded"}:
        raise ValueError("coverage_mode must be strict or degraded")
    if hard_budget_units < 1:
        raise ValueError("hard_budget_units must be positive")
    if max_browser_tasks_per_wave < 1 or max_browser_tasks_per_wave > 32:
        raise ValueError("max_browser_tasks_per_wave must be between 1 and 32")
    lease_cap = (
        max_browser_tasks_per_wave
        if browser_companion_lease_cap is None
        else browser_companion_lease_cap
    )
    if lease_cap < 1 or lease_cap > 32:
        raise ValueError("browser_companion_lease_cap must be between 1 and 32")
    if not capabilities:
        raise SearchScheduleSafetyError("source allow-list is empty")
    ids = tuple(item.task_id for item in capabilities)
    if len(ids) != len(set(ids)):
        raise SearchScheduleSafetyError("source allow-list contains duplicate task ids")
    unsafe = tuple(
        item.task_id
        for item in capabilities
        if not item.tenant_authorized or item.permission != ToolPermission.READ_ONLY_EXTERNAL
    )
    if unsafe:
        raise SearchScheduleSafetyError(
            f"source allow-list contains unauthorized or non-read-only tasks: {list(unsafe)}"
        )

    by_id = {item.task_id: item for item in capabilities}
    fallback_delay_regressions = _provider_delay_regressions(ids, by_id)
    if fallback_delay_regressions:
        raise SearchScheduleSafetyError(
            "deterministic fallback contains provider delay regressions: "
            f"{list(fallback_delay_regressions)}"
        )
    reasons: list[str] = []
    if proposal is None:
        reasons.append("model_proposal_unavailable")
    else:
        flattened = tuple(task_id for wave in proposal.waves for task_id in wave.task_ids)
        skipped = proposal.skipped_task_ids
        wave_ids = tuple(wave.id for wave in proposal.waves)
        if len(wave_ids) != len(set(wave_ids)):
            reasons.append("duplicate_wave_ids")
        unknown = (set(flattened) | set(skipped)) - set(by_id)
        if unknown:
            reasons.append(f"unknown_task_ids:{sorted(unknown)}")
        if len(flattened) != len(set(flattened)):
            reasons.append("duplicate_scheduled_task_ids")
        if len(skipped) != len(set(skipped)):
            reasons.append("duplicate_skipped_task_ids")
        if set(flattened) & set(skipped):
            reasons.append("scheduled_and_skipped_overlap")
        declared = set(flattened) | set(skipped)
        omitted = set(by_id) - declared
        if omitted:
            reasons.append(f"tasks_not_explicitly_disposed:{sorted(omitted)}")
        missing_required = {item.task_id for item in capabilities if item.required} - set(flattened)
        if missing_required:
            reasons.append(f"required_tasks_missing:{sorted(missing_required)}")
        if coverage_mode == "strict" and skipped:
            reasons.append("strict_mode_cannot_skip_tasks")
        if any(
            sum(by_id[task_id].budget_units for task_id in wave.task_ids if task_id in by_id) < 1
            for wave in proposal.waves
        ):
            reasons.append("empty_or_unknown_only_wave")
        for wave in proposal.waves:
            browser_count = sum(
                by_id[task_id].vertical != "public-transfer"
                for task_id in wave.task_ids
                if task_id in by_id
            )
            if browser_count > max_browser_tasks_per_wave:
                reasons.append(f"wave_browser_concurrency_exceeded:{wave.id}:{browser_count}")
        scheduled_browser_count = sum(
            by_id[task_id].vertical != "public-transfer"
            for task_id in flattened
            if task_id in by_id
        )
        minimum_browser_batches = _minimum_browser_lease_batches(
            scheduled_browser_count,
            max_browser_tasks_per_wave=max_browser_tasks_per_wave,
            browser_companion_lease_cap=lease_cap,
        )
        proposed_browser_batches = _browser_barrier_batches(
            proposal.waves,
            by_id,
            browser_companion_lease_cap=lease_cap,
        )
        if proposed_browser_batches > minimum_browser_batches:
            reasons.append(
                "browser_barrier_batches_exceeded:"
                f"{proposed_browser_batches}>{minimum_browser_batches}"
            )
        reasons.extend(_provider_delay_regressions(flattened, by_id))
        applied_cost = sum(by_id[task_id].budget_units for task_id in flattened if task_id in by_id)
        if applied_cost > hard_budget_units:
            reasons.append(f"hard_budget_exceeded:{applied_cost}>{hard_budget_units}")
        if proposal.declared_budget_units != applied_cost:
            reasons.append(
                f"declared_budget_mismatch:{proposal.declared_budget_units}!={applied_cost}"
            )
        if not reasons:
            return AppliedSearchSchedule(
                coverage_mode=coverage_mode,
                proposal_source="model_agent",
                proposal_accepted=True,
                waves=proposal.waves,
                ordered_task_ids=flattened,
                skipped_task_ids=skipped,
                applied_budget_units=applied_cost,
                hard_budget_units=hard_budget_units,
                max_browser_tasks_per_wave=max_browser_tasks_per_wave,
                browser_companion_lease_cap=lease_cap,
                minimum_browser_lease_batches=minimum_browser_batches,
                applied_browser_barrier_batches=proposed_browser_batches,
            )

    fallback_cost = sum(item.budget_units for item in capabilities)
    if fallback_cost > hard_budget_units:
        raise SearchScheduleSafetyError(
            "deterministic fallback cannot fit the hard search budget: "
            f"{fallback_cost}>{hard_budget_units}"
        )
    grouped: list[tuple[str, ...]] = []
    current: list[str] = []
    current_browser_count = 0
    for capability in capabilities:
        is_browser = capability.vertical != "public-transfer"
        if is_browser and current_browser_count == max_browser_tasks_per_wave:
            grouped.append(tuple(current))
            current = []
            current_browser_count = 0
        current.append(capability.task_id)
        current_browser_count += int(is_browser)
    if current:
        grouped.append(tuple(current))
    fallback_waves = tuple(
        SearchScheduleWave(id=f"fallback-wave-{index}", task_ids=task_ids)
        for index, task_ids in enumerate(grouped, start=1)
    )
    fallback_browser_task_count = sum(item.vertical != "public-transfer" for item in capabilities)
    fallback_minimum_browser_batches = _minimum_browser_lease_batches(
        fallback_browser_task_count,
        max_browser_tasks_per_wave=max_browser_tasks_per_wave,
        browser_companion_lease_cap=lease_cap,
    )
    fallback_browser_batches = _browser_barrier_batches(
        fallback_waves,
        by_id,
        browser_companion_lease_cap=lease_cap,
    )
    if fallback_browser_batches != fallback_minimum_browser_batches:
        raise SearchScheduleSafetyError(
            "deterministic fallback exceeds the minimum browser lease critical path: "
            f"{fallback_browser_batches}>{fallback_minimum_browser_batches}"
        )
    return AppliedSearchSchedule(
        coverage_mode=coverage_mode,
        proposal_source="scripted_fallback",
        proposal_accepted=False,
        waves=fallback_waves,
        ordered_task_ids=ids,
        applied_budget_units=fallback_cost,
        hard_budget_units=hard_budget_units,
        max_browser_tasks_per_wave=max_browser_tasks_per_wave,
        browser_companion_lease_cap=lease_cap,
        minimum_browser_lease_batches=fallback_minimum_browser_batches,
        applied_browser_barrier_batches=fallback_browser_batches,
        rejected_reasons=tuple(dict.fromkeys(reasons)),
    )


def materialize_search_schedule(
    tasks: tuple[AgentTask, ...],
    schedule: AppliedSearchSchedule,
    *,
    supervisor_task_id: str,
) -> tuple[AgentTask, ...]:
    """Turn an accepted/fallback schedule into real DAG waves.

    Skipped tasks remain auditable graph nodes but carry an explicit marker and
    never call an external tool.  This preserves expected Source IDs while
    making degraded omissions visible to coverage and Done-Gate checks.
    """

    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise SearchScheduleSafetyError("source tasks contain duplicate ids")
    referenced = set(schedule.ordered_task_ids) | set(schedule.skipped_task_ids)
    if referenced != set(by_id):
        raise SearchScheduleSafetyError("schedule does not reconcile with source tasks")
    materialized: list[AgentTask] = []
    previous_wave_ids: tuple[str, ...] = (supervisor_task_id,)
    global_index = 0
    for wave in schedule.waves:
        current_ids = wave.task_ids
        for task_id in current_ids:
            task = by_id[task_id]
            dependencies = tuple(dict.fromkeys((*task.dependencies, *previous_wave_ids)))
            materialized.append(
                task.model_copy(
                    update={
                        "dependencies": dependencies,
                        "priority": max(-100, 100 - global_index),
                        "input": {
                            **task.input,
                            "search_schedule_wave_id": wave.id,
                            "search_schedule_index": global_index,
                        },
                    }
                )
            )
            global_index += 1
        previous_wave_ids = current_ids
    for task_id in schedule.skipped_task_ids:
        task = by_id[task_id]
        materialized.append(
            task.model_copy(
                update={
                    "dependencies": tuple(dict.fromkeys((*task.dependencies, *previous_wave_ids))),
                    "priority": -100,
                    "input": {
                        **task.input,
                        "search_supervisor_skipped": True,
                        "search_supervisor_skip_reason": (
                            "explicit degraded-mode optional-task omission"
                        ),
                    },
                }
            )
        )
    return tuple(materialized)
