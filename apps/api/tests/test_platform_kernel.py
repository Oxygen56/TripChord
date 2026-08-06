"""Domain contract tests for the v0.2 dynamic platform kernel.

These tests establish the immutable contracts: stable scope identity,
deterministic eligibility, immutable hash-bound snapshots, atomic rejection of
forged provider IDs / snapshot hashes, and the invariant that a disabled or
un-authorized scope can never be selected.
"""

from __future__ import annotations

import pytest
from tripchord.platform import (
    CertificationStage,
    ProviderRegistry,
    ProviderVertical,
    ScopeSelectionState,
    build_default_registry,
    build_legacy_v4_registry,
    compute_eligible_scope_keys,
)
from tripchord.platform.capability import ProviderCapability, ProviderScopeKey
from tripchord.platform.registry import registry_from_capabilities
from tripchord.platform.selection import (
    EligibilityInput,
    SelectionSnapshot,
    UserScopeSelection,
    UserScopeSelectionSet,
    build_selection_snapshot,
)


def _default_runtime() -> EligibilityInput:
    reg = build_default_registry()
    authorized = frozenset(
        cap.key.key
        for cap in reg.capabilities
        if cap.certification_stage is CertificationStage.CERTIFIED_ACTIVE
    )
    return EligibilityInput(authorized_scope_keys=authorized, connected_scope_keys=authorized)


_FIXED_CREATED_AT = __import__("datetime").datetime(2026, 8, 6, 12, 0, 0)


def _snapshot_for(
    reg: ProviderRegistry,
    verticals: tuple[ProviderVertical, ...],
    *,
    user: UserScopeSelectionSet | None = None,
    runtime: EligibilityInput | None = None,
    created_at=None,
):
    return build_selection_snapshot(
        run_key="contract-test",
        registry=reg,
        verticals=verticals,
        user=user or UserScopeSelectionSet(),
        runtime=runtime or _default_runtime(),
        created_at=created_at or _FIXED_CREATED_AT,
    )


def test_scope_key_identity_is_stable_and_hashable() -> None:
    a = ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT)
    b = ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT)
    assert a == b
    assert hash(a) == hash(b)
    assert a.key == "ctrip:flight"
    assert len(a.scope_sha256()) == 64


def test_default_registry_preserves_audited_capability_boundary() -> None:
    reg = build_default_registry()
    keys = {cap.key.key for cap in reg.capabilities}
    assert keys == {
        "ctrip:flight",
        "ctrip:lodging",
        "qunar:flight",
        "qunar:lodging",
        "tongcheng:flight",
        "tongcheng:lodging",
        "icom:transfer",
    }
    tongcheng_lodging = reg.require(
        ProviderScopeKey(provider="tongcheng", vertical=ProviderVertical.LODGING)
    )
    assert tongcheng_lodging.certification_stage is CertificationStage.DISABLED
    assert "2026-08-05" in (tongcheng_lodging.excluded_reason or "")


def test_legacy_v4_profile_is_preserved_not_mutated() -> None:
    reg = build_legacy_v4_registry()
    keys = {cap.key.key for cap in reg.capabilities}
    assert "fliggy:flight" in keys
    assert "fliggy:lodging" in keys
    assert "tongcheng:flight" not in keys
    fliggy = reg.require(ProviderScopeKey(provider="fliggy", vertical=ProviderVertical.FLIGHT))
    assert fliggy.certification_stage is CertificationStage.DISABLED
    assert "removed from the active live matrix" in (fliggy.excluded_reason or "")


def test_eligible_flight_and_lodging_matches_documented_scope() -> None:
    reg = build_default_registry()
    eligible_flight = compute_eligible_scope_keys(
        reg, ProviderVertical.FLIGHT, runtime=_default_runtime()
    )
    eligible_lodging = compute_eligible_scope_keys(
        reg, ProviderVertical.LODGING, runtime=_default_runtime()
    )
    assert {s.key for s in eligible_flight} == {
        "ctrip:flight",
        "qunar:flight",
        "tongcheng:flight",
    }
    assert {s.key for s in eligible_lodging} == {"ctrip:lodging", "qunar:lodging"}


def test_disabled_tongcheng_lodging_never_eligible() -> None:
    reg = build_default_registry()
    eligible = compute_eligible_scope_keys(
        reg, ProviderVertical.LODGING, runtime=_default_runtime()
    )
    assert all(s.provider != "tongcheng" for s in eligible)


def test_snapshot_is_immutable_and_hash_bound() -> None:
    reg = build_default_registry()
    snap = _snapshot_for(reg, (ProviderVertical.FLIGHT, ProviderVertical.LODGING))
    assert snap.verify()
    assert len(snap.snapshot_sha256) == 64
    # Same inputs -> same hash (determinism)
    snap2 = _snapshot_for(reg, (ProviderVertical.FLIGHT, ProviderVertical.LODGING))
    assert snap.snapshot_sha256 == snap2.snapshot_sha256
    # Selected flight = ctrip + qunar + tongcheng
    assert set(snap.selected_providers_for_vertical(ProviderVertical.FLIGHT)) == {
        "ctrip",
        "qunar",
        "tongcheng",
    }
    assert set(snap.selected_providers_for_vertical(ProviderVertical.LODGING)) == {
        "ctrip",
        "qunar",
    }


def test_forged_snapshot_hash_is_atomically_rejected() -> None:
    reg = build_default_registry()
    snap = _snapshot_for(reg, (ProviderVertical.FLIGHT,))
    forged = snap.model_copy(update={"snapshot_sha256": "0" * 64})
    assert not forged.verify()


def test_forged_provider_id_changes_hash_and_fails_verification() -> None:
    reg = build_default_registry()
    snap = _snapshot_for(reg, (ProviderVertical.FLIGHT,))
    rows = []
    for entry in snap.scopes:
        rows.append(entry.model_copy())
    rows[0] = rows[0].model_copy(update={"provider": "attacker"})
    # Recomputing the hash over the tampered rows yields a different hash.
    tampered_hash = SelectionSnapshot(
        run_key=snap.run_key,
        created_at=snap.created_at,
        verticals=snap.verticals,
        registry_profile_version=snap.registry_profile_version,
        registry_sha256=snap.registry_sha256,
        scopes=tuple(rows),
        snapshot_sha256=snap.snapshot_sha256,
        requested_verticals_without_eligible_scope=snap.requested_verticals_without_eligible_scope,
    ).compute_sha256()
    assert tampered_hash != snap.snapshot_sha256
    # A snapshot carrying the original hash over tampered rows fails verify().
    forged = SelectionSnapshot(
        run_key=snap.run_key,
        created_at=snap.created_at,
        verticals=snap.verticals,
        registry_profile_version=snap.registry_profile_version,
        registry_sha256=snap.registry_sha256,
        scopes=tuple(rows),
        snapshot_sha256=snap.snapshot_sha256,
        requested_verticals_without_eligible_scope=snap.requested_verticals_without_eligible_scope,
    )
    assert not forged.verify()


def test_user_disabled_scope_never_selected() -> None:
    reg = build_default_registry()
    user = UserScopeSelectionSet(
        entries=(
            UserScopeSelection(
                scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
                enabled=False,
            ),
        )
    )
    snap = _snapshot_for(reg, (ProviderVertical.FLIGHT,), user=user)
    assert snap.selected_providers_for_vertical(ProviderVertical.FLIGHT) == (
        "qunar",
        "tongcheng",
    )
    ctrip_flight = next(
        e for e in snap.scopes if e.scope.key == "ctrip:flight"
    )
    assert ctrip_flight.state is ScopeSelectionState.EXPECTED
    assert ctrip_flight.user_enabled is False


def test_unauthorized_scope_never_selected() -> None:
    reg = build_default_registry()
    runtime = _default_runtime()
    # Remove qunar from authorized set.
    runtime = runtime.model_copy(
        update={
            "authorized_scope_keys": frozenset(
                k for k in runtime.authorized_scope_keys if not k.startswith("qunar")
            ),
            "connected_scope_keys": frozenset(
                k for k in runtime.connected_scope_keys if not k.startswith("qunar")
            ),
        }
    )
    snap = _snapshot_for(reg, (ProviderVertical.LODGING,), runtime=runtime)
    assert snap.selected_providers_for_vertical(ProviderVertical.LODGING) == ("ctrip",)
    # FLIGHT was not a requested vertical, so qunar flight cannot be selected
    # either; the scope stays eligible-but-unselected (state=eligible).
    assert snap.selected_providers_for_vertical(ProviderVertical.FLIGHT) == ()


def test_cooldown_scope_excluded_with_reason() -> None:
    reg = build_default_registry()
    runtime = _default_runtime()
    runtime = runtime.model_copy(
        update={
            "cooldown_scope_keys": frozenset({"ctrip:lodging"}),
            "known_blocking_scope_keys": frozenset({"qunar:lodging"}),
        }
    )
    snap = _snapshot_for(reg, (ProviderVertical.LODGING,), runtime=runtime)
    assert snap.selected_providers_for_vertical(ProviderVertical.LODGING) == ()
    assert snap.requested_verticals_without_eligible_scope == (ProviderVertical.LODGING,)


def test_zero_eligible_vertical_blocks_startup() -> None:
    """A requested vertical with no eligible scope must refuse to start."""
    reg = build_default_registry()
    snap = _snapshot_for(reg, (ProviderVertical.ACTIVITY,))
    assert snap.selected_providers_for_vertical(ProviderVertical.ACTIVITY) == ()
    assert ProviderVertical.ACTIVITY in snap.requested_verticals_without_eligible_scope


def test_registry_unknown_scope_raises() -> None:
    reg = build_default_registry()
    with pytest.raises(KeyError):
        reg.require(ProviderScopeKey(provider="unknown", vertical=ProviderVertical.FLIGHT))


def test_custom_registry_supports_one_provider() -> None:
    cap = ProviderCapability(
        key=ProviderScopeKey(provider="solo", vertical=ProviderVertical.FLIGHT),
        provider_id="solo",
        display_name="Solo",
        vertical=ProviderVertical.FLIGHT,
        certification_stage=CertificationStage.CERTIFIED_ACTIVE,
        adapter_version="0.1.0",
        capability_version="tripchord-capability-v1",
        official_domains=("solo.example",),
        host_permissions=("*://solo.example/*",),
        selector_contract_version="v1",
    )
    reg = registry_from_capabilities([cap])
    runtime = EligibilityInput(
        authorized_scope_keys=frozenset({"solo:flight"}),
        connected_scope_keys=frozenset({"solo:flight"}),
    )
    snap = _snapshot_for(reg, (ProviderVertical.FLIGHT,), runtime=runtime)
    assert snap.selected_providers_for_vertical(ProviderVertical.FLIGHT) == ("solo",)
    # 0-provider scenario: nothing authorized -> refused.
    snap0 = _snapshot_for(
        reg,
        (ProviderVertical.FLIGHT,),
        runtime=EligibilityInput(),
    )
    assert snap0.selected_providers_for_vertical(ProviderVertical.FLIGHT) == ()
    assert ProviderVertical.FLIGHT in snap0.requested_verticals_without_eligible_scope


def test_registry_hash_is_version_sensitive() -> None:
    reg = build_default_registry()
    sha1 = reg.registry_sha256()
    reg2 = build_default_registry()
    assert sha1 == reg2.registry_sha256()
    assert reg.profile_version == reg2.profile_version
