from __future__ import annotations

import json
from pathlib import Path

import pytest

from training.build_compact_lora_datasets import compact_problem
from training.build_orchestration_datasets import build as build_orchestration
from training.build_orchestration_datasets import split_for as orchestration_split
from training.build_trace_datasets import SPLIT_GROUPS
from training.collect_lora_evidence import training_snapshot_hashes
from training.data_contracts import DataContractError, validate_dpo_records
from training.orchestration_policy import run as run_orchestration_policy
from training.policy_reranker import build_examples, metrics, train
from training.post_training_audit import audit
from training.train_dpo import (
    PROMPT_COMPLETION_SEPARATOR,
    add_stable_completion_boundary,
)
from training.train_dpo import token_length_audit as dpo_token_length_audit
from training.train_dpo import validate_records as validate_dpo
from training.train_sft import token_length_audit as sft_token_length_audit
from training.train_sft import validate_records as validate_sft

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "training" / "data"


class _CharacterTokenizer:
    def apply_chat_template(self, messages: list[dict[str, str]], *, tokenize: bool) -> str:
        assert tokenize is False
        return "|".join(message["content"] for message in messages)

    def encode(self, text: str, *, add_special_tokens: bool) -> list[str]:
        assert add_special_tokens is False
        return list(text)


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


def test_policy_reranker_is_disclosed_as_oracle_formula_distillation() -> None:
    examples = build_examples()
    weights = train(examples)
    held_out = metrics(weights, examples, "test")

    assert held_out["examples"] == 60
    assert held_out["top1_accuracy"] > held_out["always_local_accuracy"]
    assert held_out["closed_form_oracle_accuracy"] == 1
    assert held_out["top1_accuracy"] <= held_out["closed_form_oracle_accuracy"]
    assert held_out["mean_oracle_regret"] < 0.01


def test_orchestration_post_training_data_is_balanced_and_group_isolated() -> None:
    datasets = build_orchestration()
    assert sum(len(rows) for name, rows in datasets.items() if "sft" in name) == 240
    for split in ("train", "validation", "test"):
        sft = datasets[f"orchestration_sft_{split}"]
        dpo = datasets[f"orchestration_dpo_{split}"]
        assert len(sft) == len(dpo)
        assert all(
            orchestration_split({"problem": {"trip": {"destinations": [row["city_group"]]}}})
            == split
            for row in sft
        )
        for row in dpo:
            prompt = json.loads(row["prompt"])
            chosen = json.loads(row["chosen"])
            rejected = json.loads(row["rejected"])
            assert "category" not in prompt
            assert set(chosen) == set(rejected)
            assert chosen["action"] != rejected["action"]
            assert chosen["must_disclose"] is (chosen["action"] != "accept")
            assert rejected["must_disclose"] is (rejected["action"] != "accept")


def test_orchestration_policy_reports_oracle_imitation_boundary() -> None:
    result = run_orchestration_policy()
    held_out = result["held_out_test"]
    assert held_out["sft"]["accuracy"] > held_out["base"]["accuracy"]
    assert held_out["sft_plus_dpo"]["accuracy"] >= held_out["sft"]["accuracy"]
    assert held_out["sft_plus_dpo"]["unsafe_accept_rate"] == 0
    assert result["safety_regression"] is False
    assert result["semantic_template_holdout"] is False
    assert result["production_runtime_loaded"] is False
    assert "oracle imitation" in result["evaluation_scope"]


def test_compact_itinerary_lora_data_keeps_constraints_and_group_isolation() -> None:
    original = json.loads((DATA / "sft_train.jsonl").read_text().splitlines()[0])
    raw_prompt = original["messages"][1]["content"]
    compact_prompt = compact_problem(raw_prompt)
    compact = json.loads(compact_prompt)
    assert compact["trip"]["must_visit"]
    assert "budget" in compact["hard_constraints"]
    assert all(len(row) == 7 for row in compact["activities"])
    assert compact["travel_columns"] == ["origin_id", "destination_id", "minutes"]
    assert compact["travel_times"]
    assert len(compact_prompt) < len(raw_prompt)

    observed: dict[str, set[int]] = {}
    for split in SPLIT_GROUPS:
        path = DATA / f"compact_itinerary_sft_{split}.jsonl"
        assert validate_sft(path) > 0
        observed[split] = groups_in(path)
    assert observed["train"].isdisjoint(observed["validation"] | observed["test"])


def test_dpo_prompt_boundary_is_explicit_for_raw_json() -> None:
    prepared = add_stable_completion_boundary({"prompt": '{"goal":"plan"}   '})
    assert prepared["prompt"].endswith(PROMPT_COMPLETION_SEPARATOR)
    assert "}{" not in prepared["prompt"] + '{"decision":"accept"}'


def test_itinerary_completions_delegate_verification_and_dpo_has_no_label_marker() -> None:
    sft = json.loads((DATA / "sft_train.jsonl").read_text().splitlines()[0])
    answer = json.loads(sft["messages"][-1]["content"])
    assert "verification" not in answer
    assert answer["verification_handoff"] == {
        "authoritative_component": "deterministic_verifier",
        "candidate_status": "unverified",
        "required": True,
    }

    dpo = json.loads((DATA / "dpo_train.jsonl").read_text().splitlines()[0])
    chosen = json.loads(dpo["chosen"])
    rejected = json.loads(dpo["rejected"])
    assert set(chosen) == set(rejected)
    assert "rejection" not in rejected
    assert validate_dpo_records([dpo], source="fixture") == 1


def test_data_contract_rejects_completion_side_label_leakage() -> None:
    leaking = {
        "id": "leak",
        "city_group": "city-0",
        "prompt": "{}",
        "chosen": '{"action":"accept"}',
        "rejected": '{"action":"block","rejection":{"reason":"bad"}}',
        "rejection_reasons": ["bad"],
    }
    with pytest.raises(DataContractError, match=r"schemas differ|label-only"):
        validate_dpo_records([leaking], source="leaking-fixture")


def test_orchestration_manifest_discloses_semantic_template_overlap() -> None:
    manifest = json.loads((DATA / "orchestration_manifest.json").read_text())
    assert manifest["contract_version"] == "post-training-data-v2"
    for split_audit in manifest["split_audits"].values():
        assert split_audit["blocking_violations"] == []
        assert split_audit["city_group_overlap_count"] == 0
        assert split_audit["exact_prompt_overlap_count"] == 0
        assert split_audit["semantic_template_overlap_count"] > 0
        assert split_audit["semantic_template_holdout"] is False


def test_post_training_audit_separates_current_data_from_historical_adapters() -> None:
    result = audit()
    contracts = result["data_contracts"]
    provenance = result["lora_provenance"]
    runtime = result["runtime_connections"]
    assert isinstance(contracts, dict)
    assert isinstance(provenance, dict)
    assert isinstance(runtime, dict)
    assert contracts["all_current"] is True
    assert contracts["all_blocking_checks_pass"] is True
    assert provenance["all_match_current_data"] is False
    assert provenance["corrected_data_adapters_ready"] is False
    assert runtime["replan_linear_policy_loaded"] is True
    assert runtime["orchestration_linear_policy_loaded"] is False
    assert runtime["lora_adapter_loaded"] is False


def test_token_length_audits_expose_truncation_instead_of_silently_training() -> None:
    tokenizer = _CharacterTokenizer()
    sft_record = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "x" * 20},
            {"role": "assistant", "content": "{}"},
        ]
    }
    dpo_record = {"prompt": "x" * 20, "chosen": "{}", "rejected": '{"x":1}'}
    assert sft_token_length_audit([sft_record], tokenizer, 10)["over_max_length"] == 1
    assert dpo_token_length_audit([dpo_record], tokenizer, 10)["over_max_length"] == 2


def test_lora_evidence_preserves_hashes_across_repeated_collection() -> None:
    current_schema = {
        "training_data_sha256_at_run": "train-at-run",
        "validation_data_sha256_at_run": "validation-at-run",
    }
    assert training_snapshot_hashes({}, current_schema) == (
        "train-at-run",
        "validation-at-run",
        "historical_evidence",
    )

    legacy_schema = {
        "train_data_sha256": "legacy-train",
        "validation_data_sha256": "legacy-validation",
    }
    assert training_snapshot_hashes({}, legacy_schema) == (
        "legacy-train",
        "legacy-validation",
        "historical_evidence",
    )
