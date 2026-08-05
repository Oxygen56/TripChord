from __future__ import annotations

import asyncio
import hashlib
import json
from enum import StrEnum
from math import isqrt
from typing import NamedTuple, Self

from pydantic import AliasChoices, Field, field_validator, model_validator

from tripchord.domain.common import DomainModel

DATE_ROWS_PER_AGENT = 12
CANDIDATES_PER_AGENT = 32
MERGER_INPUTS_PER_AGENT = 12
DIRECT_DATE_PAIR_LIMIT = 8
COARSE_DATE_PAIR_LIMIT = 400
CANDIDATE_LIMIT = 2_000
EVIDENCE_GAP_LIMIT = 32
LOGICAL_AGENT_HARD_CAP = 96

BROWSER_CONCURRENCY = 6
QUNAR_LODGING_CONCURRENCY = 1
DATE_PAIR_EXECUTION_CONCURRENCY = 1
ICOM_CONCURRENCY_PER_PAIR = 4

MODEL_CONCURRENCY_LEVELS = (2, 6, 8, 12)
HEALTH_ADJUSTED_CONCURRENCY_LEVELS = (1, 2, 4, 6, 8, 12)


class ProviderHealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"

    @property
    def half_units(self) -> int:
        return {
            ProviderHealthStatus.UNKNOWN: 1,
            ProviderHealthStatus.HEALTHY: 2,
            ProviderHealthStatus.DEGRADED: 1,
            ProviderHealthStatus.BLOCKED: 0,
        }[self]


class ProviderHealth(DomainModel):
    provider: str = Field(min_length=1, max_length=80)
    vertical: str = Field(min_length=1, max_length=40)
    required: bool = True
    status: ProviderHealthStatus

    @field_validator("provider", "vertical")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("provider health identifiers cannot be blank")
        return normalized


class AdaptiveControlInput(DomainModel):
    date_pair_count: int = Field(
        ge=0,
        le=COARSE_DATE_PAIR_LIMIT,
        validation_alias=AliasChoices("date_pair_count", "D"),
    )
    candidate_count: int = Field(
        ge=0,
        le=CANDIDATE_LIMIT,
        validation_alias=AliasChoices("candidate_count", "C"),
    )
    evidence_gap_count: int = Field(
        ge=0,
        le=EVIDENCE_GAP_LIMIT,
        validation_alias=AliasChoices("evidence_gap_count", "G"),
    )
    repair_required: bool = Field(
        validation_alias=AliasChoices("repair_required", "R"),
    )
    event_active: bool = Field(
        validation_alias=AliasChoices("event_active", "E"),
    )
    exploration_pair_count: int = Field(default=1, ge=0, le=8)
    publication_pair_count: int = Field(default=0, ge=0, le=8)
    direct_final_pair_count: int = Field(default=0, ge=0, le=8)
    provider_health: tuple[ProviderHealth, ...] = ()
    model_endpoint_health: tuple[ProviderHealth, ...] = ()
    strict_mode: bool = True

    @model_validator(mode="after")
    def validate_provider_health_uniqueness(self) -> Self:
        for label, providers in (
            ("provider", self.provider_health),
            ("model endpoint", self.model_endpoint_health),
        ):
            keys = tuple((item.provider, item.vertical) for item in providers)
            if len(keys) != len(set(keys)):
                raise ValueError(f"{label} health entries must be unique by provider and vertical")
        if self.publication_pair_count and self.direct_final_pair_count:
            raise ValueError(
                "publication refresh pairs and direct-final pairs are mutually exclusive"
            )
        if self.exploration_pair_count and self.direct_final_pair_count:
            raise ValueError("exploration pairs and direct-final pairs are mutually exclusive")
        if self.publication_pair_count > self.exploration_pair_count:
            raise ValueError("publication refresh pairs cannot exceed explored date pairs")
        return self

    @property
    def D(self) -> int:
        return self.date_pair_count

    @property
    def C(self) -> int:
        return self.candidate_count

    @property
    def G(self) -> int:
        return self.evidence_gap_count

    @property
    def R(self) -> bool:
        return self.repair_required

    @property
    def E(self) -> bool:
        return self.event_active


class AdaptiveStopReason(StrEnum):
    NO_REMAINING_WORK = "no_remaining_work"
    STRICT_PROVIDER_COVERAGE_UNREACHABLE = "strict_provider_coverage_unreachable"
    NO_SEARCH_PROVIDER_AVAILABLE = "no_search_provider_available"
    LOGICAL_CAP_SATURATED_SPLIT_REQUIRED = "logical_cap_saturated_split_required"
    BACKGROUND_BATCH_REQUIRED = "background_batch_required"


class _BudgetValues(NamedTuple):
    date_shards: int
    date_mergers: int
    candidate_shards: int
    background_batches: int
    raw_logical_agents: int
    logical_agent_cap: int
    logical_saturated: bool
    raw_model_concurrency: int
    desired_model_concurrency: int
    health_adjusted_model_concurrency: int
    theoretical_browser_task_count: int
    theoretical_icom_task_count: int
    stop_reason: AdaptiveStopReason | None
    diagnostic_reasons: tuple[str, ...]


class ScaleDirective(DomainModel):
    policy_version: str = "adaptive-control-v1"
    control_input: AdaptiveControlInput
    state_fingerprint: str = Field(pattern="^[0-9a-f]{64}$")

    date_shards: int = Field(ge=0)
    date_mergers: int = Field(ge=0)
    candidate_shards: int = Field(ge=0)
    background_batches: int = Field(ge=0)

    raw_logical_agents: int = Field(ge=0)
    logical_agent_cap: int = Field(ge=0, le=LOGICAL_AGENT_HARD_CAP)
    logical_saturated: bool

    raw_model_concurrency: int = Field(ge=1)
    desired_model_concurrency: int
    health_adjusted_model_concurrency: int

    browser_concurrency: int = Field(default=BROWSER_CONCURRENCY, ge=6, le=6)
    qunar_lodging_concurrency: int = Field(
        default=QUNAR_LODGING_CONCURRENCY,
        ge=1,
        le=1,
    )
    date_pair_execution_concurrency: int = Field(
        default=DATE_PAIR_EXECUTION_CONCURRENCY,
        ge=1,
        le=1,
    )
    icom_concurrency_per_pair: int = Field(
        default=ICOM_CONCURRENCY_PER_PAIR,
        ge=4,
        le=4,
    )

    theoretical_browser_task_count: int = Field(ge=0)
    theoretical_icom_task_count: int = Field(ge=0)
    stop_reason: AdaptiveStopReason | None = None
    diagnostic_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_deterministic_derivation(self) -> Self:
        expected = _derive_values(self.control_input)
        fingerprint = adaptive_state_fingerprint(self.control_input)
        comparisons = {
            "state_fingerprint": (self.state_fingerprint, fingerprint),
            "date_shards": (self.date_shards, expected.date_shards),
            "date_mergers": (self.date_mergers, expected.date_mergers),
            "candidate_shards": (self.candidate_shards, expected.candidate_shards),
            "background_batches": (self.background_batches, expected.background_batches),
            "raw_logical_agents": (self.raw_logical_agents, expected.raw_logical_agents),
            "logical_agent_cap": (self.logical_agent_cap, expected.logical_agent_cap),
            "logical_saturated": (self.logical_saturated, expected.logical_saturated),
            "raw_model_concurrency": (
                self.raw_model_concurrency,
                expected.raw_model_concurrency,
            ),
            "desired_model_concurrency": (
                self.desired_model_concurrency,
                expected.desired_model_concurrency,
            ),
            "health_adjusted_model_concurrency": (
                self.health_adjusted_model_concurrency,
                expected.health_adjusted_model_concurrency,
            ),
            "theoretical_browser_task_count": (
                self.theoretical_browser_task_count,
                expected.theoretical_browser_task_count,
            ),
            "theoretical_icom_task_count": (
                self.theoretical_icom_task_count,
                expected.theoretical_icom_task_count,
            ),
            "stop_reason": (self.stop_reason, expected.stop_reason),
            "diagnostic_reasons": (
                self.diagnostic_reasons,
                expected.diagnostic_reasons,
            ),
        }
        mismatched = tuple(
            name for name, (actual, wanted) in comparisons.items() if actual != wanted
        )
        if mismatched:
            raise ValueError(
                f"scale directive conflicts with deterministic derivation: {list(mismatched)}"
            )
        if self.desired_model_concurrency not in MODEL_CONCURRENCY_LEVELS:
            raise ValueError("desired model concurrency is outside the policy levels")
        if self.health_adjusted_model_concurrency not in HEALTH_ADJUSTED_CONCURRENCY_LEVELS:
            raise ValueError("health-adjusted concurrency is outside the policy levels")
        if self.health_adjusted_model_concurrency > self.desired_model_concurrency:
            raise ValueError("provider health cannot increase model concurrency")
        return self


class AdaptiveConcurrencyAudit(DomainModel):
    """Typed runtime evidence for the bounded additive-increase/halve policy."""

    ceiling: int = Field(ge=1, le=MODEL_CONCURRENCY_LEVELS[-1])
    initial_limit: int = Field(ge=1, le=MODEL_CONCURRENCY_LEVELS[-1])
    final_limit: int = Field(ge=1, le=MODEL_CONCURRENCY_LEVELS[-1])
    peak_in_flight: int = Field(ge=0, le=MODEL_CONCURRENCY_LEVELS[-1])
    admitted_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    additive_increase_count: int = Field(ge=0)
    multiplicative_decrease_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_runtime_accounting(self) -> Self:
        if self.initial_limit > self.ceiling or self.final_limit > self.ceiling:
            raise ValueError("runtime concurrency limit cannot exceed its frozen ceiling")
        if self.peak_in_flight > self.ceiling:
            raise ValueError("runtime in-flight count cannot exceed its frozen ceiling")
        if self.success_count + self.failure_count != self.admitted_count:
            raise ValueError("runtime concurrency outcomes do not reconcile")
        return self


class AdaptiveModelConcurrencyGate:
    """A bounded model-only gate that starts small and reacts to observed outcomes.

    The deterministic ``ScaleDirective`` remains the hard ceiling.  Successful
    completions add one permit after a small evidence window; a failed model
    stage halves the live limit.  This gate never changes browser, provider, or
    date-pair execution concurrency.
    """

    def __init__(
        self,
        ceiling: int,
        *,
        initial_limit: int = 2,
        success_window: int = 2,
    ) -> None:
        if ceiling < 1 or ceiling > MODEL_CONCURRENCY_LEVELS[-1]:
            raise ValueError("model concurrency ceiling must be between 1 and 12")
        if initial_limit < 1:
            raise ValueError("initial model concurrency must be positive")
        if success_window < 1:
            raise ValueError("success window must be positive")
        self._ceiling = ceiling
        self._initial_limit = min(initial_limit, ceiling)
        self._limit = self._initial_limit
        self._success_window = success_window
        self._condition = asyncio.Condition()
        self._in_flight = 0
        self._peak_in_flight = 0
        self._success_streak = 0
        self._admitted_count = 0
        self._success_count = 0
        self._failure_count = 0
        self._increase_count = 0
        self._decrease_count = 0

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._limit)
            self._in_flight += 1
            self._admitted_count += 1
            self._peak_in_flight = max(self._peak_in_flight, self._in_flight)

    async def release(self, *, successful: bool) -> None:
        async with self._condition:
            if self._in_flight < 1:
                raise RuntimeError("model concurrency release without an active lease")
            self._in_flight -= 1
            if successful:
                self._success_count += 1
                self._success_streak += 1
                if self._success_streak >= self._success_window and self._limit < self._ceiling:
                    self._limit += 1
                    self._success_streak = 0
                    self._increase_count += 1
            else:
                self._failure_count += 1
                self._success_streak = 0
                reduced = max(1, self._limit // 2)
                if reduced < self._limit:
                    self._limit = reduced
                    self._decrease_count += 1
            self._condition.notify_all()

    def audit(self) -> AdaptiveConcurrencyAudit:
        if self._in_flight:
            raise RuntimeError("cannot finalize concurrency audit with active leases")
        return AdaptiveConcurrencyAudit(
            ceiling=self._ceiling,
            initial_limit=self._initial_limit,
            final_limit=self._limit,
            peak_in_flight=self._peak_in_flight,
            admitted_count=self._admitted_count,
            success_count=self._success_count,
            failure_count=self._failure_count,
            additive_increase_count=self._increase_count,
            multiplicative_decrease_count=self._decrease_count,
        )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator if numerator else 0


def _ceil_sqrt(value: int) -> int:
    root = isqrt(value)
    return root if root * root == value else root + 1


def _quantize_up(value: int) -> int:
    return next(
        (level for level in MODEL_CONCURRENCY_LEVELS if level >= value),
        MODEL_CONCURRENCY_LEVELS[-1],
    )


def _quantize_health_down(value: int) -> int:
    bounded = max(1, value)
    return max(level for level in HEALTH_ADJUSTED_CONCURRENCY_LEVELS if level <= bounded)


def _health_adjusted_concurrency(
    desired: int,
    providers: tuple[ProviderHealth, ...],
) -> int:
    required = tuple(item for item in providers if item.required)
    relevant = required or providers
    if not relevant:
        return desired
    numerator = sum(item.status.half_units for item in relevant)
    denominator = 2 * len(relevant)
    adjusted = desired * numerator // denominator
    return _quantize_health_down(adjusted)


def _diagnostic_reasons(
    control: AdaptiveControlInput,
    *,
    logical_saturated: bool,
) -> tuple[AdaptiveStopReason, ...]:
    reasons: list[AdaptiveStopReason] = []
    if control.D == 0 and control.C == 0 and control.G == 0 and not control.R and not control.E:
        reasons.append(AdaptiveStopReason.NO_REMAINING_WORK)

    required_lodging = tuple(
        item for item in control.provider_health if item.required and item.vertical == "lodging"
    )
    usable_required_lodging = sum(
        item.status != ProviderHealthStatus.BLOCKED for item in required_lodging
    )
    if control.strict_mode and usable_required_lodging < 2:
        reasons.append(AdaptiveStopReason.STRICT_PROVIDER_COVERAGE_UNREACHABLE)

    if control.provider_health and all(
        item.status == ProviderHealthStatus.BLOCKED for item in control.provider_health
    ):
        reasons.append(AdaptiveStopReason.NO_SEARCH_PROVIDER_AVAILABLE)
    if logical_saturated:
        reasons.append(AdaptiveStopReason.LOGICAL_CAP_SATURATED_SPLIT_REQUIRED)
    if control.D > DIRECT_DATE_PAIR_LIMIT:
        reasons.append(AdaptiveStopReason.BACKGROUND_BATCH_REQUIRED)
    return tuple(dict.fromkeys(reasons))


def _derive_values(control: AdaptiveControlInput) -> _BudgetValues:
    date_shards = _ceil_div(control.D, DATE_ROWS_PER_AGENT)
    date_mergers = (
        0
        if date_shards <= 1
        else 1
        + (
            _ceil_div(date_shards, MERGER_INPUTS_PER_AGENT)
            if date_shards > MERGER_INPUTS_PER_AGENT
            else 0
        )
    )
    candidate_shards = _ceil_div(control.C, CANDIDATES_PER_AGENT)
    background_batches = _ceil_div(control.D, DIRECT_DATE_PAIR_LIMIT)

    query_agents = 0 if date_shards == 0 else max(1, date_shards + date_mergers)
    pipeline_core_agents = (
        7 * control.exploration_pair_count
        + 8 * control.publication_pair_count
        + 9 * control.direct_final_pair_count
    )
    candidate_extra = candidate_shards if candidate_shards > 1 else 0
    raw_logical = (
        query_agents
        + pipeline_core_agents
        + candidate_extra
        + control.G
        + 2 * int(control.R)
        + int(control.E)
    )
    logical_cap = min(raw_logical, LOGICAL_AGENT_HARD_CAP)
    logical_saturated = raw_logical > LOGICAL_AGENT_HARD_CAP

    parallel_shard_work = max(
        1,
        max(0, date_shards - 1) + max(0, candidate_shards - 1),
    )
    raw_model_concurrency = 1 + _ceil_sqrt(parallel_shard_work)
    desired_model_concurrency = _quantize_up(raw_model_concurrency)
    health_adjusted = _health_adjusted_concurrency(
        desired_model_concurrency,
        control.model_endpoint_health,
    )
    reasons = _diagnostic_reasons(control, logical_saturated=logical_saturated)
    return _BudgetValues(
        date_shards=date_shards,
        date_mergers=date_mergers,
        candidate_shards=candidate_shards,
        background_batches=background_batches,
        raw_logical_agents=raw_logical,
        logical_agent_cap=logical_cap,
        logical_saturated=logical_saturated,
        raw_model_concurrency=raw_model_concurrency,
        desired_model_concurrency=desired_model_concurrency,
        health_adjusted_model_concurrency=health_adjusted,
        theoretical_browser_task_count=13 * control.D,
        theoretical_icom_task_count=4 * control.D,
        stop_reason=reasons[0] if reasons else None,
        diagnostic_reasons=tuple(item.value for item in reasons),
    )


def adaptive_state_fingerprint(control: AdaptiveControlInput) -> str:
    providers = sorted(
        (
            {
                "provider": item.provider,
                "vertical": item.vertical,
                "required": item.required,
                "status": item.status.value,
            }
            for item in control.provider_health
        ),
        key=lambda item: (str(item["provider"]), str(item["vertical"])),
    )
    payload = {
        "policy_version": "adaptive-control-v1",
        "D": control.D,
        "C": control.C,
        "G": control.G,
        "R": control.R,
        "E": control.E,
        "exploration_pair_count": control.exploration_pair_count,
        "publication_pair_count": control.publication_pair_count,
        "direct_final_pair_count": control.direct_final_pair_count,
        "provider_health": providers,
        "model_endpoint_health": sorted(
            (
                {
                    "provider": item.provider,
                    "vertical": item.vertical,
                    "required": item.required,
                    "status": item.status.value,
                }
                for item in control.model_endpoint_health
            ),
            key=lambda item: (str(item["provider"]), str(item["vertical"])),
        ),
        "strict_mode": control.strict_mode,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def derive_scale_directive(control: AdaptiveControlInput) -> ScaleDirective:
    values = _derive_values(control)
    return ScaleDirective(
        control_input=control,
        state_fingerprint=adaptive_state_fingerprint(control),
        date_shards=values.date_shards,
        date_mergers=values.date_mergers,
        candidate_shards=values.candidate_shards,
        background_batches=values.background_batches,
        raw_logical_agents=values.raw_logical_agents,
        logical_agent_cap=values.logical_agent_cap,
        logical_saturated=values.logical_saturated,
        raw_model_concurrency=values.raw_model_concurrency,
        desired_model_concurrency=values.desired_model_concurrency,
        health_adjusted_model_concurrency=values.health_adjusted_model_concurrency,
        theoretical_browser_task_count=values.theoretical_browser_task_count,
        theoretical_icom_task_count=values.theoretical_icom_task_count,
        stop_reason=values.stop_reason,
        diagnostic_reasons=values.diagnostic_reasons,
    )


compute_adaptive_control = derive_scale_directive
build_scale_directive = derive_scale_directive
