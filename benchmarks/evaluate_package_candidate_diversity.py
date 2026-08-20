from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageIntent,
    PackageInventory,
    PackagePlaceKey,
    PackagePlanner,
    TransferOption,
    TransferPriceGuarantee,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT / "benchmarks" / "scenarios" / "package-candidate-diversity-v1.json"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "package-candidate-diversity-v1.json"
BENCHMARK_VERSION = "package-candidate-diversity-v1"
FROZEN_INPUT_SHA256 = "158163c146d89e62f0fdc4a0b5f37cc18e966d2ba4e9fd37210c77acc941eb9c"
BOUNDARY = (
    "固定 synthetic 规范化报价只验证 package-candidate-beam-v3 在一个小上限场景中的"
    "确定性候选选择性质；不证明真实 OTA 可订性、平台质量、全量穷举、线上召回或 SLA。"
)

MALDIVES = timezone(timedelta(hours=5))
CHINA = timezone(timedelta(hours=8))
CAPTURED_AT = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 7, 30, 10, 0, tzinfo=UTC)
START_DATE = date(2026, 8, 23)
END_DATE = date(2026, 8, 30)


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def load_scenario(path: Path = SCENARIO) -> dict[str, Any]:
    content = path.read_bytes()
    if path.resolve() == SCENARIO.resolve():
        digest = hashlib.sha256(content).hexdigest()
        if digest != FROZEN_INPUT_SHA256:
            raise ValueError(
                "frozen package candidate diversity fixture hash mismatch; "
                "create a new benchmark version"
            )
    scenario: dict[str, Any] = json.loads(content)
    claimed_digest = scenario.get("scenario_sha256")
    unsigned = {key: value for key, value in scenario.items() if key != "scenario_sha256"}
    actual_digest = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    if claimed_digest != actual_digest:
        raise ValueError("package candidate diversity scenario hash mismatch")
    if scenario.get("benchmark_version") != BENCHMARK_VERSION:
        raise ValueError("package candidate diversity benchmark version mismatch")
    classification = scenario.get("input_classification")
    if classification != {
        "kind": "synthetic_normalized_quotes",
        "live": False,
        "bookability_verified": False,
    }:
        raise ValueError("benchmark inputs must remain explicitly synthetic and non-live")
    return scenario


def _intent() -> PackageIntent:
    return PackageIntent(
        trip_id="fixture-hgh-mle-20260823",
        origin="HGH",
        destination="MLE",
        start_date=START_DATE,
        end_date=END_DATE,
        adults=2,
        rooms=1,
        currency="CNY",
        budget_cents=1_600_000,
        require_checked_baggage=False,
        minimum_arrival_to_boat_minutes=120,
        minimum_airport_buffer_minutes=180,
        maximum_quote_capture_skew_minutes=20,
    )


def _flight(row: dict[str, Any]) -> NormalizedFlightQuote:
    quote_id = str(row["id"])
    return NormalizedFlightQuote(
        id=quote_id,
        provider=str(row["provider"]),
        total_for_party_cents=int(row["total_for_party_cents"]),
        taxes_and_fees_included=True,
        captured_at=CAPTURED_AT,
        expires_at=EXPIRES_AT,
        evidence_refs=(f"synthetic-evidence:{quote_id}",),
        origin="HGH",
        destination="MLE",
        adults=2,
        party_availability_confirmed=True,
        outbound_depart_at=datetime(2026, 8, 23, 8, 30, tzinfo=CHINA),
        outbound_arrive_at=datetime(2026, 8, 23, 18, 35, tzinfo=MALDIVES),
        return_depart_at=datetime(2026, 8, 30, 10, 45, tzinfo=MALDIVES),
        return_arrive_at=datetime(2026, 8, 31, 15, 40, tzinfo=CHINA),
        checked_baggage_per_adult_kg=0,
        provider_itinerary_id=f"synthetic-itinerary:{quote_id}",
    )


def _lodging(
    quote_id: str,
    property_name: str,
    area: PackageArea,
    check_in: date,
    check_out: date,
    total_cents: int,
) -> NormalizedLodgingQuote:
    return NormalizedLodgingQuote(
        id=quote_id,
        provider="fixture-lodging-platform",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=CAPTURED_AT,
        expires_at=EXPIRES_AT,
        evidence_refs=(f"synthetic-evidence:{quote_id}",),
        property_name=property_name,
        area=area,
        check_in=check_in,
        check_out=check_out,
        adults=2,
        rooms=1,
        breakfast_included=True,
        place_key=(
            PackagePlaceKey.MAAFUSHI
            if area == PackageArea.DESTINATION_ISLAND
            else PackagePlaceKey.HULHUMALE
        ),
    )


def _transfer(
    quote_id: str,
    origin: PackageArea,
    destination: PackageArea,
    depart_at: datetime,
    arrive_at: datetime,
    total_cents: int,
) -> TransferOption:
    duration_minutes = int((arrive_at - depart_at).total_seconds() // 60)
    return TransferOption(
        id=quote_id,
        provider="fixture-transfer-platform",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=CAPTURED_AT,
        expires_at=EXPIRES_AT,
        evidence_refs=(f"synthetic-evidence:{quote_id}",),
        origin_area=origin,
        destination_area=destination,
        origin_place_key={
            PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
            PackageArea.AIRPORT_ISLAND: PackagePlaceKey.HULHUMALE,
            PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
        }[origin],
        destination_place_key={
            PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
            PackageArea.AIRPORT_ISLAND: PackagePlaceKey.HULHUMALE,
            PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
        }[destination],
        adults=2,
        service_date=depart_at.date(),
        schedule_mode=TransferScheduleMode.EXACT_DEPARTURE,
        duration_minutes=duration_minutes,
        depart_at=depart_at,
        arrive_at=arrive_at,
        operates_24_hours=False,
        requires_reservation=True,
        price_scope=TransferPriceScope.ONE_WAY,
        price_contract_id=f"synthetic-contract:{quote_id}",
        purchase_scope=TransferPurchaseScope.PUBLIC_INDEPENDENT,
        price_guarantee=TransferPriceGuarantee.ALL_IN_CONFIRMED,
        contract_evidence_text=(
            f"synthetic one-way {origin.value} to {destination.value}; "
            f"all-in CNY {total_cents / 100:.2f}"
        ),
        detail_url="https://example.invalid/tripchord-synthetic-benchmark",
    )


def _inventory(scenario: dict[str, Any]) -> PackageInventory:
    first_checkout = START_DATE + timedelta(days=1)
    last_checkin = END_DATE - timedelta(days=1)
    return PackageInventory(
        flights=tuple(_flight(row) for row in scenario["flights"]),
        lodgings=(
            _lodging(
                "fixture-stay-direct",
                "Synthetic Island Stay",
                PackageArea.DESTINATION_ISLAND,
                START_DATE,
                END_DATE,
                471_100,
            ),
            _lodging(
                "fixture-stay-first",
                "Synthetic Airport Stay",
                PackageArea.AIRPORT_ISLAND,
                START_DATE,
                first_checkout,
                39_600,
            ),
            _lodging(
                "fixture-stay-middle",
                "Synthetic Island Stay",
                PackageArea.DESTINATION_ISLAND,
                first_checkout,
                last_checkin,
                336_500,
            ),
            _lodging(
                "fixture-stay-last",
                "Synthetic Airport Stay",
                PackageArea.AIRPORT_ISLAND,
                last_checkin,
                END_DATE,
                39_600,
            ),
        ),
        transfers=(
            _transfer(
                "fixture-transfer-direct-out",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                datetime(2026, 8, 23, 19, 20, tzinfo=MALDIVES),
                datetime(2026, 8, 23, 20, 5, tzinfo=MALDIVES),
                36_000,
            ),
            _transfer(
                "fixture-transfer-direct-back",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 30, 7, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 30, 8, 15, tzinfo=MALDIVES),
                36_000,
            ),
            _transfer(
                "fixture-transfer-airport-hotel",
                PackageArea.AIRPORT,
                PackageArea.AIRPORT_ISLAND,
                datetime(2026, 8, 23, 19, 20, tzinfo=MALDIVES),
                datetime(2026, 8, 23, 19, 40, tzinfo=MALDIVES),
                10_800,
            ),
            _transfer(
                "fixture-transfer-first-hotel-airport",
                PackageArea.AIRPORT_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 24, 6, 40, tzinfo=MALDIVES),
                datetime(2026, 8, 24, 7, 0, tzinfo=MALDIVES),
                10_800,
            ),
            _transfer(
                "fixture-transfer-airport-island-next-day",
                PackageArea.AIRPORT,
                PackageArea.DESTINATION_ISLAND,
                datetime(2026, 8, 24, 7, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 24, 8, 15, tzinfo=MALDIVES),
                36_000,
            ),
            _transfer(
                "fixture-transfer-island-airport-day-before",
                PackageArea.DESTINATION_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 29, 16, 0, tzinfo=MALDIVES),
                datetime(2026, 8, 29, 16, 45, tzinfo=MALDIVES),
                36_000,
            ),
            _transfer(
                "fixture-transfer-airport-last-hotel",
                PackageArea.AIRPORT,
                PackageArea.AIRPORT_ISLAND,
                datetime(2026, 8, 29, 17, 30, tzinfo=MALDIVES),
                datetime(2026, 8, 29, 17, 50, tzinfo=MALDIVES),
                10_800,
            ),
            _transfer(
                "fixture-transfer-hotel-airport",
                PackageArea.AIRPORT_ISLAND,
                PackageArea.AIRPORT,
                datetime(2026, 8, 30, 6, 50, tzinfo=MALDIVES),
                datetime(2026, 8, 30, 7, 10, tzinfo=MALDIVES),
                10_800,
            ),
        ),
    )


def evaluate(path: Path = SCENARIO) -> dict[str, Any]:
    scenario = load_scenario(path)
    expected: dict[str, int] = scenario["expected"]
    candidate_cap = int(scenario["candidate_cap"])
    intent = _intent()
    inventory = _inventory(scenario)
    planner = PackagePlanner()
    complete = planner.generate_bounded(intent, inventory, candidate_cap=2_000)
    bounded = planner.generate_bounded(intent, inventory, candidate_cap=candidate_cap)
    reordered = planner.generate_bounded(
        intent,
        inventory.model_copy(
            update={
                "flights": tuple(reversed(inventory.flights)),
                "lodgings": tuple(reversed(inventory.lodgings)),
                "transfers": tuple(reversed(inventory.transfers)),
            }
        ),
        candidate_cap=candidate_cap,
    )

    bounded_ids = tuple(candidate.id for candidate in bounded.candidates)
    complete_ids = tuple(candidate.id for candidate in complete.candidates)
    providers = sorted({candidate.flight.provider for candidate in bounded.candidates})
    flights = sorted({candidate.flight.id for candidate in bounded.candidates})
    kinds = sorted({candidate.kind.value for candidate in bounded.candidates})
    audit = bounded.audit
    checks = {
        "global_best_retained": bool(bounded_ids) and bounded_ids[0] == complete_ids[0],
        "bounded_candidates_are_from_complete_pool": set(bounded_ids) <= set(complete_ids),
        "provider_coverage_met": len(providers) == expected["provider_count"],
        "flight_coverage_met": len(flights) == expected["flight_count"],
        "package_kind_coverage_met": len(kinds) == expected["package_kind_count"],
        "input_reordering_stable": (
            bounded_ids == reordered.audit.generated_candidate_ids
            and audit.generation_proof_sha256 == reordered.audit.generation_proof_sha256
        ),
        "candidate_cap_respected": (
            len(bounded_ids) == expected["bounded_candidate_count"] == candidate_cap
            and audit.generated_candidate_count <= audit.generation_candidate_cap
        ),
        "small_cap_truncation_exercised": (
            audit.generation_stopped_at_cap
            and audit.structurally_joined_candidate_count
            == expected["full_candidate_count"]
            and audit.structurally_joined_candidate_count > candidate_cap
        ),
        "structural_upper_bound_respected": (
            len(complete_ids) == expected["full_candidate_count"]
            and audit.raw_structural_candidate_upper_bound
            == expected["raw_structural_candidate_upper_bound"]
            and audit.prescreened_structural_candidate_upper_bound
            == expected["prescreened_structural_candidate_upper_bound"]
            and audit.structurally_joined_candidate_count
            <= audit.prescreened_structural_candidate_upper_bound
            <= audit.raw_structural_candidate_upper_bound
        ),
        "bounded_structure_scan_completed": audit.prescreen_structure_scan_completed,
        "audited_ids_match_output": audit.generated_candidate_ids == bounded_ids,
        "non_exhaustive_boundary_preserved": (
            not audit.full_enumeration_claimed
            and not audit.transfer_combinations_exhaustively_enumerated
        ),
    }
    passed = all(checks.values())
    payload: dict[str, Any] = {
        "benchmark_version": BENCHMARK_VERSION,
        "scenario_id": scenario["scenario_id"],
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "scenario_sha256": scenario["scenario_sha256"],
        "input_classification": scenario["input_classification"],
        "boundary": BOUNDARY,
        "policy_version": audit.policy_version,
        "selection_policy_version": audit.selection_policy_version,
        "candidate_cap": candidate_cap,
        "complete_pool": {
            "candidate_count": len(complete_ids),
            "globally_best_candidate_id": complete_ids[0],
            "candidate_ids": list(complete_ids),
        },
        "bounded_pool": {
            "candidate_count": len(bounded_ids),
            "candidate_ids": list(bounded_ids),
            "flight_providers": providers,
            "flight_ids": flights,
            "package_kinds": kinds,
        },
        "audit": {
            "raw_structural_candidate_upper_bound": (
                audit.raw_structural_candidate_upper_bound
            ),
            "prescreened_structural_candidate_upper_bound": (
                audit.prescreened_structural_candidate_upper_bound
            ),
            "structurally_joined_candidate_count": (
                audit.structurally_joined_candidate_count
            ),
            "generated_candidate_count": audit.generated_candidate_count,
            "generation_candidate_cap": audit.generation_candidate_cap,
            "generation_stopped_at_cap": audit.generation_stopped_at_cap,
            "prescreen_structure_scan_completed": (
                audit.prescreen_structure_scan_completed
            ),
            "input_prescreen_pruned": audit.input_prescreen_pruned,
            "generation_proof_sha256": audit.generation_proof_sha256,
        },
        "reordered_input": {
            "candidate_ids": list(reordered.audit.generated_candidate_ids),
            "generation_proof_sha256": reordered.audit.generation_proof_sha256,
        },
        "checks": checks,
        "passed": passed,
        "claim_boundary": {
            "small_cap_selection_claim_allowed": passed,
            "live_ota_quality_claim_allowed": False,
            "bookability_claim_allowed": False,
            "platform_superiority_claim_allowed": False,
            "exhaustive_search_claim_allowed": False,
        },
    }
    payload["result_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def write_result(result: dict[str, Any], path: Path = DEFAULT_OUTPUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=SCENARIO)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = evaluate(args.input)
    write_result(result, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
