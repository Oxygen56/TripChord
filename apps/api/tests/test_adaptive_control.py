from __future__ import annotations

import pytest
from pydantic import ValidationError
from tripchord.agents.adaptive_control import (
    AdaptiveControlInput,
    AdaptiveModelConcurrencyGate,
    AdaptiveStopReason,
    ProviderHealth,
    ProviderHealthStatus,
    ScaleDirective,
    adaptive_state_fingerprint,
    derive_scale_directive,
)


def _healthy_providers() -> tuple[ProviderHealth, ...]:
    return (
        ProviderHealth(
            provider="ctrip",
            vertical="lodging",
            required=True,
            status=ProviderHealthStatus.HEALTHY,
        ),
        ProviderHealth(
            provider="qunar",
            vertical="lodging",
            required=True,
            status=ProviderHealthStatus.HEALTHY,
        ),
        ProviderHealth(
            provider="tongcheng",
            vertical="flight",
            required=False,
            status=ProviderHealthStatus.HEALTHY,
        ),
    )


def _control(
    *,
    D: int,
    C: int,
    G: int = 0,
    R: bool = False,
    E: bool = False,
    provider_health: tuple[ProviderHealth, ...] | None = None,
    model_endpoint_health: tuple[ProviderHealth, ...] = (),
    strict_mode: bool = True,
) -> AdaptiveControlInput:
    return AdaptiveControlInput(
        D=D,
        C=C,
        G=G,
        R=R,
        E=E,
        provider_health=provider_health or _healthy_providers(),
        model_endpoint_health=model_endpoint_health,
        strict_mode=strict_mode,
    )


@pytest.mark.parametrize(
    ("D", "C", "expected_dates", "expected_candidates", "expected_batches"),
    (
        (0, 0, 0, 0, 0),
        (1, 1, 1, 1, 1),
        (8, 32, 1, 1, 1),
        (12, 33, 1, 2, 2),
        (13, 256, 2, 8, 2),
        (24, 1_000, 2, 32, 3),
        (25, 2_000, 3, 63, 4),
        (124, 2_000, 11, 63, 16),
        (400, 2_000, 34, 63, 50),
    ),
)
def test_shard_and_batch_boundaries(
    D: int,
    C: int,
    expected_dates: int,
    expected_candidates: int,
    expected_batches: int,
) -> None:
    directive = derive_scale_directive(_control(D=D, C=C))
    assert directive.date_shards == expected_dates
    assert directive.candidate_shards == expected_candidates
    assert directive.background_batches == expected_batches
    assert directive.theoretical_browser_task_count == 13 * D
    assert directive.theoretical_icom_task_count == 4 * D


@pytest.mark.parametrize(
    ("D", "C", "G", "R", "E", "logical", "concurrency"),
    (
        (1, 32, 0, False, False, 8, 2),
        (1, 32, 0, True, False, 10, 2),
        (8, 256, 4, False, False, 20, 6),
        (8, 256, 8, False, False, 24, 6),
        (32, 1_000, 9, True, False, 54, 8),
        (32, 1_000, 17, True, False, 62, 8),
        (124, 2_000, 0, True, True, 85, 12),
        (124, 2_000, 11, True, True, 96, 12),
    ),
)
def test_confirmed_scale_examples(
    D: int,
    C: int,
    G: int,
    R: bool,
    E: bool,
    logical: int,
    concurrency: int,
) -> None:
    directive = derive_scale_directive(_control(D=D, C=C, G=G, R=R, E=E))
    assert directive.raw_logical_agents == logical
    assert directive.logical_agent_cap == logical
    assert directive.desired_model_concurrency == concurrency
    assert directive.health_adjusted_model_concurrency == concurrency


def test_logical_cap_saturates_and_requires_a_recoverable_split() -> None:
    directive = derive_scale_directive(_control(D=124, C=2_000, G=12, R=True, E=True))
    assert directive.raw_logical_agents == 97
    assert directive.logical_agent_cap == 96
    assert directive.logical_saturated is True
    assert (
        AdaptiveStopReason.LOGICAL_CAP_SATURATED_SPLIT_REQUIRED.value
        in directive.diagnostic_reasons
    )


def test_publication_attempt_budget_accepts_the_full_exact_pair_range() -> None:
    directive = derive_scale_directive(
        AdaptiveControlInput(
            D=1,
            C=0,
            G=0,
            R=False,
            E=False,
            exploration_pair_count=8,
            publication_pair_count=8,
            provider_health=_healthy_providers(),
        )
    )

    assert directive.control_input.publication_pair_count == 8
    assert directive.raw_logical_agents == 121
    assert directive.logical_saturated
    with pytest.raises(ValidationError):
        AdaptiveControlInput(
            D=1,
            C=0,
            G=0,
            R=False,
            E=False,
            exploration_pair_count=8,
            publication_pair_count=9,
        )


def test_strict_mode_fails_closed_when_two_lodging_sources_are_unreachable() -> None:
    providers = (
        ProviderHealth(
            provider="ctrip",
            vertical="lodging",
            status=ProviderHealthStatus.HEALTHY,
        ),
        ProviderHealth(
            provider="qunar",
            vertical="lodging",
            status=ProviderHealthStatus.BLOCKED,
        ),
    )
    directive = derive_scale_directive(_control(D=8, C=256, provider_health=providers))
    assert directive.stop_reason == AdaptiveStopReason.STRICT_PROVIDER_COVERAGE_UNREACHABLE
    assert directive.health_adjusted_model_concurrency == directive.desired_model_concurrency


def test_unknown_quote_source_health_neither_claims_success_nor_throttles_model() -> None:
    providers = tuple(
        ProviderHealth(
            provider=name,
            vertical="lodging",
            required=True,
            status=ProviderHealthStatus.UNKNOWN,
        )
        for name in ("ctrip", "qunar")
    )
    directive = derive_scale_directive(_control(D=24, C=256, provider_health=providers))

    assert directive.stop_reason == AdaptiveStopReason.BACKGROUND_BATCH_REQUIRED
    assert directive.health_adjusted_model_concurrency == directive.desired_model_concurrency
    assert all(item.status == ProviderHealthStatus.UNKNOWN for item in providers)


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (ProviderHealthStatus.HEALTHY, 12),
        (ProviderHealthStatus.DEGRADED, 6),
        (ProviderHealthStatus.BLOCKED, 1),
    ),
)
def test_model_endpoint_health_never_increases_concurrency(
    status: ProviderHealthStatus,
    expected: int,
) -> None:
    providers = tuple(
        ProviderHealth(provider=name, vertical="lodging", status=status)
        for name in ("ctrip", "qunar")
    )
    directive = derive_scale_directive(
        _control(
            D=124,
            C=2_000,
            R=True,
            E=True,
            model_endpoint_health=providers,
        )
    )
    assert directive.health_adjusted_model_concurrency == expected
    assert directive.health_adjusted_model_concurrency <= directive.desired_model_concurrency


def test_state_fingerprint_is_order_independent_and_state_sensitive() -> None:
    providers = _healthy_providers()
    first = _control(D=8, C=256, provider_health=providers)
    reordered = _control(D=8, C=256, provider_health=tuple(reversed(providers)))
    changed = _control(D=8, C=257, provider_health=providers)
    assert adaptive_state_fingerprint(first) == adaptive_state_fingerprint(reordered)
    assert adaptive_state_fingerprint(first) != adaptive_state_fingerprint(changed)


def test_scale_directive_rejects_forged_derived_fields() -> None:
    directive = derive_scale_directive(_control(D=8, C=256))
    payload = directive.model_dump(mode="json")
    payload["logical_agent_cap"] = 95
    with pytest.raises(ValidationError, match="deterministic derivation"):
        ScaleDirective.model_validate(payload)


def test_input_is_frozen_and_rejects_duplicate_or_out_of_range_state() -> None:
    provider = _healthy_providers()[0]
    with pytest.raises(ValidationError, match="unique"):
        _control(D=1, C=1, provider_health=(provider, provider))
    with pytest.raises(ValidationError):
        _control(D=401, C=1)
    control = _control(D=1, C=1)
    with pytest.raises(ValidationError):
        control.date_pair_count = 2  # type: ignore[misc]


def test_repair_and_event_have_exact_logical_deltas_without_changing_shards() -> None:
    baseline = derive_scale_directive(_control(D=8, C=256))
    repair = derive_scale_directive(_control(D=8, C=256, R=True))
    event = derive_scale_directive(_control(D=8, C=256, E=True))
    assert repair.raw_logical_agents == baseline.raw_logical_agents + 2
    assert event.raw_logical_agents == baseline.raw_logical_agents + 1
    assert repair.date_shards == event.date_shards == baseline.date_shards
    assert repair.candidate_shards == event.candidate_shards == baseline.candidate_shards
    assert repair.desired_model_concurrency == baseline.desired_model_concurrency
    assert event.desired_model_concurrency == baseline.desired_model_concurrency


def test_budget_is_monotone_over_representative_legal_states() -> None:
    dates = (1, 12, 13, 124)
    candidates = (1, 32, 33, 256, 2_000)
    previous_by_candidate: dict[int, int] = {}
    for D in dates:
        previous = -1
        for C in candidates:
            raw = derive_scale_directive(_control(D=D, C=C)).raw_logical_agents
            assert raw >= previous
            previous = raw
            if C in previous_by_candidate:
                assert raw >= previous_by_candidate[C]
            previous_by_candidate[C] = raw


def test_execution_layer_limits_never_follow_model_concurrency() -> None:
    for D, C in ((1, 32), (8, 256), (32, 1_000), (124, 2_000)):
        directive = derive_scale_directive(_control(D=D, C=C))
        assert directive.browser_concurrency == 6
        assert directive.qunar_lodging_concurrency == 1
        assert directive.date_pair_execution_concurrency == 1
        assert directive.icom_concurrency_per_pair == 4


@pytest.mark.asyncio
async def test_runtime_model_gate_starts_small_increases_and_halves_within_ceiling() -> None:
    gate = AdaptiveModelConcurrencyGate(ceiling=6, initial_limit=2, success_window=2)

    for successful in (True, True, True, True, False, True):
        await gate.acquire()
        await gate.release(successful=successful)

    audit = gate.audit()
    assert audit.initial_limit == 2
    assert audit.ceiling == 6
    assert audit.additive_increase_count == 2
    assert audit.multiplicative_decrease_count == 1
    assert audit.final_limit == 2
    assert audit.admitted_count == 6
    assert audit.success_count == 5
    assert audit.failure_count == 1


@pytest.mark.asyncio
async def test_runtime_model_gate_rejects_audit_while_lease_is_active() -> None:
    gate = AdaptiveModelConcurrencyGate(ceiling=2)
    await gate.acquire()
    with pytest.raises(RuntimeError, match="active leases"):
        gate.audit()
    await gate.release(successful=False)
