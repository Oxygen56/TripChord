from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def validate_records(path: Path) -> int:
    records = load_jsonl(path)
    for record in records:
        messages = record.get("messages")
        if not isinstance(messages, list) or [item.get("role") for item in messages] != [
            "system",
            "user",
            "assistant",
        ]:
            raise ValueError(f"invalid conversational SFT record in {path}")
        json.loads(messages[-1]["content"])
    return len(records)


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
    command.add_argument("--validate-only", action="store_true")
    return command


def main() -> None:
    args = parser().parse_args()
    counts = {
        "train": validate_records(args.train),
        "validation": validate_records(args.validation),
    }
    if args.validate_only:
        print(json.dumps({"valid": True, "counts": counts}, indent=2))
        return
    if not args.model:
        raise SystemExit("--model is required unless --validate-only is used")

    try:
        from datasets import load_dataset
        from peft import LoraConfig
        from trl import SFTConfig, SFTTrainer
    except ImportError as error:
        raise SystemExit("install training dependencies with: uv sync --extra training") from error

    dataset = load_dataset(
        "json",
        data_files={"train": str(args.train), "validation": str(args.validation)},
    )
    config = SFTConfig(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
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
        seed=20260727,
    )
    trainer = SFTTrainer(
        model=args.model,
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
    )
    trainer.train()
    trainer.save_model()


if __name__ == "__main__":
    main()
