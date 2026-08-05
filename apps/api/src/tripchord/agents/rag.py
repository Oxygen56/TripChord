from __future__ import annotations

import json
import math
import re
from collections import Counter
from enum import StrEnum

from pydantic import Field, JsonValue

from tripchord.agents.memory import (
    MemoryAccessContext,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryStore,
    MemoryVolatility,
)
from tripchord.domain.common import DomainModel


class RagPurpose(StrEnum):
    QUERY = "query"
    PLANNER = "planner"
    REPAIR = "repair"


class RagRequest(DomainModel):
    purpose: RagPurpose
    text: str = ""
    topics: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    token_budget: int = Field(default=1_200, ge=128, le=16_000)
    limit: int = Field(default=12, ge=1, le=100)


class RagHit(DomainModel):
    memory_id: str
    memory_version: int
    kind: MemoryKind
    topic: str
    subject: str
    payload: dict[str, JsonValue]
    source: str
    confidence: float = Field(ge=0, le=1)
    approximate_tokens: int = Field(ge=1)
    retrieval_score: float = Field(default=0, ge=0)


class RagResult(DomainModel):
    purpose: RagPurpose
    hits: tuple[RagHit, ...]
    used_tokens: int = Field(ge=0)
    omitted_memory_ids: tuple[str, ...] = ()
    ranking_method: str = "bm25 lexical + scope/privacy/TTL filters"
    boundary: str = (
        "RAG 仅检索用户偏好、历史决策、平台能力和非实时证据；"
        "实时价格和库存只能从当前工具回执进入工作上下文。"
    )


_KINDS_BY_PURPOSE: dict[RagPurpose, tuple[MemoryKind, ...]] = {
    RagPurpose.QUERY: (
        MemoryKind.USER_PREFERENCE,
        MemoryKind.EPISODIC,
        MemoryKind.PROVIDER_CAPABILITY,
    ),
    RagPurpose.PLANNER: (
        MemoryKind.USER_PREFERENCE,
        MemoryKind.EPISODIC,
        MemoryKind.PROVIDER_CAPABILITY,
        MemoryKind.EVIDENCE,
    ),
    RagPurpose.REPAIR: (
        MemoryKind.USER_PREFERENCE,
        MemoryKind.EPISODIC,
        MemoryKind.PROVIDER_CAPABILITY,
        MemoryKind.EVIDENCE,
    ),
}

_PRICE_MARKERS = (
    "price",
    "fare",
    "quote",
    "inventory",
    "价格",
    "报价",
    "票价",
    "库存",
)

_SENSITIVE_PRICE_KEYS = (
    "price",
    "fare",
    "quote",
    "inventory",
    "amount",
    "total_cents",
    "total_for_party_cents",
    "价格",
    "报价",
    "票价",
    "库存",
    "金额",
)
_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u3400-\u9fff]+", re.IGNORECASE)


class EvidenceRagRetriever:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def retrieve(
        self,
        request: RagRequest,
        access: MemoryAccessContext,
    ) -> RagResult:
        candidates = self._store.query(
            MemoryQuery(
                kinds=_KINDS_BY_PURPOSE[request.purpose],
                topics=request.topics,
                tags=request.tags,
                fresh_only=True,
                rag_only=True,
                limit=min(200, max(request.limit * 4, request.limit)),
            ),
            access,
        )
        hits: list[RagHit] = []
        omitted: list[str] = []
        used_tokens = 0
        ranked = self._rank_bm25(candidates, request)
        for record, retrieval_score in ranked:
            if not self._safe_for_rag(record):
                omitted.append(record.id)
                continue
            estimated = record.approximate_tokens
            if len(hits) >= request.limit or used_tokens + estimated > request.token_budget:
                omitted.append(record.id)
                continue
            hits.append(
                RagHit(
                    memory_id=record.id,
                    memory_version=record.version,
                    kind=record.kind,
                    topic=record.topic,
                    subject=record.subject,
                    payload=record.payload,
                    source=record.source,
                    confidence=record.confidence,
                    approximate_tokens=estimated,
                    retrieval_score=retrieval_score,
                )
            )
            used_tokens += estimated
        return RagResult(
            purpose=request.purpose,
            hits=tuple(hits),
            used_tokens=used_tokens,
            omitted_memory_ids=tuple(omitted),
        )

    @staticmethod
    def _safe_for_rag(record: MemoryRecord) -> bool:
        if (
            record.tainted
            or record.volatility == MemoryVolatility.REALTIME
            or not record.rag_eligible
        ):
            return False
        searchable = f"{record.topic} {record.subject}".casefold()
        if any(marker in searchable for marker in _PRICE_MARKERS):
            return False
        if record.kind == MemoryKind.USER_PREFERENCE:
            return True
        serialized_payload = json.dumps(
            record.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).casefold()
        return not any(marker in serialized_payload for marker in _SENSITIVE_PRICE_KEYS)

    @classmethod
    def _rank_bm25(
        cls,
        records: tuple[MemoryRecord, ...],
        request: RagRequest,
    ) -> tuple[tuple[MemoryRecord, float], ...]:
        if not records:
            return ()
        query_tokens = cls._tokens(" ".join((request.text, *request.topics, *request.tags)))
        if not query_tokens:
            return tuple((record, 0.0) for record in records)
        documents = [
            cls._tokens(
                " ".join(
                    (
                        record.topic,
                        record.subject,
                        *record.tags,
                        json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
                    )
                )
            )
            for record in records
        ]
        document_frequencies = Counter(token for document in documents for token in set(document))
        average_length = sum(map(len, documents)) / max(1, len(documents))
        k1 = 1.2
        b = 0.75
        scored: list[tuple[MemoryRecord, float]] = []
        for record, document in zip(records, documents, strict=True):
            frequencies = Counter(document)
            score = 0.0
            for token in query_tokens:
                term_frequency = frequencies[token]
                if not term_frequency:
                    continue
                document_frequency = document_frequencies[token]
                inverse_frequency = math.log(
                    1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                normalizer = term_frequency + k1 * (
                    1 - b + b * len(document) / max(1.0, average_length)
                )
                score += inverse_frequency * term_frequency * (k1 + 1) / normalizer
            score += 0.25 * record.confidence
            scored.append((record, score))
        scored.sort(
            key=lambda item: (
                -item[1],
                -item[0].confidence,
                item[0].topic,
                item[0].subject,
                item[0].id,
            )
        )
        return tuple(scored)

    @staticmethod
    def _tokens(value: str) -> tuple[str, ...]:
        tokens: list[str] = []
        for chunk in _TOKEN_PATTERN.findall(value.casefold()):
            if any("\u3400" <= character <= "\u9fff" for character in chunk):
                tokens.extend(chunk)
                tokens.extend(chunk[index : index + 2] for index in range(max(0, len(chunk) - 1)))
            else:
                tokens.append(chunk)
        return tuple(tokens)
