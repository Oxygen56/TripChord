from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter

from training.data_contracts import CONTRACT_VERSION, sha256

ROOT = Path(__file__).resolve().parents[1]
LORA_EVIDENCE = ROOT / "benchmarks" / "results" / "lora-training-evidence.json"
_LORA_DATASETS = (
    (
        "orchestration",
        "sft",
        ROOT / "training/data/orchestration_sft_train.jsonl",
        ROOT / "training/data/orchestration_sft_validation.jsonl",
    ),
    (
        "orchestration",
        "sft_plus_dpo",
        ROOT / "training/data/orchestration_dpo_train.jsonl",
        ROOT / "training/data/orchestration_dpo_validation.jsonl",
    ),
    (
        "itinerary_generation_repair",
        "sft",
        ROOT / "training/data/compact_itinerary_sft_train.jsonl",
        ROOT / "training/data/compact_itinerary_sft_validation.jsonl",
    ),
    (
        "itinerary_generation_repair",
        "sft_plus_dpo",
        ROOT / "training/data/compact_itinerary_dpo_train.jsonl",
        ROOT / "training/data/compact_itinerary_dpo_validation.jsonl",
    ),
)


def _recorded_hash(run: dict[str, Any], current_name: str, historical_name: str) -> str | None:
    value = run.get(current_name, run.get(historical_name))
    return value if isinstance(value, str) else None


def audit_lora_provenance(path: Path = LORA_EVIDENCE) -> dict[str, JsonValue]:
    payload = json.loads(path.read_text())
    historical = {
        (str(run["domain"]), str(run["phase"])): run
        for run in payload["runs"]
        if isinstance(run, dict)
    }
    rows: list[dict[str, JsonValue]] = []
    for domain, phase, train_path, validation_path in _LORA_DATASETS:
        run = historical[(domain, phase)]
        recorded_train = _recorded_hash(
            run, "training_data_sha256_at_run", "train_data_sha256"
        )
        recorded_validation = _recorded_hash(
            run, "validation_data_sha256_at_run", "validation_data_sha256"
        )
        current_train = sha256(train_path)
        current_validation = sha256(validation_path)
        rows.append(
            TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "domain": domain,
                    "phase": phase,
                    "recorded_train_sha256": recorded_train,
                    "current_train_sha256": current_train,
                    "recorded_validation_sha256": recorded_validation,
                    "current_validation_sha256": current_validation,
                    "matches_current_data": bool(
                        recorded_train == current_train
                        and recorded_validation == current_validation
                    ),
                }
            )
        )
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "evidence_path": str(path.relative_to(ROOT)),
            "runs": rows,
            "all_match_current_data": all(bool(row["matches_current_data"]) for row in rows),
            "corrected_data_adapters_ready": all(
                bool(row["matches_current_data"]) for row in rows
            ),
            "boundary": (
                "A reloadable historical adapter is not an adapter trained on the current "
                "corrected dataset unless its training-time hashes match."
            ),
        }
    )


def audit_runtime_connections() -> dict[str, JsonValue]:
    source_root = ROOT / "apps" / "api" / "src"
    source = "\n".join(path.read_text() for path in source_root.rglob("*.py"))
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "replan_linear_policy_loaded": "replan-policy.json" in source,
            "orchestration_linear_policy_loaded": "orchestration-policy.json" in source,
            "lora_adapter_loaded": any(
                marker in source for marker in ("PeftModel", "adapter_model.safetensors")
            ),
            "boundary": (
                "Source-reference audit proves composition wiring only; it is not production "
                "traffic or model-quality evidence."
            ),
        }
    )


def audit_manifests() -> dict[str, JsonValue]:
    paths = (
        ROOT / "training/data/manifest.json",
        ROOT / "training/data/compact_itinerary_manifest.json",
        ROOT / "training/data/orchestration_manifest.json",
    )
    rows: list[dict[str, JsonValue]] = []
    for path in paths:
        payload = json.loads(path.read_text())
        audits = payload.get("split_audits", {})
        blocking = [
            violation
            for audit in audits.values()
            for violation in audit.get("blocking_violations", [])
        ]
        rows.append(
            TypeAdapter(dict[str, JsonValue]).validate_python(
                {
                    "path": str(path.relative_to(ROOT)),
                    "contract_version": payload.get("contract_version"),
                    "blocking_violations": blocking,
                }
            )
        )
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "required_contract_version": CONTRACT_VERSION,
            "manifests": rows,
            "all_current": all(row["contract_version"] == CONTRACT_VERSION for row in rows),
            "all_blocking_checks_pass": all(not row["blocking_violations"] for row in rows),
        }
    )


def audit() -> dict[str, JsonValue]:
    return TypeAdapter(dict[str, JsonValue]).validate_python(
        {
            "data_contracts": audit_manifests(),
            "lora_provenance": audit_lora_provenance(),
            "runtime_connections": audit_runtime_connections(),
            "overall_boundary": (
                "The corrected data pipeline is validated. Historical three-step LoRA "
                "adapters remain smoke artifacts and must be retrained before they can be "
                "evaluated against the corrected data."
            ),
        }
    )


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
