from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

from training.build_orchestration_datasets import chosen_action, split_for

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks" / "scenarios" / "agent-suite-v1.jsonl"
ARTIFACT = ROOT / "training" / "artifacts" / "orchestration-policy.json"
RESULT = ROOT / "benchmarks" / "results" / "orchestration-post-training.json"
ACTIONS = ("accept", "repair", "block")
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


def features(scenario: dict[str, Any]) -> tuple[float, ...]:
    preferences = scenario["preferences"]["rules"]
    modes = {rule["mode"] for rule in preferences}
    return (
        1.0,
        float(not scenario["hotel_breakfast"]),
        float(scenario["red_eye_flight"]),
        float(int(scenario["neural_shift_minutes"]) > 0),
        float(scenario["transient_tool_failure"]),
        float("required" in modes),
        float("forbidden" in modes),
        float("weighted" in modes),
    )


def _scores(weights: list[list[float]], row: tuple[float, ...]) -> list[float]:
    return [
        sum(weight * value for weight, value in zip(action, row, strict=True)) for action in weights
    ]


def predict(weights: list[list[float]], scenario: dict[str, Any]) -> str:
    scores = _scores(weights, features(scenario))
    return ACTIONS[max(range(len(scores)), key=scores.__getitem__)]


def _softmax(scores: list[float]) -> list[float]:
    maximum = max(scores)
    exponents = [math.exp(score - maximum) for score in scores]
    total = sum(exponents)
    return [value / total for value in exponents]


def train_sft(
    scenarios: list[dict[str, Any]],
    *,
    epochs: int = 800,
    learning_rate: float = 0.08,
) -> list[list[float]]:
    train_rows = [scenario for scenario in scenarios if split_for(scenario) == "train"]
    width = len(features(train_rows[0]))
    weights = [[0.0] * width for _ in ACTIONS]
    for _ in range(epochs):
        gradients = [[0.0] * width for _ in ACTIONS]
        for scenario in train_rows:
            row = features(scenario)
            expected = ACTIONS.index(chosen_action(scenario))
            probabilities = _softmax(_scores(weights, row))
            for action_index in range(len(ACTIONS)):
                error = probabilities[action_index] - float(action_index == expected)
                for index, value in enumerate(row):
                    gradients[action_index][index] += error * value
        for action_index in range(len(ACTIONS)):
            for index in range(width):
                weights[action_index][index] -= (
                    learning_rate * gradients[action_index][index] / len(train_rows)
                )
    return weights


def train_dpo(
    initial: list[list[float]],
    scenarios: list[dict[str, Any]],
    *,
    epochs: int = 300,
    learning_rate: float = 0.03,
) -> list[list[float]]:
    weights = [row.copy() for row in initial]
    train_rows = [scenario for scenario in scenarios if split_for(scenario) == "train"]
    for _ in range(epochs):
        for scenario in train_rows:
            row = features(scenario)
            chosen = ACTIONS.index(chosen_action(scenario))
            rejected = ACTIONS.index("accept" if ACTIONS[chosen] != "accept" else "block")
            scores = _scores(weights, row)
            margin = max(-30.0, min(30.0, scores[chosen] - scores[rejected]))
            scale = 1.0 / (1.0 + math.exp(margin))
            for index, value in enumerate(row):
                update = learning_rate * scale * value
                weights[chosen][index] += update
                weights[rejected][index] -= update
    return weights


def metrics(
    weights: list[list[float]],
    scenarios: list[dict[str, Any]],
    split: str,
) -> dict[str, Any]:
    rows = [scenario for scenario in scenarios if split_for(scenario) == split]
    predictions = [predict(weights, scenario) for scenario in rows]
    expected = [chosen_action(scenario) for scenario in rows]
    risky = [index for index, action in enumerate(expected) if action != "accept"]
    return {
        "examples": len(rows),
        "accuracy": mean(
            prediction == target for prediction, target in zip(predictions, expected, strict=True)
        ),
        "unsafe_accept_rate": (
            mean(predictions[index] == "accept" for index in risky) if risky else 0
        ),
    }


def run(path: Path = SOURCE) -> dict[str, Any]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    width = len(features(scenarios[0]))
    base = [[0.0] * width for _ in ACTIONS]
    sft = train_sft(scenarios)
    dpo = train_dpo(sft, scenarios)
    artifact = {
        "model": "multiclass-linear-orchestration-policy",
        "actions": ACTIONS,
        "benchmark_categories": CATEGORIES,
        "feature_count": width,
        "feature_contract": (
            "runtime-observable signals only; benchmark category labels are excluded"
        ),
        "weights": dpo,
        "safety_contract": (
            "advisory only; Preference Guard and deterministic Verifier remain mandatory"
        ),
    }
    serialized = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    result = {
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "split_policy": "destination city group; every split contains all 12 categories",
        "label_source": "frozen deterministic orchestration oracle; no human preference labels",
        "evaluation_scope": (
            "city-group-held-out oracle imitation; semantic templates repeat across splits"
        ),
        "semantic_template_holdout": False,
        "production_runtime_loaded": False,
        "claim_boundary": (
            "accuracy does not establish LLM capability, unseen-task generalization, "
            "or production travel quality"
        ),
        "artifact_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "held_out_test": {
            "base": metrics(base, scenarios, "test"),
            "sft": metrics(sft, scenarios, "test"),
            "sft_plus_dpo": metrics(dpo, scenarios, "test"),
        },
        "safety_regression": (
            metrics(dpo, scenarios, "test")["unsafe_accept_rate"]
            > metrics(sft, scenarios, "test")["unsafe_accept_rate"]
        ),
    }
    ARTIFACT.write_text(serialized)
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
