"""v0.7 Provider SDK / capability profile / conformance kit contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tripchord.platform.capability import (
    CertificationStage,
    ProviderCapability,
    ProviderScopeKey,
    ProviderVertical,
)
from tripchord.platform.sdk import (
    CapabilityProfileValidationError,
    ConformanceStatus,
    ProviderConformanceRunner,
    ProviderProfileFixture,
    ProviderStateTransition,
    one_click_cooldown,
    validate_capability_profile,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _capability(
    *,
    stage: CertificationStage = CertificationStage.CERTIFIED_ACTIVE,
    vertical: ProviderVertical = ProviderVertical.FLIGHT,
) -> ProviderCapability:
    return ProviderCapability(
        key=ProviderScopeKey(provider="ctrip", vertical=vertical),
        provider_id="ctrip",
        display_name="携程",
        vertical=vertical,
        certification_stage=stage,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("ctrip.com",),
        host_permissions=("*://*.ctrip.com/*",),
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )


class _SchedulableAdapter:
    def __init__(self, scope: ProviderScopeKey, can_schedule: bool = True) -> None:
        self.scope = scope
        self._can_schedule = can_schedule

    def can_schedule(self) -> bool:
        return self._can_schedule


def test_certified_profile_requires_audited_hosts_and_locator() -> None:
    capability = _capability()
    validate_capability_profile(capability)

    bad = capability.model_copy(update={"host_permissions": ()})
    with pytest.raises(CapabilityProfileValidationError):
        validate_capability_profile(bad)

    no_locator = capability.model_copy(
        update={
            "supports_stable_detail_page": False,
            "supports_prefilled_search_page": False,
            "supports_param_card_only": False,
        }
    )
    with pytest.raises(CapabilityProfileValidationError):
        validate_capability_profile(no_locator)


def test_conformance_skips_shadow_and_fails_unversioned() -> None:
    runner = ProviderConformanceRunner(now=NOW)
    shadow = _capability(stage=CertificationStage.SHADOW)
    assert runner.run(shadow) is ConformanceStatus.SKIPPED_SHADOW

    unversioned = _capability().model_copy(update={"adapter_version": "unversioned"})
    with pytest.raises(CapabilityProfileValidationError):
        runner.run(unversioned)


def test_conformance_is_per_vertical_and_requires_schedulable_adapter() -> None:
    runner = ProviderConformanceRunner(now=NOW)
    capability = _capability()
    adapter = _SchedulableAdapter(capability.key, can_schedule=True)
    assert runner.run(capability, adapter) is ConformanceStatus.PASS
    assert (
        runner.run(capability, adapter, vertical=ProviderVertical.LODGING)
        is ConformanceStatus.FAIL
    )
    broken = _SchedulableAdapter(capability.key, can_schedule=False)
    assert runner.run(capability, broken) is ConformanceStatus.FAIL
    assert runner.run(capability, None) is ConformanceStatus.FAIL


def test_shadow_fixture_never_certified_or_disabled() -> None:
    fixture = ProviderProfileFixture(
        fixture_id="fixture-ctrip-flight",
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        certification_stage=CertificationStage.SHADOW,
        fixture_quotes=({"price_cents": 1000},),
        created_at=NOW,
    )
    assert fixture.fixture_quote_count() == 1
    with pytest.raises(ValueError):
        ProviderProfileFixture(
            fixture_id="bad",
            scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
            certification_stage=CertificationStage.CERTIFIED_ACTIVE,
            fixture_quotes=({"price_cents": 1000},),
        )
    with pytest.raises(ValueError):
        ProviderProfileFixture(
            fixture_id="bad",
            scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
            certification_stage=CertificationStage.SHADOW,
            fixture_quotes=(),
        )


def test_lifecycle_transition_rejects_illegal_edges() -> None:
    with pytest.raises(ValueError):
        ProviderStateTransition(
            scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
            from_stage=CertificationStage.SHADOW,
            to_stage=CertificationStage.CERTIFIED_ACTIVE,
            reason="shadow cannot skip testing",
            performed_at=NOW,
        )
    transition = ProviderStateTransition(
        scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        from_stage=CertificationStage.TESTING,
        to_stage=CertificationStage.CERTIFIED_ACTIVE,
        reason="canary passed",
        performed_at=NOW,
    )
    assert transition.to_stage is CertificationStage.CERTIFIED_ACTIVE


def test_one_click_cooldown_pauses_scope_and_records_transition() -> None:
    capability = _capability()
    updated, transition = one_click_cooldown(
        capability,
        performed_at=NOW,
        reason="repeated dom drift",
    )
    assert updated.certification_stage is CertificationStage.COOLDOWN
    assert transition.from_stage is CertificationStage.CERTIFIED_ACTIVE
    assert transition.to_stage is CertificationStage.COOLDOWN
    # A cooldown scope is no longer default-eligible.
    assert updated.certification_stage.default_eligible is False
