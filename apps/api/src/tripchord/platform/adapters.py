"""Backward-compatible adapters from registry scopes to existing enums (v0.2).

The v0.1 planning/live code consumes fixed tuples of ``TravelPlatform`` /
``BrowserProvider``.  These adapters derive the same tuples from a
:class:`SelectionSnapshot` so dynamic 0/1/2/3/4-provider scenarios can drive
the same DAG builders without changing their internals.
"""

from __future__ import annotations

from tripchord.planning.flexible_dates import TravelPlatform
from tripchord.platform.capability import ProviderVertical
from tripchord.platform.selection import SelectionSnapshot
from tripchord.providers.browser_bridge import BrowserProvider

# Registry provider ids map 1:1 onto the existing planning/live enums.
_PROVIDER_TO_TRAVEL_PLATFORM: dict[str, TravelPlatform] = {
    member.value: member for member in TravelPlatform
}
_PROVIDER_TO_BROWSER_PROVIDER: dict[str, BrowserProvider] = {
    member.value: member for member in BrowserProvider
}


def _vertical(value: str | ProviderVertical) -> ProviderVertical:
    if isinstance(value, ProviderVertical):
        return value
    return ProviderVertical(value)


def selected_travel_platforms(
    snapshot: SelectionSnapshot,
    vertical: str | ProviderVertical,
) -> tuple[TravelPlatform, ...]:
    """Map selected providers for one vertical onto ``TravelPlatform`` values."""
    target = _vertical(vertical)
    result: list[TravelPlatform] = []
    for provider in snapshot.selected_providers_for_vertical(target):
        mapped = _PROVIDER_TO_TRAVEL_PLATFORM.get(provider)
        if mapped is not None:
            result.append(mapped)
    return tuple(result)


def selected_browser_providers(
    snapshot: SelectionSnapshot,
    vertical: str | ProviderVertical,
) -> tuple[BrowserProvider, ...]:
    """Map selected providers for one vertical onto ``BrowserProvider`` values."""
    target = _vertical(vertical)
    result: list[BrowserProvider] = []
    for provider in snapshot.selected_providers_for_vertical(target):
        mapped = _PROVIDER_TO_BROWSER_PROVIDER.get(provider)
        if mapped is not None:
            result.append(mapped)
    return tuple(result)


def default_platforms_from_registry() -> tuple[TravelPlatform, ...]:
    """Derive the default flight+lodging platform set from the default registry.

    Used at application assembly time so the planning/live systems are built
    from the registry's certified scopes rather than a hard-coded tuple.
    """
    snapshot = _default_snapshot()
    merged: list[TravelPlatform] = []
    for provider in snapshot.selected_providers_for_vertical(ProviderVertical.FLIGHT):
        mapped = _PROVIDER_TO_TRAVEL_PLATFORM.get(provider)
        if mapped is not None and mapped not in merged:
            merged.append(mapped)
    for provider in snapshot.selected_providers_for_vertical(ProviderVertical.LODGING):
        mapped = _PROVIDER_TO_TRAVEL_PLATFORM.get(provider)
        if mapped is not None and mapped not in merged:
            merged.append(mapped)
    return tuple(merged)


def default_browser_providers_from_registry() -> tuple[BrowserProvider, ...]:
    """Derive the default browser provider set from the default registry."""
    snapshot = _default_snapshot()
    result: list[BrowserProvider] = []
    for provider in snapshot.selected_providers_for_vertical(ProviderVertical.FLIGHT):
        mapped = _PROVIDER_TO_BROWSER_PROVIDER.get(provider)
        if mapped is not None and mapped not in result:
            result.append(mapped)
    for provider in snapshot.selected_providers_for_vertical(ProviderVertical.LODGING):
        mapped = _PROVIDER_TO_BROWSER_PROVIDER.get(provider)
        if mapped is not None and mapped not in result:
            result.append(mapped)
    return tuple(result)


def _default_snapshot() -> SelectionSnapshot:
    """Build a snapshot that requests flight+lodging under the default profile."""
    from tripchord.platform.capability import ProviderVertical
    from tripchord.platform.registry import build_default_registry
    from tripchord.platform.selection import (
        EligibilityInput,
        UserScopeSelectionSet,
        build_selection_snapshot,
    )

    registry = build_default_registry()
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
    return build_selection_snapshot(
        run_key="assembly-default",
        registry=registry,
        verticals=(ProviderVertical.FLIGHT, ProviderVertical.LODGING),
        user=UserScopeSelectionSet(),
        runtime=EligibilityInput(
            authorized_scope_keys=authorized,
            connected_scope_keys=authorized,
        ),
    )
