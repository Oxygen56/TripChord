from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from tripchord.agents.models import (
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
)
from tripchord.domain.common import Money
from tripchord.domain.trip import TripSpec
from tripchord.planning.problem import ActivityAvailability, ActivityCandidate, PlanningProblem

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "benchmarks" / "scenarios" / "agent-suite-v1.jsonl"

CATEGORIES = (
    "standard",
    "budget_tight",
    "must_visit",
    "tight_window",
    "required_preference_satisfied",
    "required_preference_conflict",
    "forbidden_preference_conflict",
    "neural_repair",
    "orchestrator_invalid_fallback",
    "transient_tool_recovery",
    "evidence_conflict",
    "weighted_preference",
)


def _problem(index: int, category: str) -> PlanningProblem:
    trip_date = date(2026, 10, 1) + timedelta(days=index % 20)
    budget = "95" if category == "budget_tight" else "500"
    return PlanningProblem(
        trip=TripSpec(
            origin="上海",
            destinations=(f"冻结城市-{(index // len(CATEGORIES)) % 12}",),
            start_date=trip_date,
            end_date=trip_date,
            budget=Money(amount=budget, currency="CNY"),
            must_visit=(f"必去-{index}",),
            max_main_activities_per_day=2,
        ),
        activities=(
            ActivityCandidate(
                id=f"must-{index}",
                title=f"必去-{index}",
                duration_minutes=90,
                cost_cents=6000,
                utility=300,
                must_visit=True,
                availability=(
                    ActivityAvailability(
                        date=trip_date,
                        start_minute=9 * 60,
                        end_minute=20 * 60,
                    ),
                ),
                source_refs=(f"frozen:must-{index}",),
            ),
            ActivityCandidate(
                id=f"optional-{index}",
                title=f"可选-{index}",
                duration_minutes=90,
                cost_cents=3400,
                utility=180,
                availability=(
                    ActivityAvailability(
                        date=trip_date,
                        start_minute=(11 * 60 if category == "tight_window" else 9 * 60),
                        end_minute=(13 * 60 if category == "tight_window" else 20 * 60),
                    ),
                ),
                source_refs=(f"frozen:optional-{index}",),
            ),
        ),
        solver_time_limit_seconds=1,
    )


def _preferences(category: str) -> PreferenceConstitution:
    if category in {"required_preference_satisfied", "required_preference_conflict"}:
        return PreferenceConstitution(
            rules=(
                PreferenceRule(
                    key="hotel_breakfast",
                    mode=PreferenceMode.REQUIRED,
                    source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                    weight=1,
                ),
            )
        )
    if category == "forbidden_preference_conflict":
        return PreferenceConstitution(
            rules=(
                PreferenceRule(
                    key="red_eye_flight",
                    mode=PreferenceMode.FORBIDDEN,
                    expected=True,
                    source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                    weight=1,
                ),
            )
        )
    if category == "weighted_preference":
        return PreferenceConstitution(
            rules=(
                PreferenceRule(
                    key="hotel_breakfast",
                    mode=PreferenceMode.WEIGHTED,
                    expected=True,
                    source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
                    weight=0.85,
                ),
            )
        )
    return PreferenceConstitution()


def generate(count: int = 240) -> list[dict[str, object]]:
    scenarios: list[dict[str, object]] = []
    for index in range(count):
        category = CATEGORIES[index % len(CATEGORIES)]
        problem = _problem(index, category)
        blocked = category in {
            "required_preference_conflict",
            "forbidden_preference_conflict",
        }
        scenarios.append(
            {
                "id": f"agent-suite-{index:04d}",
                "category": category,
                "problem": problem.model_dump(mode="json"),
                "preferences": _preferences(category).model_dump(mode="json"),
                "hotel_breakfast": category != "required_preference_conflict",
                "red_eye_flight": category == "forbidden_preference_conflict",
                "neural_shift_minutes": 720 if category == "neural_repair" else 0,
                "orchestrator_candidate": (
                    "candidate:neural"
                    if category == "neural_repair"
                    else "candidate:missing"
                    if category == "orchestrator_invalid_fallback"
                    else "candidate:cp-sat"
                ),
                "transient_tool_failure": category == "transient_tool_recovery",
                "expected_state": "replan_or_block" if blocked else "accept",
                "expected_repair": category == "neural_repair",
            }
        )
    return scenarios


def write(path: Path = OUTPUT) -> None:
    rows = generate()
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows) + "\n"
    )


if __name__ == "__main__":
    write()
