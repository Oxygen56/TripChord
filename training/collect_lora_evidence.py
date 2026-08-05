from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "HuggingFaceTB/SmolLM2-135M-Instruct"


class RunSpec(TypedDict):
    domain: str
    phase: str
    path: Path
    train_data: Path
    validation_data: Path


RUNS: tuple[RunSpec, ...] = (
    {
        "domain": "orchestration",
        "phase": "sft",
        "path": ROOT / "training/runs/orchestration-sft-lora",
        "train_data": ROOT / "training/data/orchestration_sft_train.jsonl",
        "validation_data": ROOT / "training/data/orchestration_sft_validation.jsonl",
    },
    {
        "domain": "orchestration",
        "phase": "sft_plus_dpo",
        "path": ROOT / "training/runs/orchestration-sft-dpo-lora",
        "train_data": ROOT / "training/data/orchestration_dpo_train.jsonl",
        "validation_data": ROOT / "training/data/orchestration_dpo_validation.jsonl",
    },
    {
        "domain": "itinerary_generation_repair",
        "phase": "sft",
        "path": ROOT / "training/runs/itinerary-compact-sft-lora",
        "train_data": ROOT / "training/data/compact_itinerary_sft_train.jsonl",
        "validation_data": ROOT / "training/data/compact_itinerary_sft_validation.jsonl",
    },
    {
        "domain": "itinerary_generation_repair",
        "phase": "sft_plus_dpo",
        "path": ROOT / "training/runs/itinerary-compact-sft-dpo-lora",
        "train_data": ROOT / "training/data/compact_itinerary_dpo_train.jsonl",
        "validation_data": ROOT / "training/data/compact_itinerary_dpo_validation.jsonl",
    },
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def line_count(path: Path) -> int:
    return sum(1 for line in path.read_text().splitlines() if line.strip())


def metrics_for(path: Path) -> dict[str, Any]:
    metrics_path = path / "tripchord_training_metrics.json"
    if metrics_path.exists():
        return cast(dict[str, Any], json.loads(metrics_path.read_text()))
    trainer_state = path / "checkpoint-3/trainer_state.json"
    state = json.loads(trainer_state.read_text())
    return {
        "max_steps": state["global_step"],
        "epoch": state["epoch"],
        "validation": state["log_history"][-1],
        "legacy_run_note": "run predates automatic TripChord metrics file",
    }


def _historical_run_index(output: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    if not output.exists():
        return {}
    payload = json.loads(output.read_text())
    runs = payload.get("runs", []) if isinstance(payload, dict) else []
    return {
        (str(run["domain"]), str(run["phase"]), str(run["adapter_sha256"])): run
        for run in runs
        if isinstance(run, dict)
        and all(key in run for key in ("domain", "phase", "adapter_sha256"))
    }


def training_snapshot_hashes(
    run_metrics: dict[str, Any], historical_run: dict[str, Any] | None
) -> tuple[object, object, str]:
    """Resolve immutable training-time hashes without rebinding to current data."""
    training_train_sha = run_metrics.get("train_data_sha256")
    training_validation_sha = run_metrics.get("validation_data_sha256")
    provenance_source = "run_metrics"
    if not isinstance(training_train_sha, str) or not isinstance(
        training_validation_sha, str
    ):
        provenance_source = "historical_evidence"
        training_train_sha = (
            historical_run.get(
                "training_data_sha256_at_run",
                historical_run.get("train_data_sha256"),
            )
            if historical_run
            else None
        )
        training_validation_sha = (
            historical_run.get(
                "validation_data_sha256_at_run",
                historical_run.get("validation_data_sha256"),
            )
            if historical_run
            else None
        )
    return training_train_sha, training_validation_sha, provenance_source


def main() -> None:
    os.environ.setdefault("HF_ENABLE_PARALLEL_LOADING", "false")
    from peft import PeftModel
    from safetensors import safe_open
    from transformers import AutoModelForCausalLM

    output = ROOT / "benchmarks/results/lora-training-evidence.json"
    historical = _historical_run_index(output)
    evidence: list[dict[str, Any]] = []
    for run in RUNS:
        path = run["path"]
        adapter = path / "adapter_model.safetensors"
        adapter_digest = sha256(adapter)
        config = json.loads((path / "adapter_config.json").read_text())
        run_metrics = metrics_for(path)
        historical_run = historical.get((str(run["domain"]), str(run["phase"]), adapter_digest))
        training_train_sha, training_validation_sha, provenance_source = (
            training_snapshot_hashes(run_metrics, historical_run)
        )
        current_train_sha = sha256(run["train_data"])
        current_validation_sha = sha256(run["validation_data"])
        with safe_open(adapter, framework="pt", device="cpu") as handle:
            tensor_keys = list(handle.keys())
        base = AutoModelForCausalLM.from_pretrained(MODEL_ID)
        loaded = PeftModel.from_pretrained(base, path)
        peft_config = loaded.peft_config["default"]
        evidence.append(
            {
                "domain": run["domain"],
                "phase": run["phase"],
                "model": MODEL_ID,
                "adapter_path": str(path.relative_to(ROOT)),
                "adapter_bytes": adapter.stat().st_size,
                "adapter_sha256": adapter_digest,
                "tensor_count": len(tensor_keys),
                "reload_passed": True,
                "peft_type": str(peft_config.peft_type),
                "base_model_reference": config["base_model_name_or_path"],
                "train_records": line_count(run["train_data"]),
                "validation_records": line_count(run["validation_data"]),
                "training_data_sha256_at_run": training_train_sha,
                "validation_data_sha256_at_run": training_validation_sha,
                "training_data_provenance_source": provenance_source,
                "current_train_data_sha256": current_train_sha,
                "current_validation_data_sha256": current_validation_sha,
                "current_data_matches_training_snapshot": bool(
                    training_train_sha == current_train_sha
                    and training_validation_sha == current_validation_sha
                ),
                "metrics": run_metrics,
            }
        )
        del loaded, base

    result = {
        "model": MODEL_ID,
        "runs": evidence,
        "all_adapters_reload": all(run["reload_passed"] for run in evidence),
        "domains_trained": sorted({str(run["domain"]) for run in evidence}),
        "phases_completed": sorted({str(run["phase"]) for run in evidence}),
        "quality_claim": False,
        "all_current_datasets_match_training_snapshots": all(
            run["current_data_matches_training_snapshot"] for run in evidence
        ),
        "claim_boundary": (
            "Actual bounded LoRA optimization and reload are verified. The 135M smoke runs "
            "do not establish production Chinese itinerary quality; held-out safety/accuracy "
            "claims come from the separately reported lightweight policies. A corrected "
            "dataset requires a new training run; current files are never rebound to an old "
            "adapter without training-time hashes."
        ),
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
