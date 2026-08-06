"""Provider selection and immutable run snapshot (v0.2).

The selection layer answers four distinct questions the roadmap separates:

1. what the user *wants* (persisted user selection);
2. what is currently *eligible* (registry + authorisation + health + cooldown);
3. what this run actually *selected* (frozen snapshot);
4. what each source *produced* (terminal receipts, handled in v0.3+).

A :class:`SelectionSnapshot` is immutable and hash-bound.  Agent tool layers
re-verify the snapshot hash so a prompt-injected or stale context cannot induce
access to an unselected scope.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import (
    CertificationStage,
    ProviderCapability,
    ProviderScopeKey,
    ProviderVertical,
)
from tripchord.platform.registry import ProviderRegistry

SNAPSHOT_SCHEMA_VERSION = "tripchord-selection-snapshot-v1"


class ScopeSelectionState(StrEnum):
    """State of one scope inside a frozen snapshot."""

    EXPECTED = "expected"
    ELIGIBLE = "eligible"
    SELECTED = "selected"
    EXCLUDED = "excluded"


class ScopeExclusionReason(StrEnum):
    """Deterministic exclusion reasons recorded on the snapshot."""

    NOT_CERTIFIED = "not_certified"
    VERTICAL_UNSUPPORTED = "vertical_unsupported"
    NOT_AUTHORIZED = "not_authorized"
    CONNECTION_UNHEALTHY = "connection_unhealthy"
    COOLDOWN = "cooldown"
    USER_DISABLED = "user_disabled"
    NO_ELIGIBLE_SCOPE_FOR_VERTICAL = "no_eligible_scope_for_vertical"


class UserScopeSelection(DomainModel):
    """Persisted per-scope user preference."""

    scope: ProviderScopeKey
    enabled: bool = True
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserScopeSelectionSet(DomainModel):
    """Persisted user toggles for a tenant/trip."""

    tenant_id: str | None = None
    entries: tuple[UserScopeSelection, ...] = ()

    def is_enabled(self, scope: ProviderScopeKey) -> bool:
        for entry in self.entries:
            if entry.scope == scope:
                return entry.enabled
        return True


class ScopeSnapshotEntry(DomainModel):
    """One scope row inside an immutable snapshot."""

    scope: ProviderScopeKey
    vertical: ProviderVertical
    provider: str
    state: ScopeSelectionState
    certification_stage: CertificationStage
    adapter_version: str = ""
    capability_version: str = ""
    selector_contract_version: str = ""
    host_permissions: tuple[str, ...] = ()
    exclusion_reason: ScopeExclusionReason | None = None
    user_enabled: bool = True
    detail: str | None = None


class EligibilityInput(DomainModel):
    """Runtime facts that gate eligibility, separate from the registry."""

    authorized_scope_keys: frozenset[str] = frozenset()
    connected_scope_keys: frozenset[str] = frozenset()
    cooldown_scope_keys: frozenset[str] = frozenset()
    known_blocking_scope_keys: frozenset[str] = frozenset()


def _default_eligibility() -> EligibilityInput:
    return EligibilityInput()


def _scope_eligible(
    cap: ProviderCapability,
    runtime: EligibilityInput,
    user: UserScopeSelectionSet,
) -> tuple[bool, ScopeExclusionReason | None, str | None]:
    if cap.certification_stage is not CertificationStage.CERTIFIED_ACTIVE:
        return False, ScopeExclusionReason.NOT_CERTIFIED, (
            f"certification_stage={cap.certification_stage.value}"
        )
    if not user.is_enabled(cap.key):
        return False, ScopeExclusionReason.USER_DISABLED, "user disabled this scope"
    if cap.key.key in runtime.cooldown_scope_keys:
        return False, ScopeExclusionReason.COOLDOWN, "scope is in cooldown"
    if cap.key.key in runtime.known_blocking_scope_keys:
        return False, ScopeExclusionReason.CONNECTION_UNHEALTHY, "known runtime blocking"
    if cap.key.key not in runtime.authorized_scope_keys:
        return False, ScopeExclusionReason.NOT_AUTHORIZED, "official domain not authorized"
    if cap.key.key not in runtime.connected_scope_keys:
        return False, ScopeExclusionReason.CONNECTION_UNHEALTHY, "companion not connected"
    return True, None, None


def compute_eligible_scope_keys(
    registry: ProviderRegistry,
    vertical: ProviderVertical,
    *,
    runtime: EligibilityInput | None = None,
    user: UserScopeSelectionSet | None = None,
) -> tuple[ProviderScopeKey, ...]:
    """Return eligible scope keys for a vertical, in registry order.

    A scope is eligible only when it is certified-active, supports the
    vertical, is authorised, is connected, not cooling down, not blocked and
    not disabled by the user.  The result is deterministic.
    """
    effective_runtime = runtime or _default_eligibility()
    effective_user = user or UserScopeSelectionSet()
    eligible: list[ProviderScopeKey] = []
    for cap in registry.capabilities_for_vertical(vertical):
        ok, _reason, _detail = _scope_eligible(cap, effective_runtime, effective_user)
        if ok:
            eligible.append(cap.key)
    return tuple(eligible)


class SelectionSnapshot(DomainModel):
    """Immutable per-run selection snapshot.

    Freeze the snapshot before launching any browser task or model tool call.
    ``snapshot_sha256`` binds the schema version, registry version and every
    selected/excluded scope row; tool layers re-verify it to atomically reject
    forged provider IDs or stale context.
    """

    run_key: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    verticals: tuple[ProviderVertical, ...] = ()
    registry_profile_version: str = ""
    registry_sha256: str = Field(min_length=64, max_length=64)
    snapshot_schema_version: str = SNAPSHOT_SCHEMA_VERSION
    scopes: tuple[ScopeSnapshotEntry, ...] = ()
    snapshot_sha256: str = Field(default="", min_length=64, max_length=64)
    requested_verticals_without_eligible_scope: tuple[ProviderVertical, ...] = ()

    def compute_sha256(self) -> str:
        return _compute_snapshot_sha256(self)

    def verify(self) -> bool:
        """Return whether the stored snapshot hash matches a recomputation.

        Tool layers and the scheduler call this before generating any task so a
        forged provider ID or stale snapshot hash is atomically rejected.
        """
        return self.snapshot_sha256 == self.compute_sha256()

    def selected_scope_keys(self) -> tuple[ProviderScopeKey, ...]:
        return tuple(
            entry.scope for entry in self.scopes if entry.state is ScopeSelectionState.SELECTED
        )

    def selected_providers_for_vertical(self, vertical: ProviderVertical) -> tuple[str, ...]:
        return tuple(
            entry.provider
            for entry in self.scopes
            if entry.vertical is vertical and entry.state is ScopeSelectionState.SELECTED
        )

    def scope_count_for_vertical(self, vertical: ProviderVertical) -> int:
        return len(self.selected_providers_for_vertical(vertical))

    def has_eligible_scope(self, vertical: ProviderVertical) -> bool:
        return any(
            entry.vertical is vertical
            and entry.state in {ScopeSelectionState.ELIGIBLE, ScopeSelectionState.SELECTED}
            for entry in self.scopes
        )


def _compute_snapshot_sha256(snapshot: SelectionSnapshot) -> str:
    """Compute the canonical snapshot hash from the model's own fields."""
    canonical = {
        "schema": snapshot.snapshot_schema_version,
        "run_key": snapshot.run_key,
        "created_at": snapshot.created_at.isoformat(),
        "registry_profile_version": snapshot.registry_profile_version,
        "registry_sha256": snapshot.registry_sha256,
        "verticals": [v.value for v in snapshot.verticals],
        "scopes": [
            {
                "scope": entry.scope.key,
                "provider": entry.provider,
                "state": entry.state.value,
                "stage": entry.certification_stage.value,
                "adapter_version": entry.adapter_version,
                "capability_version": entry.capability_version,
                "selector_contract_version": entry.selector_contract_version,
                "exclusion_reason": (
                    entry.exclusion_reason.value if entry.exclusion_reason else None
                ),
                "user_enabled": entry.user_enabled,
            }
            for entry in snapshot.scopes
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _snapshot_hash_from_parts(
    *,
    run_key: str,
    created_at: datetime,
    verticals: tuple[ProviderVertical, ...],
    registry_profile_version: str,
    registry_sha256: str,
    rows: tuple[ScopeSnapshotEntry, ...],
) -> str:
    """Compute the canonical hash from raw parts before model construction."""
    canonical = {
        "schema": SNAPSHOT_SCHEMA_VERSION,
        "run_key": run_key,
        "created_at": created_at.isoformat(),
        "registry_profile_version": registry_profile_version,
        "registry_sha256": registry_sha256,
        "verticals": [v.value for v in verticals],
        "scopes": [
            {
                "scope": entry.scope.key,
                "provider": entry.provider,
                "state": entry.state.value,
                "stage": entry.certification_stage.value,
                "adapter_version": entry.adapter_version,
                "capability_version": entry.capability_version,
                "selector_contract_version": entry.selector_contract_version,
                "exclusion_reason": (
                    entry.exclusion_reason.value if entry.exclusion_reason else None
                ),
                "user_enabled": entry.user_enabled,
            }
            for entry in rows
        ],
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_selection_snapshot(
    *,
    run_key: str,
    registry: ProviderRegistry,
    verticals: tuple[ProviderVertical, ...],
    user: UserScopeSelectionSet,
    runtime: EligibilityInput,
    created_at: datetime | None = None,
) -> SelectionSnapshot:
    """Build an immutable snapshot.

    The snapshot covers every registry scope, marking ``selected`` for scopes
    that are eligible for a requested vertical, ``eligible`` for eligible
    scopes outside the requested verticals, ``expected`` for certified scopes
    that are blocked, and ``excluded`` otherwise.  Requested verticals with no
    eligible scope are recorded so the runner can refuse to start before any
    browser or model call.
    """
    created = created_at or datetime.now(UTC)
    rows: list[ScopeSnapshotEntry] = []
    for cap in registry.capabilities:
        ok, reason, detail = _scope_eligible(cap, runtime, user)
        vertical_requested = cap.vertical in verticals
        if ok and vertical_requested:
            state = ScopeSelectionState.SELECTED
        elif ok:
            state = ScopeSelectionState.ELIGIBLE
        elif cap.certification_stage is CertificationStage.CERTIFIED_ACTIVE:
            state = ScopeSelectionState.EXPECTED
        else:
            state = ScopeSelectionState.EXCLUDED
        rows.append(
            ScopeSnapshotEntry(
                scope=cap.key,
                vertical=cap.vertical,
                provider=cap.provider_id,
                state=state,
                certification_stage=cap.certification_stage,
                adapter_version=cap.adapter_version,
                capability_version=cap.capability_version,
                selector_contract_version=cap.selector_contract_version,
                host_permissions=cap.host_permissions,
                exclusion_reason=reason,
                user_enabled=user.is_enabled(cap.key),
                detail=detail,
            )
        )
    missing_verticals = tuple(
        v
        for v in verticals
        if not any(row.vertical is v and row.state is ScopeSelectionState.SELECTED for row in rows)
    )
    hash_value = _snapshot_hash_from_parts(
        run_key=run_key,
        created_at=created,
        verticals=verticals,
        registry_profile_version=registry.profile_version,
        registry_sha256=registry.registry_sha256(),
        rows=tuple(rows),
    )
    return SelectionSnapshot(
        run_key=run_key,
        created_at=created,
        verticals=verticals,
        registry_profile_version=registry.profile_version,
        registry_sha256=registry.registry_sha256(),
        scopes=tuple(rows),
        snapshot_sha256=hash_value,
        requested_verticals_without_eligible_scope=missing_verticals,
    )
