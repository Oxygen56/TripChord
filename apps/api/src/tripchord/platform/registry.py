"""Deterministic provider registry (v0.2).

The registry is the versioned source of truth for which ``provider x vertical``
scopes exist and their current read-only capability.  It replaces the fixed
``LIVE_V5_PLATFORMS``/``LEGACY_V4_PLATFORMS`` tuples used by the backend,
frontend, Companion, Query Planner, live systems and Done-Gate.

The default profile is *backward compatible*: it mirrors the audited
2026-08-05 capability boundary (Ctrip flight+lodging, Qunar flight+lodging,
Tongcheng flight; Tongcheng overseas lodging intentionally disabled by the
user's explicit scope decision).  Historical v4 (Fliggy) is preserved as an
explicit legacy profile, never silently mutated.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import Field, PrivateAttr

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import (
    CertificationStage,
    ProviderCapability,
    ProviderScopeKey,
    ProviderVertical,
)


class ProviderRegistryProfile(DomainModel):
    """A frozen, versioned bundle of capabilities."""

    profile_version: str = Field(min_length=1)
    capabilities: tuple[ProviderCapability, ...] = ()
    generated_at: str = Field(default="deterministic")

    def capabilities_by_key(self) -> dict[str, ProviderCapability]:
        return {cap.key.key: cap for cap in self.capabilities}


def _default_capabilities() -> tuple[ProviderCapability, ...]:
    """Mirror the audited 2026-08-05 capability boundary (see providers.md).

    Tongcheng overseas lodging is intentionally disabled after the user
    explicitly skipped it on 2026-08-05; the exclusion reason is preserved so
    no agent can silently re-enable it.
    """
    ctrip_flight = ProviderCapability(
        key=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
        provider_id="ctrip",
        display_name="携程",
        vertical=ProviderVertical.FLIGHT,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("ctrip.com",),
        host_permissions=("*://*.ctrip.com/*",),
        allowed_actions=frozenset({"search", "filter", "select_outbound", "open_detail"}),
        concurrency_limit=6,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )
    ctrip_lodging = ProviderCapability(
        key=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.LODGING),
        provider_id="ctrip",
        display_name="携程",
        vertical=ProviderVertical.LODGING,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("ctrip.com",),
        host_permissions=("*://*.ctrip.com/*",),
        allowed_actions=frozenset({"search", "filter", "open_detail"}),
        concurrency_limit=6,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )
    qunar_flight = ProviderCapability(
        key=ProviderScopeKey(provider="qunar", vertical=ProviderVertical.FLIGHT),
        provider_id="qunar",
        display_name="去哪儿",
        vertical=ProviderVertical.FLIGHT,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("qunar.com",),
        host_permissions=("*://*.qunar.com/*",),
        allowed_actions=frozenset({"search", "filter", "open_detail"}),
        concurrency_limit=6,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )
    qunar_lodging = ProviderCapability(
        key=ProviderScopeKey(provider="qunar", vertical=ProviderVertical.LODGING),
        provider_id="qunar",
        display_name="去哪儿",
        vertical=ProviderVertical.LODGING,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("qunar.com",),
        host_permissions=("*://*.qunar.com/*",),
        allowed_actions=frozenset({"search", "filter", "open_detail"}),
        concurrency_limit=6,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )
    tongcheng_flight = ProviderCapability(
        key=ProviderScopeKey(provider="tongcheng", vertical=ProviderVertical.FLIGHT),
        provider_id="tongcheng",
        display_name="同程",
        vertical=ProviderVertical.FLIGHT,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("ly.com", "elong.com"),
        host_permissions=("*://*.ly.com/*", "*://*.elong.com/*"),
        allowed_actions=frozenset({"search", "filter", "select_outbound", "open_detail"}),
        concurrency_limit=1,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=True,
        supports_param_card_only=False,
        selector_contract_version="tripchord-visible-dom-v3",
    )
    tongcheng_lodging = ProviderCapability(
        key=ProviderScopeKey(provider="tongcheng", vertical=ProviderVertical.LODGING),
        provider_id="tongcheng",
        display_name="同程",
        vertical=ProviderVertical.LODGING,
        certification_stage=CertificationStage.DISABLED,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("ly.com", "elong.com"),
        host_permissions=(),
        allowed_actions=frozenset(),
        concurrency_limit=0,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=False,
        supports_prefilled_search_page=False,
        supports_param_card_only=True,
        selector_contract_version="tripchord-visible-dom-v3",
        excluded_reason=(
            "user explicitly skipped Tongcheng overseas lodging on 2026-08-05 "
            "after repeated account-security gates; re-entry requires a new "
            "explicit user decision"
        ),
    )
    icom_transfer = ProviderCapability(
        key=ProviderScopeKey(provider="icom", vertical=ProviderVertical.TRANSFER),
        provider_id="icom",
        display_name="ICom 官方公共接驳",
        vertical=ProviderVertical.TRANSFER,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.2.0",
        capability_version="tripchord-capability-v1",
        official_domains=("sfs-api.icomtours.com",),
        host_permissions=(),
        allowed_actions=frozenset({"search", "open_detail"}),
        concurrency_limit=4,
        rate_limit_ms=500,
        login_precheck_required=False,
        supports_stable_detail_page=True,
        supports_prefilled_search_page=False,
        supports_param_card_only=True,
        selector_contract_version="icom-public-transfer-v1",
    )
    return (
        ctrip_flight,
        ctrip_lodging,
        qunar_flight,
        qunar_lodging,
        tongcheng_flight,
        tongcheng_lodging,
        icom_transfer,
    )


LEGACY_V4_CAPABILITIES: tuple[ProviderCapability, ...] = (
    *tuple(
        cap
        for cap in _default_capabilities()
        if cap.key.provider != "tongcheng" and cap.vertical is not ProviderVertical.TRANSFER
    ),
    ProviderCapability(
        key=ProviderScopeKey(provider="fliggy", vertical=ProviderVertical.FLIGHT),
        provider_id="fliggy",
        display_name="飞猪",
        vertical=ProviderVertical.FLIGHT,
        certification_stage=CertificationStage.DISABLED,
        adapter_version="0.1.0",
        capability_version="tripchord-capability-v1",
        official_domains=("fliggy.com", "fliggy.hk"),
        host_permissions=(),
        allowed_actions=frozenset(),
        concurrency_limit=0,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=False,
        supports_prefilled_search_page=True,
        supports_param_card_only=True,
        selector_contract_version="tripchord-visible-dom-v3",
        excluded_reason=(
            "removed from the active live matrix after repeated verification "
            "gate failures made unattended read-only evidence collection "
            "unreliable (2026-08-04)"
        ),
    ),
    ProviderCapability(
        key=ProviderScopeKey(provider="fliggy", vertical=ProviderVertical.LODGING),
        provider_id="fliggy",
        display_name="飞猪",
        vertical=ProviderVertical.LODGING,
        certification_stage=CertificationStage.DISABLED,
        adapter_version="0.1.0",
        capability_version="tripchord-capability-v1",
        official_domains=("fliggy.com", "fliggy.hk"),
        host_permissions=(),
        allowed_actions=frozenset(),
        concurrency_limit=0,
        rate_limit_ms=1_000,
        login_precheck_required=True,
        supports_stable_detail_page=False,
        supports_prefilled_search_page=True,
        supports_param_card_only=True,
        selector_contract_version="tripchord-visible-dom-v3",
        excluded_reason=(
            "removed from the active live matrix after repeated verification "
            "gate failures (2026-08-04)"
        ),
    ),
)


def build_default_registry() -> ProviderRegistry:
    return ProviderRegistry(
        capabilities=_default_capabilities(),
        profile_version="tripchord-provider-profile-v1",
    )


def build_legacy_v4_registry() -> ProviderRegistry:
    return ProviderRegistry(
        capabilities=LEGACY_V4_CAPABILITIES,
        profile_version="tripchord-provider-profile-v4-legacy",
    )


class ProviderRegistry(DomainModel):
    """Versioned, deterministic provider capability registry.

    The registry is immutable once built.  Run-time health, cooldown and user
    authorisation are separate inputs consumed by :mod:`tripchord.platform
    .selection`; they never mutate the registry itself.
    """

    profile_version: str = Field(min_length=1)
    capabilities: tuple[ProviderCapability, ...] = ()
    _by_key: dict[str, ProviderCapability] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        self._by_key = {cap.key.key: cap for cap in self.capabilities}

    def get(self, scope: ProviderScopeKey) -> ProviderCapability | None:
        return self._by_key.get(scope.key)

    def require(self, scope: ProviderScopeKey) -> ProviderCapability:
        cap = self.get(scope)
        if cap is None:
            raise KeyError(f"unknown provider scope: {scope.key}")
        return cap

    def scopes(self) -> tuple[ProviderScopeKey, ...]:
        return tuple(cap.key for cap in self.capabilities)

    def capabilities_for_vertical(
        self, vertical: ProviderVertical
    ) -> tuple[ProviderCapability, ...]:
        return tuple(cap for cap in self.capabilities if cap.vertical is vertical)

    def certified_scopes(self) -> tuple[ProviderScopeKey, ...]:
        return tuple(
            cap.key
            for cap in self.capabilities
            if cap.certification_stage is CertificationStage.CERTIFIED_ACTIVE
        )

    def enabled_scope_keys(self) -> tuple[ProviderScopeKey, ...]:
        """Scopes that may generate browser/model work under the default profile.

        ``DISABLED`` scopes (Tongcheng overseas lodging, Fliggy) never appear
        here, so the DAG builder can fail closed if a caller asks for them.
        """
        return tuple(
            cap.key
            for cap in self.capabilities
            if cap.certification_stage is not CertificationStage.DISABLED
        )

    def capability_map(self) -> dict[str, ProviderCapability]:
        return dict(self._by_key)

    def registry_sha256(self) -> str:
        import hashlib
        import json

        canonical = {
            "profile_version": self.profile_version,
            "scopes": sorted(
                {
                    cap.key.key: {
                        "stage": cap.certification_stage.value,
                        "adapter_version": cap.adapter_version,
                        "capability_version": cap.capability_version,
                        "selector_contract_version": cap.selector_contract_version,
                    }
                    for cap in self.capabilities
                }.items()
            ),
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


def registry_from_capabilities(
    capabilities: Iterable[ProviderCapability],
    profile_version: str = "custom",
) -> ProviderRegistry:
    return ProviderRegistry(capabilities=tuple(capabilities), profile_version=profile_version)


def registry_from_mapping(
    mapping: Mapping[str, ProviderCapability],
    profile_version: str = "custom",
) -> ProviderRegistry:
    return registry_from_capabilities(mapping.values(), profile_version)
