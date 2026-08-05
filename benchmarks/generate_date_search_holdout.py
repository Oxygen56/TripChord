from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from benchmarks.evaluate_date_search import _canonical_bytes
from benchmarks.generate_date_search_scenarios import (
    CONDITIONS,
    _canonical_json,
    _make_scenario,
)

ROOT = Path(__file__).resolve().parents[1]
POLICY_MANIFEST = ROOT / "benchmarks" / "manifests" / "date-search-hybrid-v2.json"
DEFAULT_OUTPUT = (
    ROOT / "benchmarks" / "scenarios" / "date-search-sealed-holdout-v2-4to7n.jsonl"
)
FROZEN_POLICY_MANIFEST_FILE_SHA256 = (
    "7494da26f48a1ee88548e59d7a6f8522c8624e3f96f12c725a80e0a48aa1267e"
)
HOLDOUT_SEED_COUNT = 16


def _load_frozen_policy(path: Path = POLICY_MANIFEST) -> dict[str, Any]:
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != FROZEN_POLICY_MANIFEST_FILE_SHA256:
        raise ValueError("policy manifest changed after calibration freeze")
    manifest: dict[str, Any] = json.loads(content)
    claimed = manifest.pop("policy_manifest_sha256")
    actual = hashlib.sha256(_canonical_bytes(manifest)).hexdigest()
    manifest["policy_manifest_sha256"] = claimed
    if claimed != actual or manifest["status"] != "frozen_calibration_candidate":
        raise ValueError("policy manifest signature/status is invalid")
    return manifest


def _sealed_seeds(policy_sha256: str) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(
            hashlib.sha256(f"{policy_sha256}:sealed-holdout:{index}".encode()).digest()[:8],
            "big",
        )
        for index in range(HOLDOUT_SEED_COUNT)
    )


def generate_holdout(path: Path = POLICY_MANIFEST) -> list[dict[str, Any]]:
    manifest = _load_frozen_policy(path)
    seeds = _sealed_seeds(manifest["policy_manifest_sha256"])
    return [
        _make_scenario(
            split="sealed_holdout",
            seed=seed,
            condition=condition,
            night_counts=(4, 5, 6, 7),
            source_request_day_range=(5, 8),
        )
        for condition in CONDITIONS
        for seed in seeds
    ]


def write_holdout(
    path: Path = DEFAULT_OUTPUT,
    *,
    policy_path: Path = POLICY_MANIFEST,
) -> str:
    scenarios = generate_holdout(policy_path)
    content = "\n".join(_canonical_json(item) for item in scenarios) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, default=POLICY_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(write_holdout(args.output, policy_path=args.policy))


if __name__ == "__main__":
    main()
