from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Any

from benchmarks.calibrate_date_search_hybrid import (
    GuardedHybridConfig,
    _metrics,
    run_guarded_selection,
)
from benchmarks.evaluate_date_search import (
    BUDGETS,
    SCENARIOS,
    _canonical_bytes,
    _load_scenarios,
    run_selection,
)
from benchmarks.generate_date_search_holdout import (
    DEFAULT_OUTPUT as SEALED_HOLDOUT,
)
from benchmarks.generate_date_search_holdout import (
    FROZEN_POLICY_MANIFEST_FILE_SHA256,
    POLICY_MANIFEST,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "date-search-hybrid-v2.json"
FROZEN_SEALED_HOLDOUT_SHA256 = (
    "15e16c9887da24a199425da1a0b8a271a46d2da0f22cfb5eebf9140e80e1f39f"
)
MATERIAL_RECALL_DELTA = 0.01
MATERIAL_REGRET_REDUCTION_RATIO = 0.03


def _load_policy() -> tuple[GuardedHybridConfig, dict[str, Any]]:
    content = POLICY_MANIFEST.read_bytes()
    if hashlib.sha256(content).hexdigest() != FROZEN_POLICY_MANIFEST_FILE_SHA256:
        raise ValueError("frozen policy manifest file changed")
    manifest: dict[str, Any] = json.loads(content)
    claimed = manifest["policy_manifest_sha256"]
    unsigned = {key: value for key, value in manifest.items() if key != "policy_manifest_sha256"}
    if hashlib.sha256(_canonical_bytes(unsigned)).hexdigest() != claimed:
        raise ValueError("frozen policy manifest signature mismatch")
    return GuardedHybridConfig(**manifest["selected_config"]), manifest


def _load_sealed_holdout() -> list[dict[str, Any]]:
    if hashlib.sha256(SEALED_HOLDOUT.read_bytes()).hexdigest() != FROZEN_SEALED_HOLDOUT_SHA256:
        raise ValueError("sealed holdout file changed")
    scenarios = _load_scenarios(SEALED_HOLDOUT)
    for scenario in scenarios:
        contract = scenario["universe_contract"]
        if (
            scenario["split"] != "sealed_holdout"
            or contract["night_counts"] != [4, 5, 6, 7]
            or contract["source_request_day_range"] != [5, 8]
            or contract["day_to_night_rule"]
            != "n calendar travel days map to n-1 lodging nights"
        ):
            raise ValueError("sealed holdout has the wrong day-to-night contract")
    return scenarios


def _strategy_metrics(
    scenario: dict[str, Any],
    *,
    budget: int,
    strategy: str,
    config: GuardedHybridConfig,
) -> tuple[dict[str, Any], str]:
    if strategy == "guarded_hybrid":
        guarded, oracle = run_guarded_selection(
            records=scenario["records"],
            budget=budget,
            config=config,
        )
        return _metrics(scenario["records"], guarded.selection, oracle), guarded.acquisition_mode
    selection, oracle = run_selection(
        records=scenario["records"],
        strategy=strategy,
        budget=budget,
    )
    return _metrics(scenario["records"], selection, oracle), strategy


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    regrets = [
        item["price_regret_cents"]
        for item in rows
        if item["price_regret_cents"] is not None
    ]
    return {
        "scenario_count": len(rows),
        "regret_evaluable_scenario_count": len(regrets),
        "mean_recall_at_k": mean(item["recall_at_k"] for item in rows),
        "mean_price_regret_cents": mean(regrets) if regrets else None,
        "failure_rate": mean(item["failed"] for item in rows),
        "mean_exact_search_coverage": mean(item["exact_search_coverage"] for item in rows),
    }


def _evaluate_set(
    name: str,
    scenarios: list[dict[str, Any]],
    config: GuardedHybridConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scenario_rows: list[dict[str, Any]] = []
    strategies = ("guarded_hybrid", "adaptive", "coarse_cheapest")
    for scenario in scenarios:
        for budget in BUDGETS:
            for strategy in strategies:
                metrics, acquisition_mode = _strategy_metrics(
                    scenario,
                    budget=budget,
                    strategy=strategy,
                    config=config,
                )
                scenario_rows.append(
                    {
                        "evaluation_set": name,
                        "scenario_id": scenario["id"],
                        "condition": scenario["condition"]["id"],
                        "budget": budget,
                        "strategy": strategy,
                        "acquisition_mode": acquisition_mode,
                        **metrics,
                    }
                )
    aggregates: list[dict[str, Any]] = []
    for budget in BUDGETS:
        for strategy in strategies:
            selected = [
                item
                for item in scenario_rows
                if item["budget"] == budget and item["strategy"] == strategy
            ]
            aggregates.append(
                {
                    "evaluation_set": name,
                    "budget": budget,
                    "strategy": strategy,
                    **_aggregate(selected),
                }
            )
    return scenario_rows, aggregates


def _comparison_rows(
    evaluation_set: str,
    aggregate_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for budget in BUDGETS:
        by_strategy = {
            item["strategy"]: item
            for item in aggregate_rows
            if item["evaluation_set"] == evaluation_set and item["budget"] == budget
        }
        hybrid = by_strategy["guarded_hybrid"]
        for baseline_name in ("coarse_cheapest", "adaptive"):
            baseline = by_strategy[baseline_name]
            baseline_regret = baseline["mean_price_regret_cents"]
            hybrid_regret = hybrid["mean_price_regret_cents"]
            regret_delta = hybrid_regret - baseline_regret
            regret_reduction_ratio = (
                -regret_delta / baseline_regret if baseline_regret else 0.0
            )
            comparisons.append(
                {
                    "evaluation_set": evaluation_set,
                    "budget": budget,
                    "baseline": baseline_name,
                    "recall_delta": (
                        hybrid["mean_recall_at_k"] - baseline["mean_recall_at_k"]
                    ),
                    "price_regret_delta_cents": regret_delta,
                    "price_regret_reduction_ratio": regret_reduction_ratio,
                    "failure_rate_delta": hybrid["failure_rate"] - baseline["failure_rate"],
                }
            )
    return comparisons


def _sealed_acceptance(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    cheap_rows = [
        item
        for item in comparisons
        if item["evaluation_set"] == "sealed_holdout_4_to_7_nights"
        and item["baseline"] == "coarse_cheapest"
    ]
    non_degrading = all(
        item["recall_delta"] >= 0
        and item["price_regret_delta_cents"] <= 0
        and item["failure_rate_delta"] <= 0
        for item in cheap_rows
    )
    material_budgets = {
        item["budget"]
        for item in cheap_rows
        if item["budget"] >= 5
        and (
            item["recall_delta"] >= MATERIAL_RECALL_DELTA
            or item["price_regret_reduction_ratio"] >= MATERIAL_REGRET_REDUCTION_RATIO
        )
    }
    accepted = non_degrading and material_budgets == {5, 8}
    return {
        "accepted_as_planning_candidate": accepted,
        "non_degrading_vs_coarse_topk_at_all_budgets": non_degrading,
        "materially_improved_budgets": sorted(material_budgets),
        "required_materially_improved_budgets": [5, 8],
        "material_recall_delta": MATERIAL_RECALL_DELTA,
        "material_regret_reduction_ratio": MATERIAL_REGRET_REDUCTION_RATIO,
        "live_default_change_allowed": False,
        "reason": (
            "只有新 sealed holdout 对粗价 Top-K 在全部预算不退化，且预算 5/8 均达到"
            "预先冻结的 material 门槛，才允许进入 planning 层成为非默认候选。"
        ),
    }


def evaluate() -> dict[str, Any]:
    config, manifest = _load_policy()
    sealed = _load_sealed_holdout()
    # Existing test was inspected before this policy task and is explicitly regression-only.
    existing = [item for item in _load_scenarios(SCENARIOS) if item["split"] == "test"]
    sealed_rows, sealed_aggregates = _evaluate_set(
        "sealed_holdout_4_to_7_nights",
        sealed,
        config,
    )
    existing_rows, existing_aggregates = _evaluate_set(
        "existing_v1_test_previously_inspected",
        existing,
        config,
    )
    aggregate_rows = sealed_aggregates + existing_aggregates
    comparisons = _comparison_rows(
        "sealed_holdout_4_to_7_nights", aggregate_rows
    ) + _comparison_rows("existing_v1_test_previously_inspected", aggregate_rows)
    payload: dict[str, Any] = {
        "evaluation_version": "date-search-hybrid-v2",
        "policy_version": config.policy_version,
        "policy_manifest_sha256": manifest["policy_manifest_sha256"],
        "sealed_holdout_input_sha256": FROZEN_SEALED_HOLDOUT_SHA256,
        "evidence_status": {
            "sealed_holdout_4_to_7_nights": "post-policy-freeze one-time evaluation",
            "existing_v1_test_previously_inspected": (
                "contaminated regression only; cannot be called blind or held-out"
            ),
        },
        "day_to_night_contract": manifest["window_contract"],
        "acceptance": _sealed_acceptance(comparisons),
        "synthetic_only": True,
        "real_ota_quality_claim_allowed": False,
        "aggregate_rows": aggregate_rows,
        "comparison_rows": comparisons,
        "scenario_rows": sealed_rows + existing_rows,
    }
    payload["result_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    printable = {key: value for key, value in result.items() if key != "scenario_rows"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
