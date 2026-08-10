"""Canonical frozen live-v4 source-graph members.

C-122 round-19 (supervision 17:03): the layer-6 done-gate compact must compare
the v4 source graph's member sets EXACTLY against one canonical frozen graph,
and BOTH the producer (``agents/live_done_gate_v4.py``) and the validator
(``scripts/run_product_done_gate.py``) must derive that graph from the SAME
sources.  A hardcoded copy in either place would drift silently and let a
forged graph with the right counts but the wrong members pass.

Every canonical set here is derived — never hand-written — from the same two
inputs the producer's ``_check_v4_source_graph`` uses:

- ``LIVE_V5_PLATFORMS`` / ``LIVE_V5_PLATFORM_QUERY_KINDS`` (the enabled live-v5
  platform capabilities, ``planning/flexible_dates.py``), and
- ``system_stay_plan_candidate_set()`` (the frozen maldives candidate set,
  ``planning/stay_plans.py``), whose segments carry the ``query_segment`` labels
  that become the per-platform browser Source ids and whose iCom-public-transfer
  contracts become the public-transfer Source-task ids.

The functions return immutable ``frozenset``/``tuple`` values so a caller can
never mutate the canonical contract in place.
"""

from __future__ import annotations

from functools import lru_cache

from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORM_QUERY_KINDS,
    LIVE_V5_PLATFORMS,
    QueryTaskKind,
)
from tripchord.planning.stay_plans import system_stay_plan_candidate_set

# C-122 HG-G: the frozen live-v4 scenario seals EXACTLY three date pairs, each
# executing the same fixed per-pair browser-source / query-task / iCom-source
# plan (mirror of the producer's ``len(run.pair_runs) != 3`` contract).
FROZEN_V4_PAIR_COUNT = 3


@lru_cache(maxsize=1)
def _frozen_candidate_set() -> object:
    """The immutable system-frozen candidate set the canonical graph is built from."""
    return system_stay_plan_candidate_set()


def _lodging_platforms() -> frozenset[object]:
    """Platforms whose live-v5 capabilities include a full-stay lodging query."""
    return frozenset(
        platform
        for platform in LIVE_V5_PLATFORMS
        if QueryTaskKind.LODGING_FULL_STAY in LIVE_V5_PLATFORM_QUERY_KINDS[platform]
    )


@lru_cache(maxsize=1)
def frozen_v4_query_shapes() -> frozenset[str]:
    """Canonical per-pair query-shape members as ``"platform:kind"`` strings.

    Each of the three frozen date pairs schedules one query task per enabled
    platform x kind (ctrip/qunar get all six kinds, tongcheng only flight).
    """
    return frozenset(
        f"{getattr(platform, 'value', platform)}:{getattr(kind, 'value', kind)}"
        for platform in LIVE_V5_PLATFORMS
        for kind in LIVE_V5_PLATFORM_QUERY_KINDS[platform]
    )


@lru_cache(maxsize=1)
def frozen_v4_browser_source_ids() -> frozenset[str]:
    """Canonical per-pair browser Source-agent id members.

    One ``"source-<platform>-flight"`` per enabled platform plus one
    ``"source-<platform>-lodging-<query_segment>"`` per segment of the frozen
    candidate set on every platform that carries a lodging capability — exactly
    the producer's ``expected_browser_source_ids`` derivation.
    """
    lodging_platforms = _lodging_platforms()
    candidate_set = _frozen_candidate_set()
    return frozenset(
        f"source-{getattr(platform, 'value', platform)}-{suffix}"
        for platform in LIVE_V5_PLATFORMS
        for suffix in (
            "flight",
            *(
                f"lodging-{segment.query_segment}"
                for plan in candidate_set.candidates
                for segment in plan.segments
                if platform in lodging_platforms
            ),
        )
    )


@lru_cache(maxsize=1)
def frozen_v4_icom_task_ids() -> frozenset[str]:
    """Canonical per-pair public-transfer Source-task id members.

    One ``"public-transfer-icom-<contract>"`` per iCom-public-transfer contract
    of the frozen candidate set — exactly the producer's ``expected_icom_tasks``
    derivation.
    """
    candidate_set = _frozen_candidate_set()
    return frozenset(
        f"public-transfer-icom-{contract.contract_id.removeprefix('icom-')}"
        for plan in candidate_set.candidates
        for contract in plan.required_transfer_contracts
        if contract.required_provider == "icom-public-transfer"
    )


@lru_cache(maxsize=1)
def frozen_v4_tasks_per_pair() -> int:
    """Canonical per-pair browser query task count (one task per Source id)."""
    return len(frozen_v4_browser_source_ids())
