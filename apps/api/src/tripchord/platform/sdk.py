"""Public Provider SDK, capability profile schema and conformance kit (v0.7).

v0.2 already wired a deterministic :class:`ProviderCapability` registry and the
per-scope selection kernel into the production path.  v0.7 turns that internal
shape into a *public* SDK so a new provider only needs one adapter plus a
versioned capability profile — never a change to the Planner/Barrier enums.

The SDK provides:

- :class:`ProviderAdapter` — the public read-only search surface a provider
  implements (schedule a browser task or an official public API call).
- :func:`validate_capability_profile` — the capability profile schema gate a
  profile must pass before it can be certified.
- :class:`ProviderProfileFixture` — a fixture template for shadow/testing
  adapters so a candidate provider can be exercised without real access.
- :class:`ProviderConformanceRunner` — the conformance test kit.  It verifies a
  profile + adapter pair against the production contract and returns a typed
  verdict.  Shadow adapters never enter the Planner, the coverage denominator or
  the default selection; certification is per ``provider x vertical`` and a
  flight certification never proves lodging.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import (
    CertificationStage,
    ProviderCapability,
    ProviderScopeKey,
    ProviderVertical,
)

SDK_SCHEMA_VERSION = "tripchord-provider-sdk-v1"


class ConformanceStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED_SHADOW = "skipped_shadow"


class ProviderAdapter(Protocol):
    """Public read-only search surface implemented by each provider adapter."""

    scope: ProviderScopeKey

    def can_schedule(self) -> bool: ...


class CapabilityProfileValidationError(ValueError):
    pass


def validate_capability_profile(capability: ProviderCapability) -> None:
    """Gate a capability profile against the SDK schema.

    A profile is valid only when it carries a stable scope key, a versioned
    selector/adapter contract and an audited host-permission allowlist.  Shadow
    and testing stages are allowed but can never become default-eligible.
    """
    if not capability.key.key:
        raise CapabilityProfileValidationError("capability profile requires a scope key")
    if capability.adapter_version in {"", "unversioned"}:
        raise CapabilityProfileValidationError(
            "capability profile requires a versioned adapter"
        )
    if capability.selector_contract_version in {"", "unversioned"}:
        raise CapabilityProfileValidationError(
            "capability profile requires a versioned selector contract"
        )
    if capability.certification_stage is CertificationStage.CERTIFIED_ACTIVE:
        if not capability.official_domains:
            raise CapabilityProfileValidationError(
                "certified-active capability requires official domains"
            )
        if not capability.host_permissions:
            raise CapabilityProfileValidationError(
                "certified-active capability requires host permissions"
            )
        has_locator = (
            capability.supports_stable_detail_page
            or capability.supports_prefilled_search_page
            or capability.supports_param_card_only
        )
        if not capability.supports_param_card_only and not has_locator:
            raise CapabilityProfileValidationError(
                "certified-active capability must declare a stable detail "
                "page, a prefilled search page or a parameter card"
            )


class ProviderProfileFixture(DomainModel):
    """A fixture template for shadow/testing adapters.

    A shadow adapter exercises the same capability profile schema and a bounded
    fixture search surface without touching a real OTA.  It never participates
    in the Planner, the coverage denominator or the default selection.
    """

    schema_version: str = SDK_SCHEMA_VERSION
    fixture_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    certification_stage: CertificationStage = CertificationStage.SHADOW
    fixture_quotes: tuple[dict[str, object], ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_fixture_stage(self) -> ProviderProfileFixture:
        if self.certification_stage in {
            CertificationStage.CERTIFIED_ACTIVE,
            CertificationStage.DISABLED,
        }:
            raise ValueError("provider profile fixture cannot be certified or disabled")
        if not self.fixture_quotes:
            raise ValueError("provider profile fixture requires at least one fixture quote")
        return self

    def fixture_quote_count(self) -> int:
        return len(self.fixture_quotes)


class ProviderConformanceRunner:
    """The conformance test kit.

    ``run`` validates the profile schema and returns a typed verdict.  Shadow
    adapters are reported as ``skipped_shadow`` (they never contribute to the
    Planner or the coverage denominator).  Certification is evaluated per
    ``provider x vertical``; a passing flight adapter never proves lodging.
    """

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    def run(
        self,
        capability: ProviderCapability,
        adapter: ProviderAdapter | None = None,
        *,
        vertical: ProviderVertical | None = None,
    ) -> ConformanceStatus:
        target_vertical = vertical or capability.vertical
        if capability.key.vertical is not target_vertical:
            return ConformanceStatus.FAIL
        if capability.certification_stage is CertificationStage.SHADOW:
            return ConformanceStatus.SKIPPED_SHADOW
        validate_capability_profile(capability)
        if capability.certification_stage is CertificationStage.CERTIFIED_ACTIVE:
            if adapter is None:
                return ConformanceStatus.FAIL
            if not adapter.can_schedule():
                return ConformanceStatus.FAIL
            return ConformanceStatus.PASS
        return ConformanceStatus.PASS


class ProviderStateTransition(BaseModel):
    """A deterministic provider lifecycle transition (v0.7 state machine)."""

    scope: ProviderScopeKey
    from_stage: CertificationStage
    to_stage: CertificationStage
    reason: str = Field(min_length=1, max_length=400)
    performed_at: datetime

    @model_validator(mode="after")
    def validate_transition(self) -> ProviderStateTransition:
        allowed: dict[CertificationStage, set[CertificationStage]] = {
            CertificationStage.DEVELOPMENT: {
                CertificationStage.SHADOW,
                CertificationStage.TESTING,
                CertificationStage.DISABLED,
            },
            CertificationStage.SHADOW: {
                CertificationStage.TESTING,
                CertificationStage.COOLDOWN,
                CertificationStage.DISABLED,
            },
            CertificationStage.TESTING: {
                CertificationStage.CERTIFIED_ACTIVE,
                CertificationStage.COOLDOWN,
                CertificationStage.DISABLED,
            },
            CertificationStage.CERTIFIED_ACTIVE: {
                CertificationStage.COOLDOWN,
                CertificationStage.DISABLED,
            },
            CertificationStage.COOLDOWN: {
                CertificationStage.TESTING,
                CertificationStage.DISABLED,
            },
            CertificationStage.DISABLED: set(),
        }
        if self.to_stage not in allowed[self.from_stage]:
            raise ValueError(
                f"illegal provider lifecycle transition {self.from_stage.value} -> "
                f"{self.to_stage.value}"
            )
        return self


def one_click_cooldown(
    capability: ProviderCapability,
    *,
    performed_at: datetime,
    reason: str,
) -> tuple[ProviderCapability, ProviderStateTransition]:
    """Pause one ``provider x vertical`` scope (one-click per-vertical cooldown)."""
    transition = ProviderStateTransition(
        scope=capability.key,
        from_stage=capability.certification_stage,
        to_stage=CertificationStage.COOLDOWN,
        reason=reason,
        performed_at=performed_at,
    )
    updated = capability.model_copy(
        update={
            "certification_stage": CertificationStage.COOLDOWN,
            "excluded_reason": reason,
        }
    )
    return updated, transition
