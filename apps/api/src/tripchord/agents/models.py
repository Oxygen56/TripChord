from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import Field, JsonValue, model_validator

from tripchord.domain.common import DomainModel
from tripchord.domain.preferences import PreferenceMode as PreferenceMode


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CONTEXT = "context"
    QUERY_STRATEGIST = "query_strategist"
    SEARCH_SUPERVISOR = "search_supervisor"
    TRANSPORT = "transport"
    LODGING = "lodging"
    POI = "poi"
    WEATHER = "weather"
    BUDGET = "budget"
    PREFERENCE_GUARD = "preference_guard"
    BROWSER_RESEARCH = "browser_research"
    NEURAL_PLANNER = "neural_planner"
    CP_SAT_PLANNER = "cp_sat_planner"
    CRITIC = "critic"
    EVIDENCE_ARBITER = "evidence_arbiter"
    CANDIDATE_GENERATOR = "candidate_generator"
    CANDIDATE_CURATOR = "candidate_curator"
    HARD_VERIFIER = "hard_verifier"
    RISK_CRITIC = "risk_critic"
    RECRITIC = "recritic"
    EVENT_DIAGNOSER = "event_diagnoser"
    REPAIR_STRATEGIST = "repair_strategist"
    REPAIR = "repair"
    SAFETY_GATE = "safety_gate"
    EXPLANATION = "explanation"
    MEMORY_CURATOR = "memory_curator"
    DECISION_AGENT = "decision_agent"
    EXPERIENCE_SPECIALIST = "experience_specialist"
    EXECUTOR = "executor"
    RECEIPT_VERIFIER = "receipt_verifier"


class DecisionState(StrEnum):
    ACCEPT = "accept"
    ACCEPT_WITH_EXCEPTION = "accept_with_exception"
    REPLAN_OR_BLOCK = "replan_or_block"

    @property
    def chinese_label(self) -> str:
        return {
            self.ACCEPT: "直接接受",
            self.ACCEPT_WITH_EXCEPTION: "确认例外后接受",
            self.REPLAN_OR_BLOCK: "重新规划或暂停",
        }[self]


class PreferenceSource(StrEnum):
    EXPLICIT_CURRENT_TRIP = "explicit_current_trip"
    EXPLICIT_LONG_TERM = "explicit_long_term"
    INFERRED_CURRENT_CONTEXT = "inferred_current_context"
    AGENT_DEFAULT = "agent_default"

    @property
    def priority(self) -> int:
        return {
            self.EXPLICIT_CURRENT_TRIP: 40,
            self.EXPLICIT_LONG_TERM: 30,
            self.INFERRED_CURRENT_CONTEXT: 20,
            self.AGENT_DEFAULT: 10,
        }[self]


class ToolPermission(IntEnum):
    PURE_COMPUTE = 0
    READ_ONLY_EXTERNAL = 1
    REVERSIBLE_WRITE = 2
    HIGH_IMPACT = 3
    FORBIDDEN = 4

    @property
    def chinese_label(self) -> str:
        return {
            self.PURE_COMPUTE: "纯计算",
            self.READ_ONLY_EXTERNAL: "只读外部查询",
            self.REVERSIBLE_WRITE: "可恢复写操作",
            self.HIGH_IMPACT: "高影响操作",
            self.FORBIDDEN: "禁止操作",
        }[self]


class PreferenceRule(DomainModel):
    key: str = Field(min_length=1)
    mode: PreferenceMode
    weight: float = Field(default=0.5, ge=0, le=1)
    expected: JsonValue | None = None
    source: PreferenceSource
    scope: str = Field(default="current_trip", min_length=1)
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PreferenceConstitution(DomainModel):
    rules: tuple[PreferenceRule, ...] = ()

    def effective(self, key: str) -> PreferenceRule | None:
        matches = [rule for rule in self.rules if rule.key == key]
        if not matches:
            return None
        return max(matches, key=lambda rule: (rule.source.priority, rule.created_at))

    def effective_rules(self) -> tuple[PreferenceRule, ...]:
        keys = sorted({rule.key for rule in self.rules})
        return tuple(rule for key in keys if (rule := self.effective(key)) is not None)

    def merged_for_trip(
        self,
        *,
        current: PreferenceConstitution | None = None,
    ) -> PreferenceConstitution:
        """Merge durable preferences without allowing them to override this trip.

        Explicit current-trip rules win by source priority.  Inferred rules
        are retained only when no explicit current or durable rule exists, so
        a user can revise or revoke a durable preference deterministically.
        """
        durable = list(self.rules)
        if current is not None:
            durable.extend(current.rules)
        return PreferenceConstitution(rules=tuple(durable)).model_copy(
            update={"rules": tuple(PreferenceConstitution(rules=tuple(durable)).effective_rules())}
        )


class EvidenceRecord(DomainModel):
    id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    payload: dict[str, JsonValue]
    source: str = Field(min_length=1)
    captured_at: datetime
    expires_at: datetime | None = None
    confidence: float = Field(default=1, ge=0, le=1)
    owner_agent: AgentRole
    version: int = Field(default=1, ge=1)
    dependencies: tuple[str, ...] = ()
    token_cost: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_timestamps(self) -> EvidenceRecord:
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.expires_at is not None:
            if self.expires_at.tzinfo is None:
                raise ValueError("expires_at must be timezone-aware")
            if self.expires_at <= self.captured_at:
                raise ValueError("expires_at must be after captured_at")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.expires_at is None or self.expires_at > reference


class ContextPack(DomainModel):
    task_id: str
    role: AgentRole
    goal: str
    evidence: tuple[EvidenceRecord, ...]
    evidence_refs: tuple[str, ...]
    omitted_evidence_refs: tuple[str, ...] = ()
    approximate_tokens: int = Field(default=0, ge=0)
    built_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DependencyPolicy(StrEnum):
    """How a task's dependencies must finish before it becomes runnable.

    ``ALL_SUCCEEDED`` is the historical semantics: every dependency must
    succeed.  ``ALL_TERMINAL`` (v0.3) releases the barrier (settle node) once
    every dependency reached a typed terminal result — success or a real typed
    failure such as ``login_required`` / ``timed_out`` / ``cancelled`` — but
    never for a ``dependency_blocked`` placeholder that never actually ran.
    """

    ALL_SUCCEEDED = "all_succeeded"
    ALL_TERMINAL = "all_terminal"


class AgentTask(DomainModel):
    id: str = Field(min_length=1)
    role: AgentRole
    goal: str = Field(min_length=1)
    context_topics: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    dependency_policy: DependencyPolicy = DependencyPolicy.ALL_SUCCEEDED
    input: dict[str, JsonValue] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=2, ge=1, le=10)


class TaskGraph(DomainModel):
    tasks: tuple[AgentTask, ...]

    @model_validator(mode="after")
    def validate_graph(self) -> TaskGraph:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            missing = set(task.dependencies) - known
            if missing:
                raise ValueError(f"task {task.id} has unknown dependencies: {sorted(missing)}")
            if task.id in task.dependencies:
                raise ValueError(f"task {task.id} cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {task.id: task.dependencies for task in self.tasks}

        def visit(task_id: str) -> None:
            if task_id in visiting:
                raise ValueError("task graph contains a cycle")
            if task_id in visited:
                return
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in ids:
            visit(task_id)
        return self


# Failure classes that mean a task never actually executed, so they are NOT a
# real terminal state and must not release an ALL_TERMINAL barrier.
_NON_TERMINAL_FAILURE_CLASSES: frozenset[str] = frozenset({"dependency_blocked"})


class AgentTaskResult(DomainModel):
    task_id: str
    agent_role: AgentRole
    success: bool
    summary: str
    output: dict[str, JsonValue] = Field(default_factory=dict)
    evidence: tuple[EvidenceRecord, ...] = ()
    spawned_tasks: tuple[AgentTask, ...] = ()
    failure_class: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    token_usage: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_failure(self) -> AgentTaskResult:
        if not self.success and not self.failure_class:
            raise ValueError("failed task results require a failure_class")
        return self

    @property
    def terminal(self) -> bool:
        """Whether this result is a real typed terminal state.

        A successful result is always terminal.  A failed result is terminal
        only when it carries a real typed failure (login_required, timed_out,
        cancelled, dom_drift, ...) — never for a ``dependency_blocked``
        placeholder that never actually executed.
        """
        if self.success:
            return True
        if self.failure_class is None:
            return False
        return self.failure_class not in _NON_TERMINAL_FAILURE_CLASSES


class TraceEvent(DomainModel):
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1)
    task_id: str | None = None
    agent_role: AgentRole | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: dict[str, JsonValue] = Field(default_factory=dict)


class AgentDecision(DomainModel):
    state: DecisionState
    summary: str = Field(min_length=1)
    verifier_violations: tuple[str, ...] = ()
    exception_reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    requires_user_confirmation: bool = False

    @model_validator(mode="after")
    def validate_exception(self) -> AgentDecision:
        if self.state == DecisionState.ACCEPT_WITH_EXCEPTION:
            if not self.exception_reasons:
                raise ValueError("exception decisions require explicit reasons")
            if not self.requires_user_confirmation:
                raise ValueError("exception decisions require user confirmation")
        if self.state == DecisionState.ACCEPT and self.requires_user_confirmation:
            raise ValueError("direct acceptance cannot require exception confirmation")
        return self
