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
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache

from tripchord.planning.adaptive_dates import (
    ExactDatePairObservation,
    RankedTopKDateRefiner,
)
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORM_QUERY_KINDS,
    LIVE_V5_PLATFORMS,
    AuditableDatePair,
    DatePairSource,
    FlexibleDateExplorer,
    FlexibleQueryPlanBuilder,
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
# R44 (canonical pair-set authority): the exact ORDERED trio a frozen run must
# seal is NOT a self-declared run-time constant.  Producer, compact and consumer
# all derive it from the SAME committed inputs — this frozen window, the frozen
# scenario's committed ``reference_date`` (``FROZEN_V4_REFERENCE_DATE``, from
# ``benchmarks/scenarios/live-hgh-mle-aug-2026-v4.json``) and the production
# date-selection algorithm (``FlexibleDateExplorer.explore`` +
# ``RankedTopKDateRefiner``) — via ``frozen_v4_canonical_pair_ids``.  The
# consumer independently recomputes that ordered trio and requires the compact's
# ``pair_ids`` / ``checkpoint_bound_pair_ids`` / per-pair / checkpoint bindings
# to match item by item, so a joint self-consistent replacement, a wrong order,
# a missing/extra pair or any other individually-valid foreign set fails closed.
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


def _is_frozen_v4_window(window: object) -> bool:
    """True when ``window`` is the frozen live-v4 scenario's travel window.

    C-122 supervision 02:56 (round-19 continuation): the frozen time contract
    must be enforced in the REAL generation path — ``FlexibleDateExplorer._pair_id``
    delegates to ``frozen_v4_pair_id`` exactly when this returns true.  The
    API raises ``earliest_departure`` to the run-time
    ``minimum_departure_lead_days`` boundary before exploration, so it is
    allowed to differ from the canonical 2026-08-01 (any later date inside the
    frozen window keeps the scenario identity); every OTHER contract field must
    match exactly, so a foreign window (different city, night range, party, or
    currency) keeps the generic pair-id generation and can never inherit the
    frozen contract's digests.
    """
    frozen = _FROZEN_V4_TRAVEL_WINDOW
    if not isinstance(window, FlexibleTravelWindow):
        return False
    if window is frozen:
        return True
    return (
        window.origin == frozen.origin
        and window.destination == frozen.destination
        and window.origin_code == frozen.origin_code
        and window.destination_code == frozen.destination_code
        and window.earliest_departure >= frozen.earliest_departure
        and window.latest_departure == frozen.latest_departure
        and window.min_nights == frozen.min_nights
        and window.max_nights == frozen.max_nights
        and window.adults == frozen.adults
        and window.rooms == frozen.rooms
        and window.currency == frozen.currency
    )


def frozen_v4_pair_id_digest(departure: date, return_date: date) -> str:
    """Recompute the canonical 12-hex pair-id digest from the frozen window.

    Mirrors ``FlexibleDateExplorer._pair_id`` (``planning/flexible_dates.py``):
    the digest is ``sha256(origin|destination|departure|return|adults|rooms|
    currency)[:12]`` over the frozen scenario's constants.  Any pair id whose
    digest does not recompute from these constants is not a real frozen-scenario
    pair id — it is foreign, regardless of how well-formed it looks.

    This is the pure digest computation.  ``frozen_v4_pair_id`` is the
    generation entry point that enforces the canonical time contract BEFORE the
    digest is computed (it raises on an out-of-contract pair), and
    ``frozen_v4_pair_id_is_canonical`` enforces the SAME contract on acceptance.
    """
    raw = (
        f"{_FROZEN_V4_TRAVEL_WINDOW.origin}|{_FROZEN_V4_TRAVEL_WINDOW.destination}|"
        f"{departure.isoformat()}|{return_date.isoformat()}|"
        f"{_FROZEN_V4_TRAVEL_WINDOW.adults}|{_FROZEN_V4_TRAVEL_WINDOW.rooms}|"
        f"{_FROZEN_V4_TRAVEL_WINDOW.currency}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def frozen_v4_pair_id_contract_violation(
    departure: date, return_date: date
) -> str | None:
    """The canonical time contract's violation reason, or ``None`` when the pair
    satisfies it (C-122 supervision 18:13).

    The frozen maldives scenario seals ONLY August-2026 departures that return
    five-to-eight nights later: departure within
    ``[earliest_departure, latest_departure]``, ``return_date > departure`` and
    ``min_nights <= (return_date - departure).days <= max_nights``.  A 2030
    departure, a reversed (return <= departure) pair and a 1/9/10-night stay all
    violate it — the SAME reason string drives the producer's
    ``_check_v4_source_graph``, the compact and the layer-6 validator, so an
    out-of-contract pair fails closed everywhere before its digest is accepted.
    """
    if departure < _FROZEN_V4_TRAVEL_WINDOW.earliest_departure:
        return (
            f"departure {departure.isoformat()} before the frozen window's "
            f"earliest {_FROZEN_V4_TRAVEL_WINDOW.earliest_departure.isoformat()}"
        )
    if departure > _FROZEN_V4_TRAVEL_WINDOW.latest_departure:
        return (
            f"departure {departure.isoformat()} after the frozen window's "
            f"latest {_FROZEN_V4_TRAVEL_WINDOW.latest_departure.isoformat()}"
        )
    if return_date <= departure:
        return (
            f"return {return_date.isoformat()} is not after departure "
            f"{departure.isoformat()}"
        )
    nights = (return_date - departure).days
    if nights < _FROZEN_V4_TRAVEL_WINDOW.min_nights:
        return (
            f"{nights} nights below the frozen scenario's minimum "
            f"{_FROZEN_V4_TRAVEL_WINDOW.min_nights}"
        )
    if nights > _FROZEN_V4_TRAVEL_WINDOW.max_nights:
        return (
            f"{nights} nights above the frozen scenario's maximum "
            f"{_FROZEN_V4_TRAVEL_WINDOW.max_nights}"
        )
    return None


def frozen_v4_pair_id_dates_canonical(departure: date, return_date: date) -> bool:
    """True only when the pair satisfies the canonical time contract."""
    return frozen_v4_pair_id_contract_violation(departure, return_date) is None


def frozen_v4_pair_id(departure: date, return_date: date) -> str:
    """Generate a canonical frozen-scenario ``date-pair:`` id.

    The canonical generation entry point: it enforces the time contract BEFORE
    the digest is computed, raising ``ValueError`` with the violation reason for
    any out-of-contract pair (C-122 supervision 18:13 — the contract must be
    enforced before digest generation, not only at acceptance).
    """
    violation = frozen_v4_pair_id_contract_violation(departure, return_date)
    if violation is not None:
        raise ValueError(
            f"not a canonical frozen-scenario pair: {violation}"
        )
    return (
        f"date-pair:{departure.isoformat()}:{return_date.isoformat()}:"
        f"{frozen_v4_pair_id_digest(departure, return_date)}"
    )


def frozen_v4_pair_id_is_canonical(pair_id: object) -> bool:
    """True only for a well-formed frozen-scenario ``date-pair:`` id.

    The producer (``agents/live_done_gate_v4.py``), the compact
    (``scripts/run_product_done_gate.py``) and the layer-6 validator all derive
    pair-id validity from this single function, so an id that is not a canonical
    frozen-scenario id fails closed everywhere.  ``pair-1`` and every other
    arbitrary string, plus any well-formed id whose digest does not recompute
    from the frozen constants, are rejected.

    C-122 supervision 18:13: the canonical TIME CONTRACT is part of validity —
    a well-formed id with a recomputing digest but an out-of-contract window
    (2030 departure, reversed dates, or a 1/9/10-night stay) is REJECTED the
    same way a foreign digest is.  This is the single authoritative contract
    shared by the producer, the compact and the layer-6 validator, enforced at
    acceptance for any id (before the digest it claims is ever trusted).
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
    if not frozen_v4_pair_id_dates_canonical(departure, return_date):
        return False
    return match.group(3) == frozen_v4_pair_id_digest(departure, return_date)


# The frozen live-v4 scenario's committed ``reference_date``
# (``benchmarks/scenarios/live-hgh-mle-aug-2026-v4.json``).  The canonical
# ordered trio is derived from THIS date (not a self-declared run-time value),
# so producer, compact and consumer each recompute the identical trio from
# committed inputs alone.
FROZEN_V4_REFERENCE_DATE = date(2026, 7, 30)


@lru_cache(maxsize=8)
def frozen_v4_canonical_pair_ids(
    reference_date: date | None = None,
) -> tuple[str, str, str]:
    """The canonical ORDERED trio of frozen pair ids the scenario must seal.

    R44 (canonical pair-set authority): the exact trio a frozen run seals is NOT
    a self-declared run-time constant.  This replays the production date
    selection — the SAME ``FlexibleDateExplorer.explore`` +
    ``RankedTopKDateRefiner`` chain ``FlexibleLiveAgentSystem.run`` uses, over
    the frozen window with the run-time ``minimum_departure_lead_days=7`` applied
    to the committed ``FROZEN_V4_REFERENCE_DATE`` — so producer, compact and
    consumer each independently recompute the identical ordered trio from
    committed inputs alone, and a joint self-consistent replacement, a wrong
    order, a missing/extra pair or any other individually-valid foreign set fails
    closed against the item-by-item comparison.

    The default reference date reproduces the exact trio the frozen request
    seals; a caller may pass a different reference date only to replay what the
    same run would have sealed at that time (mirror of the producer's run clock).
    """
    reference = reference_date or FROZEN_V4_REFERENCE_DATE
    frozen = _FROZEN_V4_TRAVEL_WINDOW
    minimum_departure = reference + timedelta(days=7)
    effective_earliest = max(frozen.earliest_departure, minimum_departure)
    effective_window = frozen.model_copy(
        update={"earliest_departure": effective_earliest}
    )
    coarse_pair_budget = min(effective_window.universe_size, 400)
    exploration_window = effective_window.model_copy(
        update={"max_pairs": coarse_pair_budget}
    )
    exploration = FlexibleDateExplorer(platforms=LIVE_V5_PLATFORMS).explore(
        exploration_window,
        (),
        now=datetime(
            reference.year, reference.month, reference.day, tzinfo=UTC
        ),
    )
    refiner = RankedTopKDateRefiner()
    observations: list[ExactDatePairObservation] = []
    trio: list[str] = []
    for _round in range(FROZEN_V4_PAIR_COUNT):
        decision = refiner.next_pair(
            exploration.candidates,
            tuple(observations),
            exact_pair_budget=FROZEN_V4_PAIR_COUNT,
        )
        if decision.selected_pair_id is None:
            break
        trio.append(decision.selected_pair_id)
        observations.append(
            ExactDatePairObservation(
                date_pair_id=decision.selected_pair_id,
                total_budget_cents=None,
                recommendable=False,
            )
        )
    if len(trio) != FROZEN_V4_PAIR_COUNT:
        raise RuntimeError(
            "frozen canonical pair-set derivation produced "
            f"{len(trio)}/{FROZEN_V4_PAIR_COUNT} pairs"
        )
    return tuple(trio)


# A real ``FlexibleQueryTask.id``: ``query:<platform>:<kind>:<16-hex>`` — the
# digest is 16 hex chars of ``sha256(pair_id|platform|kind|start_date|end_date|
# zone|stay_plan_id)`` (``FlexibleQueryPlanBuilder._task``), so the id embeds
# the pair's departure/return dates, zone and stay-plan identity.
_QUERY_TASK_ID_RE = re.compile(
    r"^query:([a-z0-9_]+):([a-z0-9_]+):([0-9a-f]{16})$"
)


def frozen_v4_query_task_id_is_wellformed(task_id: object) -> bool:
    """True only for a real frozen-scenario ``FlexibleQueryTask.id``.

    The real producer / checkpoint seals FULL ``query:<platform>:<kind>:<digest>``
    task ids (the ``FlexibleQueryTask.id`` namespace) — a bare ownership id with
    no digest (``query:ctrip:flight``), an arbitrary suffix that is not the
    16-hex digest, a foreign owner outside the enabled platform capabilities, or
    a non-string member is NOT a real frozen query task id and must fail closed.
    """
    if not isinstance(task_id, str):
        return False
    match = _QUERY_TASK_ID_RE.fullmatch(task_id)
    if match is None:
        return False
    return f"{match.group(1)}:{match.group(2)}" in frozen_v4_query_shapes()


def frozen_v4_query_task_id(
    pair_id: str,
    platform: object,
    kind: object,
    start_date: date,
    end_date: date,
    zone: object,
    stay_plan_id: object,
) -> str:
    """Recompute a real ``FlexibleQueryTask.id`` from its production fields.

    Mirrors ``FlexibleQueryPlanBuilder._task`` (``planning/flexible_dates.py``):
    the digest is ``sha256(pair_id|platform|kind|start_date|end_date|zone|stay_
    plan_id)[:16]``.  Any id whose digest does not recompute from these fields —
    a wrong pair id / date / zone / stay-plan — is not the frozen scenario's
    query task for that pair, regardless of how well-formed the prefix looks.
    """
    raw = (
        f"{pair_id}|{getattr(platform, 'value', platform)}|"
        f"{getattr(kind, 'value', kind)}|"
        f"{start_date.isoformat()}|{end_date.isoformat()}|{zone or '-'}|"
        f"{stay_plan_id or '-'}"
    )
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return (
        f"query:{getattr(platform, 'value', platform)}:"
        f"{getattr(kind, 'value', kind)}:{digest}"
    )


@lru_cache(maxsize=1)
def frozen_v4_per_pair_query_task_ids() -> tuple[dict[str, frozenset[str]], ...]:
    """The canonical EXACT per-pair FULL query-task id sets, in trio order.

    C-round2 (04:05Z 增量打回): comparing the checkpoint binding's query-task
    OWNERSHIP projection is not enough — a no-digest id (``query:ctrip:flight``),
    an arbitrary suffix, a same-owner duplicate digest, or a whole cross-pair
    swap of full id sets can still look canonical.  This derives, for each of
    the three canonical frozen pairs, the EXACT set of ``query:<platform>:<kind>:
    <digest>`` task ids the real producer seals — using the SAME production
    builder (``FlexibleQueryPlanBuilder``), the SAME frozen window and the SAME
    frozen stay-plan candidate set.  The digest binds the pair id, departure/
    return dates, zone and stay-plan id, so a binding whose ids carry the wrong
    pair's dates/zone/stay-plan, a missing/extra/wrong digest, a bare ownership
    id, or a whole cross-pair swap fails closed at the consumer against this
    item-by-item authority.
    """
    window = _FROZEN_V4_TRAVEL_WINDOW
    candidate_set = _frozen_candidate_set()
    builder = FlexibleQueryPlanBuilder(platforms=LIVE_V5_PLATFORMS)
    per_pair: list[dict[str, frozenset[str]]] = []
    for index, pair_id in enumerate(frozen_v4_canonical_pair_ids()):
        departure_s, return_s = pair_id.split(":")[1], pair_id.split(":")[2]
        departure = date.fromisoformat(departure_s)
        return_d = date.fromisoformat(return_s)
        pair = AuditableDatePair(
            id=pair_id,
            rank=index + 1,
            departure_date=departure,
            return_date=return_d,
            night_count=(return_d - departure).days,
            source=DatePairSource.FUSED_FARE_HINT,
            audit_reason="frozen canonical pair query-task authority",
        )
        task_ids: set[str] = set()
        for platform in LIVE_V5_PLATFORMS:
            for kind, start_date, end_date, zone in builder._task_windows(
                pair, True, candidate_set
            ):
                if kind not in LIVE_V5_PLATFORM_QUERY_KINDS.get(
                    platform, frozenset(QueryTaskKind)
                ):
                    continue
                stay_plan_id = builder._stay_plan_id(kind, candidate_set)
                task = builder._task(
                    window,
                    pair,
                    platform,
                    kind,
                    start_date,
                    end_date,
                    zone,
                    stay_plan_id,
                    0,
                )
                task_ids.add(task.id)
        per_pair.append({pair_id: frozenset(task_ids)})
    return tuple(per_pair)


def frozen_v4_window_for_run(
    window: object, stay_plan_candidate_set: object
) -> FlexibleTravelWindow | None:
    """The canonical frozen window when the run is the frozen gateway scenario.

    The scenario's requirement text ("玩5-8天") is interpreted by the requirement
    agent as a 4-7-night window, so the API would otherwise explore a NON-frozen
    window and seal generic (non-frozen) pair ids.  When the client EXPLICITLY
    supplies the system-frozen candidate set AND the interpretation still carries
    the frozen city identity, this returns the frozen window so the run seals the
    canonical frozen trio; otherwise ``None`` (the run keeps its own window).
    """
    frozen = _FROZEN_V4_TRAVEL_WINDOW
    if stay_plan_candidate_set is None:
        return None
    if stay_plan_candidate_set != _frozen_candidate_set():
        return None
    if not isinstance(window, FlexibleTravelWindow):
        return None
    if (
        window.origin != frozen.origin
        or window.destination != frozen.destination
        or window.origin_code != frozen.origin_code
        or window.destination_code != frozen.destination_code
    ):
        return None
    return frozen
