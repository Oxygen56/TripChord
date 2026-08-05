from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from training.data_contracts import (
    CONTRACT_VERSION,
    audit_split_contract,
    orchestration_semantic_fingerprint,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "benchmarks" / "scenarios" / "agent-suite-v1.jsonl"
OUTPUT = ROOT / "training" / "data"


def split_for(scenario: dict[str, Any]) -> str:
    destination = str(scenario["problem"]["trip"]["destinations"][0])
    group = int(destination.rsplit("-", maxsplit=1)[1])
    if group <= 7:
        return "train"
    if group <= 9:
        return "validation"
    return "test"


def prompt(scenario: dict[str, Any]) -> str:
    payload = {
        "trip": scenario["problem"]["trip"],
        "preferences": scenario["preferences"],
        "signals": {
            "hotel_breakfast": scenario["hotel_breakfast"],
            "red_eye_flight": scenario["red_eye_flight"],
            "neural_shift_minutes": scenario["neural_shift_minutes"],
            "transient_tool_failure": scenario["transient_tool_failure"],
        },
        "contract": {
            "actions": ["accept", "repair", "block"],
            "hard_rules": [
                "explicit_required_and_forbidden_preferences",
                "deterministic_constraints",
                "L3_requires_user_approval",
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def chosen_action(scenario: dict[str, Any]) -> str:
    if scenario["expected_state"] == "replan_or_block":
        return "block"
    if scenario["expected_repair"] or scenario["transient_tool_failure"]:
        return "repair"
    return "accept"


def rejected_action(chosen: str) -> str:
    return "accept" if chosen in {"block", "repair"} else "block"


def answer_for_action(action: str) -> dict[str, Any]:
    base_graph = [
        "parallel_source_agents",
        "preference_guard",
        "evidence_arbiter",
        "dual_planners",
        "critic",
        "orchestrator",
    ]
    return {
        "action": action,
        "decision_state": "accept" if action == "accept" else "replan_or_block",
        "must_disclose": action != "accept",
        "task_graph": [
            *base_graph,
            *(("repair", "orchestrator_final") if action == "repair" else ()),
        ],
    }


def build(path: Path = SOURCE) -> dict[str, list[dict[str, Any]]]:
    scenarios = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    datasets: dict[str, list[dict[str, Any]]] = {}
    for split in ("train", "validation", "test"):
        datasets[f"orchestration_sft_{split}"] = []
        datasets[f"orchestration_dpo_{split}"] = []
    for scenario in scenarios:
        split = split_for(scenario)
        chosen = chosen_action(scenario)
        answer = answer_for_action(chosen)
        datasets[f"orchestration_sft_{split}"].append(
            {
                "id": scenario["id"],
                "city_group": scenario["problem"]["trip"]["destinations"][0],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are TripChord's orchestration policy. Return strict JSON; "
                            "never bypass explicit preferences, deterministic constraints, "
                            "or tool permission gates."
                        ),
                    },
                    {"role": "user", "content": prompt(scenario)},
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            answer,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    },
                ],
            }
        )
        rejected = answer_for_action(rejected_action(chosen))
        datasets[f"orchestration_dpo_{split}"].append(
            {
                "id": f"{scenario['id']}:policy",
                "scenario_id": scenario["id"],
                "city_group": scenario["problem"]["trip"]["destinations"][0],
                "prompt": prompt(scenario),
                "chosen": json.dumps(answer, ensure_ascii=False, sort_keys=True),
                "rejected": json.dumps(rejected, ensure_ascii=False, sort_keys=True),
                "rejection_reasons": [
                    "violates deterministic orchestration oracle or preference constitution"
                ],
                "label_source": "frozen deterministic orchestration oracle",
            }
        )
    return datasets


def write(datasets: dict[str, list[dict[str, Any]]], output: Path = OUTPUT) -> dict[str, Any]:
    split_audits = {
        kind: audit_split_contract(
            {
                split: datasets[f"orchestration_{kind}_{split}"]
                for split in ("train", "validation", "test")
            },
            kind=kind,
            semantic_fingerprint=orchestration_semantic_fingerprint,
        )
        for kind in ("sft", "dpo")
    }
    files: dict[str, dict[str, Any]] = {}
    for name, records in datasets.items():
        payload = "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
            for record in records
        )
        path = output / f"{name}.jsonl"
        path.write_text(payload)
        files[path.name] = {
            "records": len(records),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "source": str(SOURCE.relative_to(ROOT)),
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "split_policy": "destination city group; groups 0-7 train, 8-9 validation, 10-11 test",
        "label_source": "frozen deterministic orchestration oracle; no human preference labels",
        "split_audits": split_audits,
        "claim_boundary": (
            "city groups are isolated, but label-relevant semantic templates cross splits; "
            "metrics are oracle-imitation regression, not unseen-task generalization"
        ),
        "files": files,
    }
    (output / "orchestration_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    return manifest


if __name__ == "__main__":
    print(json.dumps(write(build()), ensure_ascii=False, indent=2))
