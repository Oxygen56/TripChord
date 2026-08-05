from __future__ import annotations

import json
from datetime import UTC, datetime

from tripchord.agents.models import AgentRole, AgentTask, ContextPack, EvidenceRecord


class EvidenceBlackboard:
    def __init__(self) -> None:
        self._records: dict[str, EvidenceRecord] = {}

    def add(self, record: EvidenceRecord) -> None:
        current = self.latest(record.topic, record.subject)
        if current is not None and record.version <= current.version:
            raise ValueError(f"evidence version must increase for {record.topic}/{record.subject}")
        missing = set(record.dependencies) - set(self._records)
        if missing:
            raise ValueError(f"evidence has unknown dependencies: {sorted(missing)}")
        self._records[record.id] = record

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return self._records.get(evidence_id)

    def latest(self, topic: str, subject: str) -> EvidenceRecord | None:
        matches = [
            record
            for record in self._records.values()
            if record.topic == topic and record.subject == subject
        ]
        return max(matches, key=lambda record: record.version, default=None)

    def query(
        self,
        topics: tuple[str, ...] = (),
        *,
        fresh_only: bool = True,
        now: datetime | None = None,
    ) -> tuple[EvidenceRecord, ...]:
        reference = now or datetime.now(UTC)
        records = [
            record
            for record in self._records.values()
            if (not topics or record.topic in topics)
            and (not fresh_only or record.is_fresh(reference))
        ]
        latest: dict[tuple[str, str], EvidenceRecord] = {}
        for record in records:
            key = (record.topic, record.subject)
            if key not in latest or record.version > latest[key].version:
                latest[key] = record
        return tuple(
            sorted(
                latest.values(),
                key=lambda item: (-item.confidence, item.topic, item.subject, -item.version),
            )
        )

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records.values())


class ContextEngine:
    def __init__(self, blackboard: EvidenceBlackboard) -> None:
        self._blackboard = blackboard

    def build_pack(
        self,
        task: AgentTask,
        *,
        token_budget: int = 4_000,
        now: datetime | None = None,
    ) -> ContextPack:
        selected: list[EvidenceRecord] = []
        omitted: list[str] = []
        used_tokens = 0
        for record in self._blackboard.query(task.context_topics, now=now):
            estimated = self._estimate_tokens(record)
            if used_tokens + estimated > token_budget:
                omitted.append(record.id)
                continue
            selected.append(record)
            used_tokens += estimated
        return ContextPack(
            task_id=task.id,
            role=task.role,
            goal=task.goal,
            evidence=tuple(selected),
            evidence_refs=tuple(record.id for record in selected),
            omitted_evidence_refs=tuple(omitted),
            approximate_tokens=used_tokens,
        )

    def add_agent_evidence(self, records: tuple[EvidenceRecord, ...]) -> None:
        for record in records:
            self._blackboard.add(record)

    def _estimate_tokens(self, record: EvidenceRecord) -> int:
        serialized = json.dumps(record.model_dump(mode="json"), ensure_ascii=False)
        return max(1, len(serialized) // 4)


def empty_context(task_id: str, role: AgentRole, goal: str) -> ContextPack:
    return ContextPack(
        task_id=task_id,
        role=role,
        goal=goal,
        evidence=(),
        evidence_refs=(),
    )
