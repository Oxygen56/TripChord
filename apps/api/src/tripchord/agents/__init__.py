"""Multi-agent runtime contracts for TripChord."""

from tripchord.agents.context import ContextEngine, EvidenceBlackboard
from tripchord.agents.model_agent import ModelToolAgent
from tripchord.agents.model_gateway import (
    AnthropicMessagesClient,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelToolCall,
    ScriptedModelClient,
)
from tripchord.agents.models import (
    AgentDecision,
    AgentRole,
    AgentTask,
    AgentTaskResult,
    DecisionState,
    EvidenceRecord,
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
    TaskGraph,
    ToolPermission,
)
from tripchord.agents.package_request import (
    HybridPackageRequirementAgent,
    HybridPackageRequirementResult,
    PackageIntentTemplate,
    PackageRequestState,
    PackageRequirementRequest,
)
from tripchord.agents.runtime import AgentRegistry, DynamicTaskScheduler, SchedulerOutcome
from tripchord.agents.tools import (
    ApprovalGrant,
    ApprovalRequiredError,
    InvalidApprovalError,
    ToolCall,
    ToolForbiddenError,
    ToolPreview,
    ToolRegistry,
    ToolSpec,
)

__all__ = [
    "AgentDecision",
    "AgentRegistry",
    "AgentRole",
    "AgentTask",
    "AgentTaskResult",
    "AnthropicMessagesClient",
    "ApprovalGrant",
    "ApprovalRequiredError",
    "ContextEngine",
    "DecisionState",
    "DynamicTaskScheduler",
    "EvidenceBlackboard",
    "EvidenceRecord",
    "HybridPackageRequirementAgent",
    "HybridPackageRequirementResult",
    "InvalidApprovalError",
    "ModelRequest",
    "ModelResponse",
    "ModelRouter",
    "ModelToolAgent",
    "ModelToolCall",
    "PackageIntentTemplate",
    "PackageRequestState",
    "PackageRequirementRequest",
    "PreferenceConstitution",
    "PreferenceMode",
    "PreferenceRule",
    "PreferenceSource",
    "SchedulerOutcome",
    "ScriptedModelClient",
    "TaskGraph",
    "ToolCall",
    "ToolForbiddenError",
    "ToolPermission",
    "ToolPreview",
    "ToolRegistry",
    "ToolSpec",
]
