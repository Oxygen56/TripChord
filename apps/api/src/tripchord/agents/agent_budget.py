from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from functools import wraps

from pydantic import Field

from tripchord.agents.adaptive_control import LOGICAL_AGENT_HARD_CAP
from tripchord.agents.models import AgentRole
from tripchord.domain.common import DomainModel


class AgentBudgetExceeded(RuntimeError):
    """Raised before a model Agent starts when the request-wide cap is exhausted."""


class AgentBudgetAdmission(DomainModel):
    sequence: int = Field(ge=1)
    task_id: str = Field(min_length=1)
    role: AgentRole


class AgentBudgetAudit(DomainModel):
    limit: int = Field(default=LOGICAL_AGENT_HARD_CAP, ge=1, le=LOGICAL_AGENT_HARD_CAP)
    admitted_count: int = Field(ge=0, le=LOGICAL_AGENT_HARD_CAP)
    rejected_count: int = Field(ge=0)
    remaining_count: int = Field(ge=0, le=LOGICAL_AGENT_HARD_CAP)
    admissions: tuple[AgentBudgetAdmission, ...] = ()
    rejected_task_ids: tuple[str, ...] = ()


class AgentBudgetLedger:
    """One request-wide logical model-Agent ledger shared through ContextVar."""

    def __init__(self, limit: int = LOGICAL_AGENT_HARD_CAP) -> None:
        if limit < 1 or limit > LOGICAL_AGENT_HARD_CAP:
            raise ValueError("logical model Agent limit must be between 1 and 96")
        self._limit = limit
        self._lock = asyncio.Lock()
        self._admissions: list[AgentBudgetAdmission] = []
        self._rejected_task_ids: list[str] = []

    async def admit(self, task_id: str, role: AgentRole) -> AgentBudgetAdmission:
        async with self._lock:
            if len(self._admissions) >= self._limit:
                self._rejected_task_ids.append(task_id)
                raise AgentBudgetExceeded(
                    f"request-wide logical model Agent cap {self._limit} exhausted"
                )
            admission = AgentBudgetAdmission(
                sequence=len(self._admissions) + 1,
                task_id=task_id,
                role=role,
            )
            self._admissions.append(admission)
            return admission

    def audit(self) -> AgentBudgetAudit:
        admitted = len(self._admissions)
        return AgentBudgetAudit(
            limit=self._limit,
            admitted_count=admitted,
            rejected_count=len(self._rejected_task_ids),
            remaining_count=self._limit - admitted,
            admissions=tuple(self._admissions),
            rejected_task_ids=tuple(self._rejected_task_ids),
        )


_CURRENT_AGENT_BUDGET: ContextVar[AgentBudgetLedger | None] = ContextVar(
    "tripchord_agent_budget",
    default=None,
)


def current_agent_budget() -> AgentBudgetLedger | None:
    return _CURRENT_AGENT_BUDGET.get()


@contextmanager
def bind_agent_budget(ledger: AgentBudgetLedger) -> Iterator[AgentBudgetLedger]:
    token: Token[AgentBudgetLedger | None] = _CURRENT_AGENT_BUDGET.set(ledger)
    try:
        yield ledger
    finally:
        _CURRENT_AGENT_BUDGET.reset(token)


def request_agent_budgeted[**P, R](
    function: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Reuse an outer ledger or create exactly one for a top-level async run."""

    @wraps(function)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if current_agent_budget() is not None:
            return await function(*args, **kwargs)
        with bind_agent_budget(AgentBudgetLedger()):
            return await function(*args, **kwargs)

    return wrapped
