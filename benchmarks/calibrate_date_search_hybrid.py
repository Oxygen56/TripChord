from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

from tripchord.planning.adaptive_dates import (
    AdaptiveDateRefiner,
    ExactDatePairObservation,
    evaluate_search_quality,
)
from tripchord.planning.flexible_dates import AuditableDatePair

from benchmarks.evaluate_date_search import (
    BUDGETS,
    RELEVANT_K,
    QueryOnlyOracle,
    SelectionRun,
    _candidate_views,
    _canonical_bytes,
    _load_scenarios,
    run_selection,
)

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_SCENARIOS = (
    ROOT / "benchmarks" / "scenarios" / "date-search-calibration-v1.jsonl"
)
DEFAULT_MANIFEST = ROOT / "benchmarks" / "manifests" / "date-search-hybrid-v2.json"
FROZEN_CALIBRATION_SHA256 = (
    "b7d8dfde0c4995fcd1b451e65e5eaec6adaa68475a25e41c1511898c23e59b95"
)
POLICY_VERSION = "coverage-guarded-hybrid-v2"

# The minimum budget and exploitation prefix are engineering guards, not grid-searched:
# the current refiner needs three observations before its calibrated early-stop logic can run,
# and a budget below five leaves no meaningful room for both exploitation and exploration.
MINIMUM_EXPLORATION_BUDGET = 5
COARSE_GUARD_OBSERVATIONS = 3
THRESHOLD_GRID: tuple[Decimal, ...] = tuple(
    Decimal(item) for item in ("0.30", "0.35", "0.40", "0.45", "0.50", "0.55", "0.60", "0.65")
)


@dataclass(frozen=True)
class GuardedHybridConfig:
    policy_version: str
    maximum_mean_platform_coverage_for_exploration: str
    minimum_exploration_budget: int
    coarse_guard_observations: int

    @property
    def coverage_threshold(self) -> Decimal:
        return Decimal(self.maximum_mean_platform_coverage_for_exploration)


@dataclass(frozen=True)
class GuardedSelectionRun:
    selection: SelectionRun
    acquisition_mode: str
    mean_platform_coverage: Decimal


def _next_coarse_pair(
    candidates: tuple[AuditableDatePair, ...], selected: list[str]
) -> str:
    return next(item.id for item in candidates if item.id not in selected)


def run_guarded_selection(
    *,
    records: list[dict[str, Any]],
    budget: int,
    config: GuardedHybridConfig,
) -> tuple[GuardedSelectionRun, QueryOnlyOracle]:
    if budget < 1:
        raise ValueError("budget must be positive")
    candidates = _candidate_views(records)
    mean_coverage = sum(
        (item.platform_coverage for item in candidates),
        start=Decimal(0),
    ) / Decimal(len(candidates))
    exploration_enabled = (
        budget >= config.minimum_exploration_budget
        and mean_coverage <= config.coverage_threshold
    )
    oracle = QueryOnlyOracle(records)
    observations: list[ExactDatePairObservation] = []
    selected: list[str] = []
    stopped_early = False
    refiner = AdaptiveDateRefiner()
    while len(selected) < budget:
        if len(selected) < min(config.coarse_guard_observations, budget):
            pair_id = _next_coarse_pair(candidates, selected)
        elif exploration_enabled:
            decision = refiner.next_pair(
                candidates,
                tuple(observations),
                exact_pair_budget=budget,
            )
            if decision.selected_pair_id is None:
                stopped_early = decision.stopped_early
                break
            pair_id = decision.selected_pair_id
        else:
            pair_id = _next_coarse_pair(candidates, selected)
        selected.append(pair_id)
        observations.append(oracle.query(pair_id))
    oracle.close_selection()
    selection = SelectionRun(
        selected_pair_ids=tuple(selected),
        successful_pair_ids=oracle.successful_query_ids(),
        query_read_pair_ids=oracle.query_reads,
        stopped_early=stopped_early,
    )
    if selection.selected_pair_ids != selection.query_read_pair_ids:
        raise AssertionError("selection trace and exact-query trace diverged")
    return (
        GuardedSelectionRun(
            selection=selection,
            acquisition_mode=(
                "coarse_guard_then_adaptive_exploration"
                if exploration_enabled
                else "coarse_cheapest_guardrail"
            ),
            mean_platform_coverage=mean_coverage,
        ),
        oracle,
    )


def _metrics(
    records: list[dict[str, Any]],
    selection: SelectionRun,
    oracle: QueryOnlyOracle,
) -> dict[str, Any]:
    quality = evaluate_search_quality(
        exact_totals_by_pair=oracle.evaluation_totals(),
        selected_pair_ids=selection.successful_pair_ids,
        relevant_k=RELEVANT_K,
    )
    return {
        "recall_at_k": float(quality.recall_at_k),
        "price_regret_cents": quality.price_regret_cents,
        "failed": quality.selected_best_cents is None,
        "exact_search_coverage": len(selection.selected_pair_ids) / len(records),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    regrets = [
        item["price_regret_cents"]
        for item in rows
        if item["price_regret_cents"] is not None
    ]
    return {
        "scenario_count": len(rows),
        "mean_recall_at_k": mean(item["recall_at_k"] for item in rows),
        "mean_price_regret_cents": mean(regrets) if regrets else None,
        "failure_rate": mean(item["failed"] for item in rows),
        "mean_exact_search_coverage": mean(item["exact_search_coverage"] for item in rows),
    }


def _evaluate_threshold(
    scenarios: list[dict[str, Any]], threshold: Decimal
) -> dict[str, Any]:
    config = GuardedHybridConfig(
        policy_version=POLICY_VERSION,
        maximum_mean_platform_coverage_for_exploration=str(threshold),
        minimum_exploration_budget=MINIMUM_EXPLORATION_BUDGET,
        coarse_guard_observations=COARSE_GUARD_OBSERVATIONS,
    )
    budget_rows: list[dict[str, Any]] = []
    for budget in BUDGETS:
        hybrid_rows: list[dict[str, Any]] = []
        cheap_rows: list[dict[str, Any]] = []
        modes: dict[str, int] = {}
        for scenario in scenarios:
            guarded, oracle = run_guarded_selection(
                records=scenario["records"],
                budget=budget,
                config=config,
            )
            modes[guarded.acquisition_mode] = modes.get(guarded.acquisition_mode, 0) + 1
            hybrid_rows.append(_metrics(scenario["records"], guarded.selection, oracle))
            cheap, cheap_oracle = run_selection(
                records=scenario["records"],
                strategy="coarse_cheapest",
                budget=budget,
            )
            cheap_rows.append(_metrics(scenario["records"], cheap, cheap_oracle))
        hybrid = _aggregate(hybrid_rows)
        cheap = _aggregate(cheap_rows)
        budget_rows.append(
            {
                "budget": budget,
                "hybrid": hybrid,
                "coarse_cheapest": cheap,
                "acquisition_mode_counts": modes,
                "recall_delta": hybrid["mean_recall_at_k"] - cheap["mean_recall_at_k"],
                "price_regret_delta_cents": (
                    hybrid["mean_price_regret_cents"]
                    - cheap["mean_price_regret_cents"]
                ),
                "failure_rate_delta": hybrid["failure_rate"] - cheap["failure_rate"],
            }
        )
    qualifies = all(
        row["recall_delta"] >= 0
        and row["price_regret_delta_cents"] <= 0
        and row["failure_rate_delta"] <= 0
        for row in budget_rows
    )
    score = sum(
        row["recall_delta"] - row["price_regret_delta_cents"] / 1_000_000
        for row in budget_rows
    )
    return {
        "threshold": str(threshold),
        "qualifies": qualifies,
        "calibration_score": score,
        "budget_rows": budget_rows,
    }


def calibrate(path: Path = CALIBRATION_SCENARIOS) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != FROZEN_CALIBRATION_SHA256:
        raise ValueError("frozen calibration fixture hash mismatch")
    scenarios = _load_scenarios(path)
    if not scenarios or any(item["split"] != "calibration" for item in scenarios):
        raise ValueError("calibration input may contain only calibration scenarios")
    candidate_rows = [_evaluate_threshold(scenarios, threshold) for threshold in THRESHOLD_GRID]
    qualified = [item for item in candidate_rows if item["qualifies"]]
    if not qualified:
        selected = None
        status = "rejected_no_non_degrading_candidate"
    else:
        winner = max(
            qualified,
            key=lambda item: (
                item["calibration_score"],
                -Decimal(item["threshold"]),
            ),
        )
        selected = asdict(
            GuardedHybridConfig(
                policy_version=POLICY_VERSION,
                maximum_mean_platform_coverage_for_exploration=winner["threshold"],
                minimum_exploration_budget=MINIMUM_EXPLORATION_BUDGET,
                coarse_guard_observations=COARSE_GUARD_OBSERVATIONS,
            )
        )
        status = "frozen_calibration_candidate"
    payload: dict[str, Any] = {
        "policy_version": POLICY_VERSION,
        "status": status,
        "calibration_input_sha256": digest,
        "calibration_scenario_count": len(scenarios),
        "threshold_grid": [str(item) for item in THRESHOLD_GRID],
        "fixed_engineering_guards": {
            "minimum_exploration_budget": MINIMUM_EXPLORATION_BUDGET,
            "coarse_guard_observations": COARSE_GUARD_OBSERVATIONS,
        },
        "selection_rule": (
            "只在每个预算的 Recall 不降、平均 regret 不升、失败率不升时入围；"
            "在 calibration score 并列时选择更低覆盖阈值，减少探索触发面。"
        ),
        "selected_config": selected,
        "window_contract": {
            "calibration_night_counts": [5, 6, 7, 8],
            "target_user_request_days": [5, 6, 7, 8],
            "target_night_counts": [4, 5, 6, 7],
            "day_to_night_rule": "lodging_nights = calendar_travel_days - 1",
            "generalization_note": (
                "策略参数在一般化 5–8 晚 calibration 上冻结；4–7 晚案例窗口只在"
                "冻结后的 sealed holdout 中评估，不参与参数选择。"
            ),
        },
        "candidate_rows": candidate_rows,
        "test_split_read_during_calibration": False,
        "boundary": (
            "该 manifest 只使用独立 calibration 文件；冻结后不得根据 test/holdout 改参数。"
        ),
    }
    payload["policy_manifest_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=CALIBRATION_SCENARIOS)
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    manifest = calibrate(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
