from __future__ import annotations

import hashlib
import json
from pathlib import Path

from benchmarks.evaluate_replanning_scale import SCENARIOS, evaluate_rows

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "training" / "data" / "replan_policy_examples_v1.json"


def run() -> dict[str, object]:
    rows = evaluate_rows()
    payload: dict[str, object] = {
        "schema_version": "tripchord-replan-policy-examples-v1",
        "source": "frozen CP-SAT replanning feature rows",
        "source_scenarios": "benchmarks/scenarios/planning-scale-v1.jsonl",
        "source_scenarios_sha256": hashlib.sha256(SCENARIOS.read_bytes()).hexdigest(),
        "derivation_boundary": (
            "The rows are a versioned training input snapshot. Regeneration may select a "
            "different equal-objective CP-SAT solution across OR-Tools platforms and must be "
            "reviewed as a dataset change rather than silently accepted by CI."
        ),
        "rows": rows,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, indent=2))
