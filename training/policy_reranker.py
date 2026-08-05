from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "training" / "artifacts" / "replan-policy.json"
RESULTS = ROOT / "benchmarks" / "results" / "phase-7-post-training.json"
FROZEN_INPUT = ROOT / "training" / "data" / "replan_policy_examples_v1.json"
PROFILES = {
    "minimum_change": (0.9, 0.1),
    "balanced": (0.5, 0.5),
    "quality_first": (0.0, 1.0),
}


@dataclass(frozen=True)
class PolicyExample:
    scenario_id: str
    city_group: str
    split: str
    profile: str
    stability_weight: float
    quality_weight: float
    local_preservation: float
    local_utility: float
    global_preservation: float
    global_utility: float
    oracle_action: str
    oracle_score: float


def split_for_city(city_group: str) -> str:
    index = int(city_group.rsplit("-", maxsplit=1)[1])
    if index <= 7:
        return "train"
    if index <= 9:
        return "validation"
    return "test"


def action_features(example: PolicyExample, action: str) -> tuple[float, ...]:
    is_local = float(action == "local")
    preservation = example.local_preservation if action == "local" else example.global_preservation
    utility = example.local_utility if action == "local" else example.global_utility
    return (
        example.stability_weight * preservation,
        example.quality_weight * utility,
        preservation,
        utility,
        is_local,
        example.stability_weight * is_local,
        1.0,
    )


def build_examples() -> list[PolicyExample]:
    payload = json.loads(FROZEN_INPUT.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "tripchord-replan-policy-examples-v1":
        raise ValueError("unsupported frozen replan policy input schema")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("frozen replan policy input must contain rows")
    examples: list[PolicyExample] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("frozen replan policy row must be an object")
        for profile, (stability_weight, quality_weight) in PROFILES.items():
            local_score = (
                stability_weight * row["local_preservation"]
                + quality_weight * row["local_utility_retention"]
            )
            global_score = (
                stability_weight * row["global_preservation"]
                + quality_weight * row["global_utility_retention"]
            )
            action = "local" if local_score >= global_score else "global"
            examples.append(
                PolicyExample(
                    scenario_id=row["id"],
                    city_group=row["city_group"],
                    split=split_for_city(row["city_group"]),
                    profile=profile,
                    stability_weight=stability_weight,
                    quality_weight=quality_weight,
                    local_preservation=row["local_preservation"],
                    local_utility=row["local_utility_retention"],
                    global_preservation=row["global_preservation"],
                    global_utility=row["global_utility_retention"],
                    oracle_action=action,
                    oracle_score=max(local_score, global_score),
                )
            )
    return examples


def dot(weights: list[float], features: tuple[float, ...]) -> float:
    return sum(weight * feature for weight, feature in zip(weights, features, strict=True))


def train(examples: list[PolicyExample], epochs: int = 1500, rate: float = 0.1) -> list[float]:
    train_rows = [example for example in examples if example.split == "train"]
    weights = [0.0] * len(action_features(train_rows[0], "local"))
    for _ in range(epochs):
        gradient = [0.0] * len(weights)
        for example in train_rows:
            chosen = action_features(example, example.oracle_action)
            rejected_action = "global" if example.oracle_action == "local" else "local"
            rejected = action_features(example, rejected_action)
            difference = tuple(
                chosen_value - rejected_value
                for chosen_value, rejected_value in zip(chosen, rejected, strict=True)
            )
            margin = max(-30.0, min(30.0, dot(weights, difference)))
            scale = 1.0 / (1.0 + math.exp(margin))
            for index, value in enumerate(difference):
                gradient[index] += scale * value
        for index in range(len(weights)):
            weights[index] += rate * gradient[index] / len(train_rows)
    return weights


def predict(weights: list[float], example: PolicyExample) -> str:
    local = dot(weights, action_features(example, "local"))
    global_score = dot(weights, action_features(example, "global"))
    return "local" if local >= global_score else "global"


def action_score(example: PolicyExample, action: str) -> float:
    if action == "local":
        return (
            example.stability_weight * example.local_preservation
            + example.quality_weight * example.local_utility
        )
    return (
        example.stability_weight * example.global_preservation
        + example.quality_weight * example.global_utility
    )


def metrics(weights: list[float], examples: list[PolicyExample], split: str) -> dict[str, Any]:
    rows = [example for example in examples if example.split == split]
    predictions = [predict(weights, example) for example in rows]
    oracle = [example.oracle_action for example in rows]
    regrets = [
        example.oracle_score - action_score(example, prediction)
        for example, prediction in zip(rows, predictions, strict=True)
    ]
    return {
        "examples": len(rows),
        "top1_accuracy": mean(
            prediction == expected for prediction, expected in zip(predictions, oracle, strict=True)
        ),
        "always_local_accuracy": mean(expected == "local" for expected in oracle),
        "closed_form_oracle_accuracy": mean(
            (
                "local"
                if action_score(example, "local") >= action_score(example, "global")
                else "global"
            )
            == expected
            for example, expected in zip(rows, oracle, strict=True)
        ),
        "mean_oracle_regret": mean(regrets),
        "oracle_local_rate": mean(expected == "local" for expected in oracle),
    }


def run() -> dict[str, Any]:
    examples = build_examples()
    weights = train(examples)
    frozen_input_sha256 = hashlib.sha256(FROZEN_INPUT.read_bytes()).hexdigest()
    artifact = {
        "model": "pairwise-logistic-linear",
        "feature_order": [
            "weighted_preservation",
            "weighted_utility",
            "preservation",
            "utility",
            "is_local",
            "stability_weight_x_is_local",
            "bias",
        ],
        "weights": weights,
        "profiles": PROFILES,
        "safety_contract": (
            "rerank feasible candidates only; deterministic verifier remains mandatory"
        ),
    }
    serialized = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    results = {
        "label_source": "synthetic weighted policy oracle; not human preference data",
        "split_policy": "destination city group; no city group crosses splits",
        "evaluation_scope": (
            "oracle-formula distillation on synthetic scenarios; candidate score terms used "
            "to create labels are also model inputs"
        ),
        "semantic_template_holdout": False,
        "oracle_feature_coupling": True,
        "production_runtime_loaded": True,
        "frozen_input_sha256": frozen_input_sha256,
        "claim_boundary": (
            "top1 accuracy is not learned preference quality or unseen-task generalization; "
            "the closed-form oracle is the appropriate upper baseline"
        ),
        "model_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
        "metrics": {
            split: metrics(weights, examples, split) for split in ("train", "validation", "test")
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(serialized)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return results


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
