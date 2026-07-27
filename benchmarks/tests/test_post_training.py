from __future__ import annotations

import json
from pathlib import Path

from training.build_trace_datasets import SPLIT_GROUPS
from training.policy_reranker import build_examples, metrics, train
from training.train_dpo import validate_records as validate_dpo
from training.train_sft import validate_records as validate_sft

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "training" / "data"


def groups_in(path: Path) -> set[int]:
    return {
        int(json.loads(line)["city_group"].rsplit("-", maxsplit=1)[1])
        for line in path.read_text().splitlines()
        if line.strip()
    }


def test_post_training_data_has_group_isolated_splits() -> None:
    observed: dict[str, set[int]] = {}
    for split in SPLIT_GROUPS:
        sft_path = DATA / f"sft_{split}.jsonl"
        dpo_path = DATA / f"dpo_{split}.jsonl"
        assert validate_sft(sft_path) > 0
        assert validate_dpo(dpo_path) > 0
        assert groups_in(sft_path) == set(SPLIT_GROUPS[split])
        assert groups_in(dpo_path) == set(SPLIT_GROUPS[split])
        observed[split] = groups_in(sft_path)
    assert observed["train"].isdisjoint(observed["validation"] | observed["test"])
    assert observed["validation"].isdisjoint(observed["test"])


def test_policy_reranker_improves_over_always_local_on_unseen_city_groups() -> None:
    examples = build_examples()
    weights = train(examples)
    held_out = metrics(weights, examples, "test")

    assert held_out["examples"] == 60
    assert held_out["top1_accuracy"] > held_out["always_local_accuracy"]
    assert held_out["mean_oracle_regret"] < 0.01
