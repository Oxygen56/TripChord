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

import hashlib
import re
from datetime import date
from functools import lru_cache

from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORM_QUERY_KINDS,
    LIVE_V5_PLATFORMS,
    FlexibleTravelWindow,
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


# The frozen live-v4 scenario's deterministic travel window (``benchmarks/
# scenarios/live-hgh-mle-aug-2026-v4.json``): HGH→MLE, every departure in
# August 2026, five-to-eight nights, two adults in one room, CNY.  The canonical
# ``date-pair:`` id derivation (``frozen_v4_pair_id_digest`` /
# ``frozen_v4_pair_id_is_canonical``) is the single source BOTH the producer's
# ``_check_v4_source_graph`` and the layer-6 validator use — a foreign pair id,
# a swapped pair or a missing/extra pair all fail closed even when every count
# and member list lines up.
#
# The actual sealed pair-id SET of a real run is not a fixed constant: the API
# applies ``minimum_departure_lead_days=7`` to the frozen window at run time and
# the pair execution refines the exploration anchors, so the run's three
# ``date-pair:`` ids depend on when it runs.  The layer-6 compact therefore
# carries the run's own sealed pair ids from the job control plane
# (``checkpoint_bound_pair_ids``) and the validator requires the compact's
# ``pair_ids`` to equal that set EXACTLY — a compact with foreign / swapped /
# missing / extra ids rejects even when every id is well-formed.
_FROZEN_V4_TRAVEL_WINDOW = FlexibleTravelWindow(
    origin="杭州",
    destination="马累",
    origin_code="HGH",
    destination_code="MLE",
    earliest_departure=date(2026, 8, 1),
    latest_departure=date(2026, 8, 31),
    min_nights=5,
    max_nights=8,
    adults=2,
    rooms=1,
    currency="CNY",
)


# A well-formed ``date-pair:`` id: ``date-pair:<departure>:<return>:<12-hex>``.
_PAIR_ID_FORMAT_RE = re.compile(
    r"^date-pair:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}):([0-9a-f]{12})$"
)


def frozen_v4_pair_id_digest(departure: date, return_date: date) -> str:
    """Recompute the canonical 12-hex pair-id digest from the frozen window.

    Mirrors ``FlexibleDateExplorer._pair_id`` (``planning/flexible_dates.py``):
    the digest is ``sha256(origin|destination|departure|return|adults|rooms|
    currency)[:12]`` over the frozen scenario's constants.  Any pair id whose
    digest does not recompute from these constants is not a real frozen-scenario
    pair id — it is foreign, regardless of how well-formed it looks.
    """
    raw = (
        f"{_FROZEN_V4_TRAVEL_WINDOW.origin}|{_FROZEN_V4_TRAVEL_WINDOW.destination}|"
        f"{departure.isoformat()}|{return_date.isoformat()}|"
        f"{_FROZEN_V4_TRAVEL_WINDOW.adults}|{_FROZEN_V4_TRAVEL_WINDOW.rooms}|"
        f"{_FROZEN_V4_TRAVEL_WINDOW.currency}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def frozen_v4_pair_id_is_canonical(pair_id: object) -> bool:
    """True only for a well-formed frozen-scenario ``date-pair:`` id.

    The producer (``agents/live_done_gate_v4.py``), the compact
    (``scripts/run_product_done_gate.py``) and the layer-6 validator all derive
    pair-id validity from this single function, so an id that is not a canonical
    frozen-scenario id fails closed everywhere.  ``pair-1`` and every other
    arbitrary string, plus any well-formed id whose digest does not recompute
    from the frozen constants, are rejected.
    """
    if not isinstance(pair_id, str):
        return False
    match = _PAIR_ID_FORMAT_RE.fullmatch(pair_id)
    if match is None:
        return False
    try:
        departure = date.fromisoformat(match.group(1))
        return_date = date.fromisoformat(match.group(2))
    except ValueError:
        return False
    return match.group(3) == frozen_v4_pair_id_digest(departure, return_date)
