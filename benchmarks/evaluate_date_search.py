from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from statistics import mean
from typing import Any

from tripchord.planning.adaptive_dates import (
    AdaptiveDateRefiner,
    ExactDatePairObservation,
    evaluate_search_quality,
)
from tripchord.planning.flexible_dates import AuditableDatePair, DatePairSource

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "benchmarks" / "scenarios" / "date-search-full-universe-v1.jsonl"
DEFAULT_OUTPUT = ROOT / "benchmarks" / "results" / "date-search-full-universe-v1.json"
BUDGETS: tuple[int, ...] = (3, 5, 8)
RELEVANT_K = 3
STRATEGIES: tuple[str, ...] = (
    "adaptive",
    "coarse_cheapest",
    "fixed_stratified",
    "chronological_first_k",
)
BOUNDARY = (
    "冻结 synthetic 全日期宇宙的离线检索基准；只衡量受控生成分布，"
    "不是任何真实 OTA 的全月最低价、库存可订性、线上质量或 SLA 证据。"
    "v1 枚举的是一般化 5–8 晚，不是用户‘玩 5–8 天’按 days-1 换算后的 4–7 晚窗口。"
)
# Any change requires a new benchmark version and an explicit fixture regeneration.
FROZEN_INPUT_SHA256 = "305e815dc6e847d0eedecd69ad47408d729e2c4e1962ca622907f105148abe3a"


@dataclass(frozen=True)
class SelectionRun:
    selected_pair_ids: tuple[str, ...]
    successful_pair_ids: tuple[str, ...]
    query_read_pair_ids: tuple[str, ...]
    stopped_early: bool


class QueryOnlyOracle:
    """Exposes one queried observation at a time; full truth is evaluation-only."""

    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        self._exact = {item["id"]: item["exact_total_cents"] for item in records}
        self._query_reads: list[str] = []
        self._observations: list[ExactDatePairObservation] = []
        self._selection_closed = False

    @property
    def query_reads(self) -> tuple[str, ...]:
        return tuple(self._query_reads)

    def query(self, pair_id: str) -> ExactDatePairObservation:
        if self._selection_closed:
            raise RuntimeError("selection-time oracle is closed")
        if pair_id not in self._exact:
            raise KeyError(pair_id)
        if pair_id in self._query_reads:
            raise ValueError(f"duplicate exact query: {pair_id}")
        self._query_reads.append(pair_id)
        total = self._exact[pair_id]
        observation = ExactDatePairObservation(
            date_pair_id=pair_id,
            total_budget_cents=total,
            recommendable=total is not None,
        )
        self._observations.append(observation)
        return observation

    def close_selection(self) -> None:
        self._selection_closed = True

    def successful_query_ids(self) -> tuple[str, ...]:
        if not self._selection_closed:
            raise RuntimeError("successful query ids are available only after selection closes")
        return tuple(item.date_pair_id for item in self._observations if item.recommendable)

    def evaluation_totals(self) -> dict[str, int]:
        if not self._selection_closed:
            raise RuntimeError("full oracle may be read only after selection is closed")
        return {pair_id: total for pair_id, total in self._exact.items() if total is not None}


def _load_scenarios(path: Path = SCENARIOS) -> list[dict[str, Any]]:
    content = path.read_bytes()
    if path.resolve() == SCENARIOS.resolve():
        digest = hashlib.sha256(content).hexdigest()
        if digest != FROZEN_INPUT_SHA256:
            raise ValueError(
                "frozen date-search fixture hash mismatch; create a new benchmark version"
            )
    scenarios = [json.loads(line) for line in content.decode().splitlines() if line.strip()]
    for scenario in scenarios:
        claimed_digest = scenario.get("scenario_sha256")
        unsigned = {key: value for key, value in scenario.items() if key != "scenario_sha256"}
        actual_digest = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
        if claimed_digest != actual_digest:
            raise ValueError(f"scenario hash mismatch: {scenario.get('id', '<unknown>')}")
        ids = [item["id"] for item in scenario["records"]]
        if len(ids) != 124 or len(set(ids)) != 124:
            raise ValueError(f"scenario must contain 124 unique pairs: {scenario['id']}")
    return scenarios


def _candidate_views(records: Sequence[dict[str, Any]]) -> tuple[AuditableDatePair, ...]:
    views = tuple(
        AuditableDatePair(
            id=item["id"],
            rank=1,
            departure_date=date.fromisoformat(item["departure_date"]),
            return_date=date.fromisoformat(item["return_date"]),
            night_count=item["night_count"],
            source=(
                DatePairSource.FUSED_FARE_HINT
                if item["coarse_total_cents"] is not None
                else DatePairSource.STRATIFIED_SAMPLE
            ),
            platform_coverage=Decimal(item["platform_coverage_count"]) / Decimal(3),
            median_total_for_party_cents=item["coarse_total_cents"],
            audit_reason="synthetic coarse prior; exact oracle intentionally excluded",
        )
        for item in records
    )
    ranked = sorted(
        views,
        key=lambda item: (
            item.median_total_for_party_cents is None,
            item.median_total_for_party_cents or 0,
            item.departure_date,
            item.night_count,
            item.id,
        ),
    )
    return tuple(
        item.model_copy(update={"rank": index}) for index, item in enumerate(ranked, start=1)
    )


def _adaptive_ids(
    candidates: tuple[AuditableDatePair, ...],
    oracle: QueryOnlyOracle,
    budget: int,
) -> tuple[tuple[str, ...], bool]:
    refiner = AdaptiveDateRefiner()
    observations: list[ExactDatePairObservation] = []
    selected: list[str] = []
    stopped_early = False
    while len(selected) < budget:
        decision = refiner.next_pair(
            candidates,
            tuple(observations),
            exact_pair_budget=budget,
        )
        if decision.selected_pair_id is None:
            stopped_early = decision.stopped_early
            break
        selected.append(decision.selected_pair_id)
        observations.append(oracle.query(decision.selected_pair_id))
    return tuple(selected), stopped_early


def _coarse_cheapest_ids(
    candidates: tuple[AuditableDatePair, ...], budget: int
) -> tuple[str, ...]:
    return tuple(item.id for item in candidates[:budget])


def _chronological_ids(
    candidates: tuple[AuditableDatePair, ...], budget: int
) -> tuple[str, ...]:
    ordered = sorted(
        candidates,
        key=lambda item: (item.departure_date, item.night_count, item.id),
    )
    return tuple(item.id for item in ordered[:budget])


def _fixed_stratified_ids(
    candidates: tuple[AuditableDatePair, ...], budget: int
) -> tuple[str, ...]:
    ordered = sorted(
        candidates,
        key=lambda item: (item.departure_date, item.night_count, item.id),
    )
    count = len(ordered)
    # One fixed midpoint per equally sized chronological stratum. No price or oracle input.
    indexes = tuple(
        min(count - 1, ((2 * stratum + 1) * count) // (2 * budget))
        for stratum in range(budget)
    )
    return tuple(ordered[index].id for index in indexes)


STATIC_SELECTORS: dict[
    str, Callable[[tuple[AuditableDatePair, ...], int], tuple[str, ...]]
] = {
    "coarse_cheapest": _coarse_cheapest_ids,
    "fixed_stratified": _fixed_stratified_ids,
    "chronological_first_k": _chronological_ids,
}


def run_selection(
    *,
    records: Sequence[dict[str, Any]],
    strategy: str,
    budget: int,
) -> tuple[SelectionRun, QueryOnlyOracle]:
    if strategy not in STRATEGIES:
        raise ValueError(f"unknown strategy: {strategy}")
    candidates = _candidate_views(records)
    oracle = QueryOnlyOracle(records)
    if strategy == "adaptive":
        selected, stopped_early = _adaptive_ids(candidates, oracle, budget)
    else:
        selected = STATIC_SELECTORS[strategy](candidates, budget)
        stopped_early = False
        for pair_id in selected:
            oracle.query(pair_id)
    oracle.close_selection()
    run = SelectionRun(
        selected_pair_ids=selected,
        successful_pair_ids=oracle.successful_query_ids(),
        query_read_pair_ids=oracle.query_reads,
        stopped_early=stopped_early,
    )
    if run.selected_pair_ids != run.query_read_pair_ids:
        raise AssertionError("selection trace and exact-query trace diverged")
    return run, oracle


def _scenario_row(
    scenario: dict[str, Any], strategy: str, budget: int
) -> dict[str, Any]:
    run, oracle = run_selection(records=scenario["records"], strategy=strategy, budget=budget)
    exact_totals = oracle.evaluation_totals()
    evaluation = evaluate_search_quality(
        exact_totals_by_pair=exact_totals,
        selected_pair_ids=run.successful_pair_ids,
        relevant_k=RELEVANT_K,
    )
    return {
        "scenario_id": scenario["id"],
        "condition": scenario["condition"]["id"],
        "strategy": strategy,
        "budget": budget,
        "query_count": len(run.selected_pair_ids),
        "exact_search_coverage": len(run.selected_pair_ids) / len(scenario["records"]),
        "successful_quote_count": len(run.successful_pair_ids),
        "successful_oracle_coverage": float(evaluation.coverage),
        "recall_at_k": float(evaluation.recall_at_k),
        "relevant_k": RELEVANT_K,
        "price_regret_cents": evaluation.price_regret_cents,
        "failed_to_find_recommendable": evaluation.selected_best_cents is None,
        "stopped_early": run.stopped_early,
        "selected_pair_ids": list(run.selected_pair_ids),
        "query_trace_sha256": hashlib.sha256(
            json.dumps(run.query_read_pair_ids, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _percentile_95(values: Sequence[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))])


def _aggregate(rows: Sequence[dict[str, Any]], group: dict[str, Any]) -> dict[str, Any]:
    regrets = [row["price_regret_cents"] for row in rows if row["price_regret_cents"] is not None]
    return {
        **group,
        "scenario_count": len(rows),
        "regret_evaluable_scenario_count": len(regrets),
        "failed_scenario_count": sum(row["failed_to_find_recommendable"] for row in rows),
        "mean_recall_at_k": mean(row["recall_at_k"] for row in rows),
        "mean_price_regret_cents": mean(regrets) if regrets else None,
        "p95_price_regret_cents": _percentile_95(regrets),
        "failure_rate": mean(row["failed_to_find_recommendable"] for row in rows),
        "mean_exact_search_coverage": mean(row["exact_search_coverage"] for row in rows),
        "mean_successful_oracle_coverage": mean(
            row["successful_oracle_coverage"] for row in rows
        ),
        "early_stop_rate": mean(row["stopped_early"] for row in rows),
    }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def evaluate(path: Path = SCENARIOS) -> dict[str, Any]:
    scenarios = [item for item in _load_scenarios(path) if item["split"] == "test"]
    scenario_rows = [
        _scenario_row(scenario, strategy, budget)
        for scenario in scenarios
        for budget in BUDGETS
        for strategy in STRATEGIES
    ]
    aggregate_rows = []
    for budget in BUDGETS:
        for strategy in STRATEGIES:
            selected = [
                row
                for row in scenario_rows
                if row["budget"] == budget and row["strategy"] == strategy
            ]
            aggregate_rows.append(
                _aggregate(selected, {"budget": budget, "strategy": strategy})
            )
    condition_rows = []
    for condition in sorted({row["condition"] for row in scenario_rows}):
        for budget in BUDGETS:
            for strategy in STRATEGIES:
                selected = [
                    row
                    for row in scenario_rows
                    if row["condition"] == condition
                    and row["budget"] == budget
                    and row["strategy"] == strategy
                ]
                condition_rows.append(
                    _aggregate(
                        selected,
                        {"condition": condition, "budget": budget, "strategy": strategy},
                    )
                )
    adaptive_vs_coarse = []
    for budget in BUDGETS:
        adaptive = next(
            row
            for row in aggregate_rows
            if row["budget"] == budget and row["strategy"] == "adaptive"
        )
        coarse = next(
            row
            for row in aggregate_rows
            if row["budget"] == budget and row["strategy"] == "coarse_cheapest"
        )
        adaptive_vs_coarse.append(
            {
                "budget": budget,
                "adaptive_recall_delta": (
                    adaptive["mean_recall_at_k"] - coarse["mean_recall_at_k"]
                ),
                "adaptive_price_regret_delta_cents": (
                    adaptive["mean_price_regret_cents"]
                    - coarse["mean_price_regret_cents"]
                ),
                "adaptive_dominates_on_both_metrics": (
                    adaptive["mean_recall_at_k"] >= coarse["mean_recall_at_k"]
                    and adaptive["mean_price_regret_cents"]
                    <= coarse["mean_price_regret_cents"]
                ),
            }
        )
    payload: dict[str, Any] = {
        "benchmark_version": "date-search-full-universe-v1",
        "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "split": "test",
        "test_scenario_count": len(scenarios),
        "pairs_per_scenario": 124,
        "budgets": list(BUDGETS),
        "relevant_k": RELEVANT_K,
        "strategies": list(STRATEGIES),
        "oracle_access_contract": (
            "策略只能读取候选的粗价、覆盖率、日期及已主动精查的观测；"
            "完整 exact oracle 在 selection 关闭后才用于评估。"
        ),
        "boundary": BOUNDARY,
        "real_ota_quality_claim_allowed": False,
        "adaptive_winner_claim_allowed": False,
        "adaptive_vs_coarse": adaptive_vs_coarse,
        "finding": (
            "是否优于简单粗价最便宜基线由冻结结果直接报告；若 aggregate 不占优，"
            "不得用局部 condition 胜出包装成总体胜出。synthetic calibration/test 均不授权"
            "修改真实 OTA 策略或线上效果声明。"
        ),
        "aggregate_rows": aggregate_rows,
        "condition_rows": condition_rows,
        "scenario_rows": scenario_rows,
    }
    payload["result_sha256"] = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    return payload


def write_result(result: dict[str, Any], path: Path = DEFAULT_OUTPUT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=SCENARIOS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = evaluate(args.input)
    write_result(result, args.output)
    printable = {key: value for key, value in result.items() if key != "scenario_rows"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
