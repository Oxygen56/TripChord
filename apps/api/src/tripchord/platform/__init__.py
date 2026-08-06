"""Dynamic provider platform kernel (v0.2).

The kernel replaces the fixed "exactly three platforms" assumption with a
deterministic, versioned provider registry and an immutable per-run selection
snapshot.  It is the single source of truth for which ``provider x vertical``
scopes exist, which are eligible, and which were actually selected for a run.

Deterministic code (registry, eligibility, snapshot hashing) owns scope
selection; no agent can change the registry or mint a snapshot hash.  Agents
may only propose changes to query order and candidate curation *within* the
scopes already frozen in the snapshot.
"""

from tripchord.platform.capability import (
    CertificationStage,
    ProviderCapability,
    ProviderVertical,
)
from tripchord.platform.registry import (
    ProviderRegistry,
    build_default_registry,
    build_legacy_v4_registry,
)
from tripchord.platform.selection import (
    ScopeSelectionState,
    ScopeSnapshotEntry,
    SelectionSnapshot,
    compute_eligible_scope_keys,
)

__all__ = [
    "CertificationStage",
    "ProviderCapability",
    "ProviderRegistry",
    "ProviderVertical",
    "ScopeSelectionState",
    "ScopeSnapshotEntry",
    "SelectionSnapshot",
    "build_default_registry",
    "build_legacy_v4_registry",
    "compute_eligible_scope_keys",
]
