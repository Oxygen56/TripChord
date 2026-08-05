from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from training.data_contracts import CONTRACT_VERSION, audit_split_contract

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "training" / "data"


def compact_problem(raw_prompt: str) -> str:
    raw = json.loads(raw_prompt)
    compact = {
        "trip": raw["trip"],
        "activity_columns": raw["activity_columns"],
        "activities": raw["activities"],
        "travel_columns": raw["travel_columns"],
        "travel_times": raw["travel_times"],
        "hard_constraints": raw["contract"]["hard_constraints"],
        "output": "strict JSON schedule; verifier remains authoritative",
    }
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    body = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )
    path.write_text(body)
    return hashlib.sha256(body.encode()).hexdigest()


def build_sft(split: str) -> tuple[int, str]:
    source = read_jsonl(DATA / f"sft_{split}.jsonl")
    records: list[dict[str, Any]] = []
    for record in source:
        messages = record["messages"]
        records.append(
            {
                "id": record["id"],
                "city_group": record["city_group"],
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are TripChord's compact itinerary policy. Return the supplied "
                            "strict JSON plan format; do not invent inventory or bypass "
                            "verification."
                        ),
                    },
                    {"role": "user", "content": compact_problem(messages[1]["content"])},
                    messages[2],
                ],
            }
        )
    digest = write_jsonl(DATA / f"compact_itinerary_sft_{split}.jsonl", records)
    return len(records), digest


def build_dpo(split: str) -> tuple[int, str]:
    source = read_jsonl(DATA / f"dpo_{split}.jsonl")
    records: list[dict[str, Any]] = []
    for record in source:
        records.append(
            {
                "id": record["id"],
                "city_group": record["city_group"],
                "prompt": compact_problem(record["prompt"]),
                "chosen": record["chosen"],
                "rejected": record["rejected"],
                "rejection_reasons": record["rejection_reasons"],
                "scenario_id": record["scenario_id"],
            }
        )
    digest = write_jsonl(DATA / f"compact_itinerary_dpo_{split}.jsonl", records)
    return len(records), digest


def main() -> None:
    files: dict[str, dict[str, str | int]] = {}
    for split in ("train", "validation", "test"):
        sft_count, sft_sha = build_sft(split)
        dpo_count, dpo_sha = build_dpo(split)
        files[f"sft_{split}"] = {"records": sft_count, "sha256": sft_sha}
        files[f"dpo_{split}"] = {"records": dpo_count, "sha256": dpo_sha}
    split_audits = {
        kind: audit_split_contract(
            {
                split: read_jsonl(DATA / f"compact_itinerary_{kind}_{split}.jsonl")
                for split in ("train", "validation", "test")
            },
            kind=kind,
        )
        for kind in ("sft", "dpo")
    }
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "source_manifest": "training/data/manifest.json",
        "claim_boundary": (
            "compact synthetic traces preserve scheduling inputs, but city-group isolation "
            "is not evidence of real-user or unseen-template generalization"
        ),
        "split_audits": split_audits,
        "files": files,
    }
    manifest_path = DATA / "compact_itinerary_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
