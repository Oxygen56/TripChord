from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from training.data_contracts import (
    CONTRACT_VERSION,
    audit_split_contract,
    read_jsonl,
    sha256,
    validate_sft_records,
)

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_jsonl(path)


def validate_records(path: Path) -> int:
    records = load_jsonl(path)
    return validate_sft_records(records, source=str(path))


def token_length_audit(
    records: list[dict[str, Any]],
    tokenizer: Any,
    max_length: int,
) -> dict[str, int]:
    lengths: list[int] = []
    for record in records:
        rendered = tokenizer.apply_chat_template(record["messages"], tokenize=False)
        lengths.append(len(tokenizer.encode(rendered, add_special_tokens=False)))
    return {
        "records": len(lengths),
        "minimum_tokens": min(lengths),
        "maximum_tokens": max(lengths),
        "over_max_length": sum(length > max_length for length in lengths),
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="LoRA SFT for TripChord planning traces")
    command.add_argument("--model", help="Hugging Face causal language model id")
    command.add_argument("--train", type=Path, default=ROOT / "training/data/sft_train.jsonl")
    command.add_argument(
        "--validation",
        type=Path,
        default=ROOT / "training/data/sft_validation.jsonl",
    )
    command.add_argument("--output", type=Path, default=ROOT / "training/runs/sft-lora")
    command.add_argument("--epochs", type=float, default=2.0)
    command.add_argument("--max-length", type=int, default=8192)
    command.add_argument(
        "--max-steps",
        type=int,
        default=-1,
        help="Bounded smoke/evidence runs; -1 uses the epoch schedule.",
    )
    command.add_argument(
        "--use-cpu",
        action="store_true",
        help="Force CPU execution when an accelerator backend is unavailable or unstable.",
    )
    command.add_argument("--validate-only", action="store_true")
    command.add_argument(
        "--allow-truncation",
        action="store_true",
        help="Explicitly allow records beyond --max-length; disabled by default.",
    )
    return command


def main() -> None:
    args = parser().parse_args()
    counts = {
        "train": validate_records(args.train),
        "validation": validate_records(args.validation),
    }
    split_audit = audit_split_contract(
        {
            "train": load_jsonl(args.train),
            "validation": load_jsonl(args.validation),
        },
        kind="sft",
    )
    if args.validate_only:
        print(
            json.dumps(
                {
                    "valid": True,
                    "contract_version": CONTRACT_VERSION,
                    "counts": counts,
                    "split_audit": split_audit,
                },
                indent=2,
            )
        )
        return
    if not args.model:
        raise SystemExit("--model is required unless --validate-only is used")

    try:
        import torch
        from datasets import load_dataset  # type: ignore[import-untyped]
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer  # type: ignore[attr-defined]
    except ImportError as error:
        raise SystemExit("install training dependencies with: uv sync --extra training") from error

    tokenizer = AutoTokenizer.from_pretrained(args.model)
    length_audits = {
        "train": token_length_audit(load_jsonl(args.train), tokenizer, args.max_length),
        "validation": token_length_audit(
            load_jsonl(args.validation), tokenizer, args.max_length
        ),
    }
    over_limit = sum(audit["over_max_length"] for audit in length_audits.values())
    if over_limit and not args.allow_truncation:
        raise SystemExit(
            f"token length contract failed: {over_limit} records exceed --max-length; "
            "raise the limit or explicitly pass --allow-truncation"
        )
    dataset = load_dataset(
        "json",
        data_files={"train": str(args.train), "validation": str(args.validation)},
    )
    config = SFTConfig(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        max_length=args.max_length,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=8,
        learning_rate=2e-4,
        eval_strategy="steps",
        eval_steps=50,
        save_steps=50,
        logging_steps=5,
        report_to="none",
        use_cpu=args.use_cpu,
        seed=20260727,
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        dtype=torch.float32 if args.use_cpu else "auto",
    )
    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=dataset["train"],
        eval_dataset=dataset["validation"],
        peft_config=LoraConfig(
            r=16,
            lora_alpha=32,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules="all-linear",
        ),
        processing_class=tokenizer,
    )
    train_result = trainer.train()
    trainer.save_model()
    eval_metrics = trainer.evaluate()
    metrics = {
        "model": args.model,
        "train_records": counts["train"],
        "validation_records": counts["validation"],
        "train_data_sha256": sha256(args.train),
        "validation_data_sha256": sha256(args.validation),
        "data_contract_version": CONTRACT_VERSION,
        "split_audit": split_audit,
        "token_length_audit": length_audits,
        "truncation_allowed": args.allow_truncation,
        "max_length": args.max_length,
        "max_steps": args.max_steps,
        "device": "cpu" if args.use_cpu else "auto",
        "train": train_result.metrics,
        "validation": eval_metrics,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "tripchord_training_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str) + "\n"
    )


if __name__ == "__main__":
    main()
