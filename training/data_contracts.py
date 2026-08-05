from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter

CONTRACT_VERSION = "post-training-data-v2"
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_FORBIDDEN_COMPLETION_KEYS = frozenset(
    {
        "expected_repair",
        "expected_state",
        "label_source",
        "oracle_action",
        "rejection",
        "rejection_reasons",
    }
)


class DataContractError(ValueError):
    pass


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(value: str, *, field: str, source: str) -> dict[str, JsonValue]:
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DataContractError(f"{source}: {field} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DataContractError(f"{source}: {field} must be a JSON object")
    return _JSON_OBJECT.validate_python(parsed)


def _forbidden_keys(value: JsonValue) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_COMPLETION_KEYS:
                found.add(key)
            found.update(_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_keys(child))
    return found


def _record_identity(record: Mapping[str, Any], *, source: str) -> tuple[str, str]:
    record_id = record.get("id")
    city_group = record.get("city_group")
    if not isinstance(record_id, str) or not record_id:
        raise DataContractError(f"{source}: record id is required")
    if not isinstance(city_group, str) or not city_group:
        raise DataContractError(f"{source}: city_group is required")
    return record_id, city_group


def validate_sft_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> int:
    if not records:
        raise DataContractError(f"{source}: SFT dataset is empty")
    seen_ids: set[str] = set()
    for record in records:
        record_id, _ = _record_identity(record, source=source)
        if record_id in seen_ids:
            raise DataContractError(f"{source}: duplicate id {record_id}")
        seen_ids.add(record_id)
        messages = record.get("messages")
        if not isinstance(messages, list) or len(messages) != 3:
            raise DataContractError(f"{source}: {record_id} requires exactly three messages")
        roles = [item.get("role") if isinstance(item, dict) else None for item in messages]
        if roles != ["system", "user", "assistant"]:
            raise DataContractError(f"{source}: {record_id} has invalid message roles")
        contents = [item.get("content") if isinstance(item, dict) else None for item in messages]
        if not all(isinstance(item, str) and item for item in contents):
            raise DataContractError(f"{source}: {record_id} has an empty message")
        assistant = _json_object(str(contents[-1]), field="assistant", source=source)
        forbidden = _forbidden_keys(assistant)
        if forbidden:
            raise DataContractError(
                f"{source}: {record_id} completion contains label-only keys {sorted(forbidden)}"
            )
        raw_verification = assistant.get("verification")
        if isinstance(raw_verification, dict) and "verdict" in raw_verification:
            raise DataContractError(
                f"{source}: {record_id} lets the learned policy self-certify verification"
            )
    return len(records)


def validate_dpo_records(
    records: Sequence[Mapping[str, Any]],
    *,
    source: str,
) -> int:
    if not records:
        raise DataContractError(f"{source}: DPO dataset is empty")
    seen_ids: set[str] = set()
    for record in records:
        record_id, _ = _record_identity(record, source=source)
        if record_id in seen_ids:
            raise DataContractError(f"{source}: duplicate id {record_id}")
        seen_ids.add(record_id)
        prompt = record.get("prompt")
        chosen_raw = record.get("chosen")
        rejected_raw = record.get("rejected")
        if not all(isinstance(item, str) and item for item in (prompt, chosen_raw, rejected_raw)):
            raise DataContractError(f"{source}: {record_id} has invalid prompt/pair fields")
        chosen = _json_object(str(chosen_raw), field="chosen", source=source)
        rejected = _json_object(str(rejected_raw), field="rejected", source=source)
        if chosen == rejected:
            raise DataContractError(f"{source}: {record_id} chosen and rejected are identical")
        if set(chosen) != set(rejected):
            raise DataContractError(
                f"{source}: {record_id} chosen/rejected schemas differ; this leaks the label"
            )
        forbidden = _forbidden_keys(chosen) | _forbidden_keys(rejected)
        if forbidden:
            raise DataContractError(
                f"{source}: {record_id} completion contains label-only keys {sorted(forbidden)}"
            )
        reasons = record.get("rejection_reasons")
        if not isinstance(reasons, list) or not reasons or not all(
            isinstance(item, str) and item for item in reasons
        ):
            raise DataContractError(f"{source}: {record_id} lacks external rejection evidence")
    return len(records)


def _prompt_for(record: Mapping[str, Any], kind: str) -> str:
    if kind == "sft":
        messages = record.get("messages")
        if not isinstance(messages, list) or len(messages) < 2 or not isinstance(messages[1], dict):
            return ""
        content = messages[1].get("content")
        return content if isinstance(content, str) else ""
    prompt = record.get("prompt")
    return prompt if isinstance(prompt, str) else ""


def orchestration_semantic_fingerprint(prompt: str) -> str:
    """Fingerprint label-relevant structure while removing city/date nuisance fields."""

    try:
        parsed: Any = json.loads(prompt)
    except json.JSONDecodeError:
        return hashlib.sha256(prompt.encode()).hexdigest()
    if not isinstance(parsed, dict):
        return hashlib.sha256(prompt.encode()).hexdigest()
    payload = {
        "preferences": parsed.get("preferences"),
        "signals": parsed.get("signals"),
        "contract": parsed.get("contract"),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def audit_split_contract(
    datasets: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    kind: str,
    semantic_fingerprint: Callable[[str], str] | None = None,
) -> dict[str, JsonValue]:
    if kind not in {"sft", "dpo"}:
        raise ValueError("kind must be sft or dpo")
    split_names = tuple(sorted(datasets))
    groups: dict[str, set[str]] = {}
    ids: dict[str, set[str]] = {}
    prompt_hashes: dict[str, set[str]] = {}
    semantic_hashes: dict[str, set[str]] = {}
    for split, records in datasets.items():
        validator = validate_sft_records if kind == "sft" else validate_dpo_records
        validator(records, source=f"{kind}:{split}")
        groups[split] = {str(record["city_group"]) for record in records}
        ids[split] = {str(record["id"]) for record in records}
        prompts = [_prompt_for(record, kind) for record in records]
        prompt_hashes[split] = {hashlib.sha256(prompt.encode()).hexdigest() for prompt in prompts}
        if semantic_fingerprint is not None:
            semantic_hashes[split] = {semantic_fingerprint(prompt) for prompt in prompts}
    group_overlaps: set[str] = set()
    id_overlaps: set[str] = set()
    exact_prompt_overlaps: set[str] = set()
    semantic_overlaps: set[str] = set()
    for index, left in enumerate(split_names):
        for right in split_names[index + 1 :]:
            group_overlaps.update(groups[left] & groups[right])
            id_overlaps.update(ids[left] & ids[right])
            exact_prompt_overlaps.update(prompt_hashes[left] & prompt_hashes[right])
            if semantic_fingerprint is not None:
                semantic_overlaps.update(semantic_hashes[left] & semantic_hashes[right])
    blocking: list[str] = []
    if group_overlaps:
        blocking.append("city_group_overlap")
    if id_overlaps:
        blocking.append("record_id_overlap")
    if exact_prompt_overlaps:
        blocking.append("exact_prompt_overlap")
    result = {
        "contract_version": CONTRACT_VERSION,
        "kind": kind,
        "splits": {split: len(datasets[split]) for split in split_names},
        "city_group_overlap_count": len(group_overlaps),
        "record_id_overlap_count": len(id_overlaps),
        "exact_prompt_overlap_count": len(exact_prompt_overlaps),
        "semantic_template_overlap_count": len(semantic_overlaps),
        "semantic_template_holdout": semantic_fingerprint is not None and not semantic_overlaps,
        "blocking_violations": blocking,
    }
    if blocking:
        raise DataContractError(f"split contract failed: {blocking}")
    return _JSON_OBJECT.validate_python(result)
