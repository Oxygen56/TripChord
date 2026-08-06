"""Provider capability matrix and user selection API (v0.2).

Exposes the deterministic provider registry as a ``provider x vertical``
capability matrix, per-scope runtime health and the persisted user selection.
The frontend renders this matrix instead of a fixed three-platform union.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.registry import ProviderRegistry
from tripchord.platform.selection import (
    EligibilityInput,
    ScopeSnapshotEntry,
    SelectionSnapshot,
    UserScopeSelection,
    UserScopeSelectionSet,
    build_selection_snapshot,
)

router = APIRouter(prefix="/api/v1", tags=["providers"])

_SELECTION_FILE_MODE = 0o600


class ProviderScopeView(BaseModel):
    key: str
    provider: str
    display_name: str
    vertical: str
    certification_stage: str
    adapter_version: str
    capability_version: str
    selector_contract_version: str
    official_domains: tuple[str, ...] = ()
    host_permissions: tuple[str, ...] = ()
    supports_stable_detail_page: bool = False
    supports_prefilled_search_page: bool = False
    supports_param_card_only: bool = True
    concurrency_limit: int = 0
    rate_limit_ms: int = 0
    excluded_reason: str | None = None
    eligible: bool = False
    state: str | None = None
    user_enabled: bool = True
    exclusion_reason: str | None = None


class ProviderCapabilitiesResponse(BaseModel):
    profile_version: str
    registry_sha256: str
    scopes: tuple[ProviderScopeView, ...] = ()
    missing_verticals: tuple[str, ...] = ()


class ProviderRuntimeHealthResponse(BaseModel):
    companion_connected_scope_keys: tuple[str, ...] = ()
    authorized_scope_keys: tuple[str, ...] = ()
    cooldown_scope_keys: tuple[str, ...] = ()
    known_blocking_scope_keys: tuple[str, ...] = ()
    model_endpoint_healthy: bool = False


class ProviderSelectionRequest(BaseModel):
    scope: str = Field(min_length=1)
    enabled: bool = True


class ProviderSelectionResponse(BaseModel):
    updated: tuple[ProviderScopeView, ...] = ()
    snapshot_sha256: str | None = None


class _UserScopeSelectionStore:
    """Local-first persisted user selection (single writer, atomic JSON)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or Path(".runtime/provider-selection.json")
        self._entries: dict[str, bool] = {}

    def load(self) -> dict[str, bool]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            raw = payload.get("entries", {})
            if not isinstance(raw, dict):
                return {}
            return {str(key): bool(value) for key, value in raw.items()}
        except (OSError, json.JSONDecodeError):
            return {}

    def set_enabled(self, scope_key: str, enabled: bool) -> dict[str, bool]:
        self._entries[scope_key] = enabled
        self._persist()
        return dict(self._entries)

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            {"entries": dict(sorted(self._entries.items()))},
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(canonical, encoding="utf-8")
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self._path)
        with suppress(OSError):
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)


_store = _UserScopeSelectionStore()


def _runtime_input() -> EligibilityInput:
    """Derive runtime eligibility from the environment's authorised scopes.

    The default local profile authorises the audited browser domains and the
    iCom public transfer endpoint.  Real Companion authorisation comes from the
    heartbeat in a later version; this deterministic default keeps the matrix
    useful before a Companion is paired.
    """
    authorized = frozenset(
        {
            "ctrip:flight",
            "ctrip:lodging",
            "qunar:flight",
            "qunar:lodging",
            "tongcheng:flight",
            "icom:transfer",
        }
    )
    return EligibilityInput(
        authorized_scope_keys=authorized,
        connected_scope_keys=authorized,
        cooldown_scope_keys=frozenset(),
        known_blocking_scope_keys=frozenset(),
    )


def _user_selection_set() -> UserScopeSelectionSet:
    entries = []
    for key, enabled in _store.load().items():
        try:
            provider, vertical = key.split(":", 1)
            scope = ProviderScopeKey(
                provider=provider, vertical=ProviderVertical(vertical)
            )
        except (ValueError, KeyError):
            continue
        entries.append(
            UserScopeSelection(scope=scope, enabled=enabled)
        )
    return UserScopeSelectionSet(entries=tuple(entries))


def _to_view(
    entry: ScopeSnapshotEntry,
    eligible_keys: frozenset[str],
) -> ProviderScopeView:
    return ProviderScopeView(
        key=entry.scope.key,
        provider=entry.provider,
        display_name=entry.scope.provider,
        vertical=entry.vertical.value,
        certification_stage=entry.certification_stage.value,
        adapter_version=entry.adapter_version,
        capability_version=entry.capability_version,
        selector_contract_version=entry.selector_contract_version,
        official_domains=entry.host_permissions,
        host_permissions=entry.host_permissions,
        supports_stable_detail_page=False,
        supports_prefilled_search_page=False,
        supports_param_card_only=True,
        concurrency_limit=0,
        rate_limit_ms=0,
        excluded_reason=entry.detail,
        eligible=entry.scope.key in eligible_keys,
        state=entry.state.value,
        user_enabled=entry.user_enabled,
        exclusion_reason=entry.exclusion_reason.value if entry.exclusion_reason else None,
    )


def _registry_from_state(request: Request) -> ProviderRegistry:
    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        from tripchord.platform.registry import build_default_registry

        registry = build_default_registry()
    return registry


def _snapshot_for(request: Request) -> SelectionSnapshot:
    registry = _registry_from_state(request)
    user = _user_selection_set()
    runtime = _runtime_input()
    return build_selection_snapshot(
        run_key=f"capabilities-{datetime.now(UTC).isoformat()}",
        registry=registry,
        verticals=(
            ProviderVertical.FLIGHT,
            ProviderVertical.LODGING,
            ProviderVertical.TRANSFER,
        ),
        user=user,
        runtime=runtime,
    )


@router.get("/providers/capabilities", response_model=ProviderCapabilitiesResponse)
async def provider_capabilities_endpoint(
    request: Request,
) -> ProviderCapabilitiesResponse:
    registry = _registry_from_state(request)
    snapshot = _snapshot_for(request)
    eligible = frozenset(key.key for key in snapshot.selected_scope_keys())
    views = tuple(_to_view(entry, eligible) for entry in snapshot.scopes)
    return ProviderCapabilitiesResponse(
        profile_version=registry.profile_version,
        registry_sha256=registry.registry_sha256(),
        scopes=views,
        missing_verticals=tuple(
            v.value for v in snapshot.requested_verticals_without_eligible_scope
        ),
    )


@router.get("/providers/runtime-health", response_model=ProviderRuntimeHealthResponse)
async def provider_runtime_health_endpoint(request: Request) -> ProviderRuntimeHealthResponse:
    runtime = _runtime_input()
    return ProviderRuntimeHealthResponse(
        companion_connected_scope_keys=tuple(sorted(runtime.connected_scope_keys)),
        authorized_scope_keys=tuple(sorted(runtime.authorized_scope_keys)),
        cooldown_scope_keys=tuple(sorted(runtime.cooldown_scope_keys)),
        known_blocking_scope_keys=tuple(sorted(runtime.known_blocking_scope_keys)),
        model_endpoint_healthy=False,
    )


@router.put("/preferences/provider-selection", response_model=ProviderSelectionResponse)
async def provider_selection_endpoint(
    request: Request,
    selection: ProviderSelectionRequest,
) -> ProviderSelectionResponse:
    _store.set_enabled(selection.scope, selection.enabled)
    snapshot = _snapshot_for(request)
    eligible = frozenset(key.key for key in snapshot.selected_scope_keys())
    views = tuple(_to_view(entry, eligible) for entry in snapshot.scopes)
    return ProviderSelectionResponse(updated=views, snapshot_sha256=snapshot.snapshot_sha256)


def require_eligible_scope_for_verticals(
    snapshot: SelectionSnapshot,
    verticals: tuple[ProviderVertical, ...],
) -> None:
    """Refuse to start before any browser/model call when a requested vertical
    has no eligible source (v0.2 contract)."""
    missing = [
        v for v in verticals if v in snapshot.requested_verticals_without_eligible_scope
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "refusing to start: no eligible provider scope for required "
                f"vertical(s): {', '.join(v.value for v in missing)}"
            ),
        )


def guard_live_start(
    request: Request,
    verticals: tuple[ProviderVertical, ...],
) -> SelectionSnapshot:
    """Build the current selection snapshot and refuse to start if any required
    vertical has no eligible scope.  Returns the snapshot for downstream use."""
    registry = _registry_from_state(request)
    user = _user_selection_set()
    runtime = _runtime_input()
    snapshot = build_selection_snapshot(
        run_key=f"guard-{datetime.now(UTC).isoformat()}",
        registry=registry,
        verticals=verticals,
        user=user,
        runtime=runtime,
    )
    require_eligible_scope_for_verticals(snapshot, verticals)
    return snapshot
