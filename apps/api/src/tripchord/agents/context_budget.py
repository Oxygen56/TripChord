from __future__ import annotations

import json
from enum import StrEnum

from pydantic import Field, JsonValue, model_validator

from tripchord.agents.memory import MemoryAccessContext
from tripchord.agents.models import AgentRole, EvidenceRecord
from tripchord.agents.rag import EvidenceRagRetriever, RagPurpose, RagRequest
from tripchord.domain.common import DomainModel


class ContextPurpose(StrEnum):
    QUERY = "query"
    PLANNER = "planner"
    REPAIR = "repair"


class ContextItemKind(StrEnum):
    CURRENT_REQUEST = "current_request"
    CURRENT_EVIDENCE = "current_evidence"
    RETRIEVED_MEMORY = "retrieved_memory"


class AgentContextItem(DomainModel):
    id: str = Field(min_length=1)
    kind: ContextItemKind
    topic: str = Field(min_length=1)
    payload: dict[str, JsonValue]
    source: str = Field(min_length=1)
    approximate_tokens: int = Field(ge=1)
    critical: bool = False
    confidence: float = Field(default=1, ge=0, le=1)


class BudgetedAgentContextPack(DomainModel):
    role: AgentRole
    purpose: ContextPurpose
    goal: str = Field(min_length=1)
    items: tuple[AgentContextItem, ...]
    included_refs: tuple[str, ...]
    omitted_refs: tuple[str, ...] = ()
    token_budget: int = Field(ge=128)
    used_tokens: int = Field(ge=0)
    tool_observation_token_reserve: int = Field(default=0, ge=0)
    boundary: str = (
        "当前请求与当前工具证据优先；历史记忆按用户/行程/会话作用域裁剪。"
        "任何实时价格都不从 RAG 恢复，必须使用当前报价回执。"
        "token_budget 同时约束初始证据/记忆与后续工具观察；"
        "系统提示、任务 schema 和模型输出不属于该检索上下文预算。"
    )

    @model_validator(mode="after")
    def validate_budget_partition(self) -> BudgetedAgentContextPack:
        if self.used_tokens + self.tool_observation_token_reserve > self.token_budget:
            raise ValueError("initial context plus tool reserve exceeds token budget")
        return self


class AgentContextBudgets(DomainModel):
    # Query Strategy needs enough room for both scoped preference/RAG context
    # and a complete compact date frontier returned after its read-only tool
    # call.  At 2,400 tokens the normal one-month frontier keeps a guaranteed
    # 600-token observation slice instead of truncating the third selectable ID.
    query_tokens: int = Field(default=2_400, ge=256, le=32_000)
    planner_tokens: int = Field(default=4_000, ge=256, le=32_000)
    repair_tokens: int = Field(default=3_000, ge=256, le=32_000)

    def for_purpose(self, purpose: ContextPurpose) -> int:
        return {
            ContextPurpose.QUERY: self.query_tokens,
            ContextPurpose.PLANNER: self.planner_tokens,
            ContextPurpose.REPAIR: self.repair_tokens,
        }[purpose]


class BudgetedAgentContextBuilder:
    """Build role-specific packs without mixing durable RAG and live quotes."""

    def __init__(
        self,
        retriever: EvidenceRagRetriever,
        *,
        budgets: AgentContextBudgets | None = None,
    ) -> None:
        self._retriever = retriever
        self._budgets = budgets or AgentContextBudgets()

    def build(
        self,
        *,
        role: AgentRole,
        purpose: ContextPurpose,
        goal: str,
        access: MemoryAccessContext,
        current_request: dict[str, JsonValue],
        current_evidence: tuple[EvidenceRecord, ...] = (),
        critical_evidence_refs: tuple[str, ...] = (),
        rag_text: str = "",
        rag_topics: tuple[str, ...] = (),
        rag_tags: tuple[str, ...] = (),
    ) -> BudgetedAgentContextPack:
        if access.agent_role is not None and access.agent_role != role:
            raise PermissionError("context access role does not match the target Agent role")
        budget = self._budgets.for_purpose(purpose)
        # Keep a hard slice of the same context-item budget for observations
        # returned after the first model turn.  Without this reservation, RAG
        # can consume the full allowance and every tool receipt is appended
        # outside the advertised budget.
        # Explanation relies on its role-specific final-handoff tool more than
        # on broad historical context.  Reserve enough room for the complete
        # compact component/evidence index; otherwise a full initial RAG pack
        # can still truncate the authoritative handoff by a few fields.
        if role == AgentRole.EXPLANATION:
            # The final handoff is the authoritative component/evidence map.
            # A real four-component Round8 receipt measured about 2,036 of
            # these conservative context units, so 1,600 still truncated the
            # fields Explanation must bind. Keep 60% for that observation.
            tool_reserve = min(2_400, max(128, budget * 3 // 5))
        else:
            tool_reserve = min(1_024, max(128, budget // 4))
        initial_budget = budget - tool_reserve
        included: list[AgentContextItem] = []
        omitted: list[str] = []
        used = 0

        request_item = self._item(
            item_id="current-request",
            kind=ContextItemKind.CURRENT_REQUEST,
            topic="current_request",
            payload=current_request,
            source="user:current_request",
            critical=True,
        )
        if request_item.approximate_tokens > initial_budget:
            raise ValueError("current request exceeds the initial context budget")
        included.append(request_item)
        used += request_item.approximate_tokens

        critical_ids = set(critical_evidence_refs)
        fresh_evidence = tuple(item for item in current_evidence if item.is_fresh())
        fresh_ids = {item.id for item in fresh_evidence}
        if missing_critical := critical_ids - fresh_ids:
            raise ValueError(f"critical evidence is missing or stale: {sorted(missing_critical)}")
        ordered_evidence = tuple(
            sorted(
                fresh_evidence,
                key=lambda item: (
                    item.id not in critical_ids,
                    -item.confidence,
                    item.topic,
                    item.subject,
                ),
            )
        )
        for record in ordered_evidence:
            item = self._item(
                item_id=record.id,
                kind=ContextItemKind.CURRENT_EVIDENCE,
                topic=record.topic,
                payload=record.payload,
                source=record.source,
                critical=record.id in critical_ids,
                confidence=record.confidence,
            )
            if used + item.approximate_tokens > initial_budget:
                if item.critical:
                    raise ValueError(f"critical evidence {record.id} exceeds context budget")
                omitted.append(record.id)
                continue
            included.append(item)
            used += item.approximate_tokens

        remaining = initial_budget - used
        if remaining >= 128:
            rag_result = self._retriever.retrieve(
                RagRequest(
                    purpose=RagPurpose(purpose.value),
                    text=rag_text,
                    topics=rag_topics,
                    tags=rag_tags,
                    token_budget=remaining,
                ),
                access,
            )
            for hit in rag_result.hits:
                included.append(
                    AgentContextItem(
                        id=hit.memory_id,
                        kind=ContextItemKind.RETRIEVED_MEMORY,
                        topic=hit.topic,
                        payload=hit.payload,
                        source=hit.source,
                        approximate_tokens=hit.approximate_tokens,
                        confidence=hit.confidence,
                    )
                )
                used += hit.approximate_tokens
            omitted.extend(rag_result.omitted_memory_ids)

        refs = tuple(item.id for item in included)
        return BudgetedAgentContextPack(
            role=role,
            purpose=purpose,
            goal=goal,
            items=tuple(included),
            included_refs=refs,
            omitted_refs=tuple(dict.fromkeys(omitted)),
            token_budget=budget,
            used_tokens=used,
            tool_observation_token_reserve=tool_reserve,
        )

    @staticmethod
    def _item(
        *,
        item_id: str,
        kind: ContextItemKind,
        topic: str,
        payload: dict[str, JsonValue],
        source: str,
        critical: bool,
        confidence: float = 1,
    ) -> AgentContextItem:
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return AgentContextItem(
            id=item_id,
            kind=kind,
            topic=topic,
            payload=payload,
            source=source,
            approximate_tokens=max(1, len(serialized) // 4),
            critical=critical,
            confidence=confidence,
        )
