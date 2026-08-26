"""ModelRouter adapter for the bounded personalization contract.

The personalization solver remains synchronous and deterministic.  This module
only turns the one bounded proposal into a typed ``BoundedPersonalizationAgent``
implementation; candidate eligibility and source binding are still checked by
``personalization.py`` after the model returns.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from typing import cast

from tripchord.agents.model_gateway import (
    ModelMessage,
    ModelRequest,
    ModelRouter,
    StructuredOutputError,
)
from tripchord.agents.models import AgentRole
from tripchord.planning.personalization import (
    AgentContextManifest,
    AgentProposalResult,
    AgentSelectionProposal,
    BoundedPersonalizationAgent,
)

_SYSTEM_PROMPT = (
    "你是 TripChord 个性化方案选择 Agent。"
    "你只能从输入 manifest 的 candidates 中选择一个 candidate_id；"
    "不得创造候选、金额、来源、事实或证据。"
    "必须返回一个完整 JSON，role、graph_version、candidate_id、source_refs "
    "必须逐字绑定输入；reason 只能解释取舍，不能新增旅行事实。"
)


def _request(manifest: AgentContextManifest) -> ModelRequest:
    role_instruction = {
        "budget": "从价格角度审查候选，优先较低的整趟人民币总价，同时不能违反程序检查。",
        "experience_specialist": (
            "从舒适度和出行便利角度审查候选，关注过早出发、过晚到达、换乘和已声明的舒适溢价。"
        ),
        "decision_agent": (
            "综合当前偏好和前面角色的提案，选出最适合当前请求的一个已有候选；"
            "如果取舍仍不明确，优先选择可解释的平衡点。"
        ),
    }.get(
        manifest.role.value,
        "从当前角色职责角度审查候选，并选择一个已有候选。",
    )
    payload = manifest.model_dump(mode="json")
    return ModelRequest(
        role=manifest.role,
        system=_SYSTEM_PROMPT,
        messages=(
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "manifest": payload,
                        "role_instruction": role_instruction,
                        "instruction": "选择一个已有候选并说明为什么；只输出 JSON。",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        ),
        response_schema=cast(dict[str, object], AgentSelectionProposal.model_json_schema()),
        temperature=0,
        max_tokens=1024,
        risk_level=2 if manifest.role == AgentRole.DECISION_AGENT else 1,
    )


def _decode_proposal(response: object) -> AgentSelectionProposal:
    structured = getattr(response, "structured_output", None)
    if structured is not None:
        return AgentSelectionProposal.model_validate(structured)
    text = getattr(response, "text", "")
    if not isinstance(text, str) or not text.strip():
        raise StructuredOutputError("personalization model returned empty output")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("personalization model returned invalid JSON") from exc
    return AgentSelectionProposal.model_validate(raw)


class ModelRouterPersonalizationAgent(BoundedPersonalizationAgent):
    """Synchronous facade over the async ModelRouter contract.

    ``personalize_complex_problem`` intentionally remains a pure synchronous
    solver.  A short-lived worker thread gives the existing endpoint a real
    ModelRouter call without moving solver authority into the model.
    """

    def __init__(self, router: ModelRouter) -> None:
        self._router = router

    @property
    def multi_agent_panel(self) -> bool:
        """Mark the production adapter; test doubles keep legacy semantics."""

        return True

    async def propose_async(self, manifest: AgentContextManifest) -> AgentProposalResult:
        started = perf_counter()
        routed = await self._router.complete(_request(manifest))
        response = routed.response
        proposal = _decode_proposal(response)
        return AgentProposalResult(
            proposal=proposal,
            model=str(getattr(response, "model", "unknown")),
            token_usage=int(getattr(getattr(response, "usage", None), "total_tokens", 0)),
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
        )

    def propose(self, manifest: AgentContextManifest) -> AgentProposalResult:
        started = perf_counter()

        async def complete() -> object:
            return await self._router.complete(_request(manifest))

        with ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="tripchord-personalization-model",
        ) as pool:
            response = pool.submit(asyncio.run, complete()).result()
        proposal = _decode_proposal(response)
        return AgentProposalResult(
            proposal=proposal,
            model=str(getattr(response, "model", "unknown")),
            token_usage=int(getattr(getattr(response, "usage", None), "total_tokens", 0)),
            latency_ms=max(0, round((perf_counter() - started) * 1000)),
        )


__all__ = ["ModelRouterPersonalizationAgent"]
