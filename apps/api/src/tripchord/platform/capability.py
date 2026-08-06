"""Provider capability profile (v0.2).

A :class:`ProviderScopeKey` is the stable identity ``(provider, vertical)``.
A :class:`ProviderCapability` describes one scope's read-only surface, its
certification stage and the versioned contracts that must match before a
run may generate browser tasks against it.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum

from pydantic import Field

from tripchord.domain.common import DomainModel


class ProviderVertical(StrEnum):
    """Business vertical a provider can serve."""

    FLIGHT = "flight"
    LODGING = "lodging"
    TRANSFER = "transfer"
    ACTIVITY = "activity"


class CertificationStage(StrEnum):
    """Provider certification lifecycle.

    Only ``CERTIFIED_ACTIVE`` scopes are eligible for default selection.
    ``SHADOW``/``TESTING`` adapters may run against fixtures but never enter
    the default selection or the Planner denominator.  ``COOLDOWN`` pauses a
    scope after repeated drift/login failures; ``DISABLED`` is terminal unless
    a new explicit user decision re-enables it.
    """

    DEVELOPMENT = "development"
    SHADOW = "shadow"
    TESTING = "testing"
    CERTIFIED_ACTIVE = "certified_active"
    COOLDOWN = "cooldown"
    DISABLED = "disabled"

    @property
    def default_eligible(self) -> bool:
        return self is CertificationStage.CERTIFIED_ACTIVE


class ProviderScopeKey(DomainModel):
    """Stable identity of one ``provider x vertical`` scope."""

    provider: str = Field(min_length=1, max_length=64)
    vertical: ProviderVertical

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.vertical.value}"

    def scope_sha256(self) -> str:
        raw = f"tripchord-scope-v1|{self.key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProviderScopeKey):
            return False
        return self.key == other.key


class ProviderCapability(DomainModel):
    """Versioned read-only capability profile for one scope.

    ``selector_contract_version`` and ``adapter_version`` change whenever the
    parser/selector contract or the adapter logic changes; a run snapshot binds
    them so late generation results cannot be attributed to a different scope.
    """

    key: ProviderScopeKey
    provider_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    vertical: ProviderVertical
    certification_stage: CertificationStage = CertificationStage.DEVELOPMENT
    adapter_version: str = Field(default="0.1.0")
    capability_version: str = Field(default="tripchord-capability-v1")
    official_domains: tuple[str, ...] = ()
    host_permissions: tuple[str, ...] = ()
    allowed_actions: frozenset[str] = frozenset(
        {"search", "filter", "open_detail"}
    )
    concurrency_limit: int = Field(default=1, ge=0, le=64)
    rate_limit_ms: int = Field(default=1_000, ge=0)
    login_precheck_required: bool = True
    health_probe_path: str | None = None
    supports_stable_detail_page: bool = False
    supports_prefilled_search_page: bool = False
    supports_param_card_only: bool = True
    selector_contract_version: str = "unversioned"
    excluded_reason: str | None = None

    @property
    def vertical_value(self) -> str:
        return self.vertical.value
