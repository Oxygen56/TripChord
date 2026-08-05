from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from pydantic import Field, JsonValue

from tripchord.agents.models import AgentRole, ToolPermission
from tripchord.domain.common import DomainModel


def empty_object_schema() -> dict[str, JsonValue]:
    return {"type": "object", "properties": {}}


class ToolSpec(DomainModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    permission: ToolPermission
    allowed_roles: tuple[AgentRole, ...]
    input_schema: dict[str, JsonValue] = Field(
        default_factory=empty_object_schema,
    )


class ToolCall(DomainModel):
    id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    agent_role: AgentRole
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolReceipt(DomainModel):
    call_id: str
    tool_name: str
    permission: ToolPermission
    success: bool
    output: dict[str, JsonValue] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    approval_token_used: bool = False
    preview_id: str | None = None
    approved_by: str | None = None
    call_digest: str


class ToolPreview(DomainModel):
    id: str
    call: ToolCall
    permission: ToolPermission
    call_digest: str
    summary: str
    created_at: datetime
    expires_at: datetime


class ApprovalGrant(DomainModel):
    token: str
    preview_id: str
    call_digest: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    used: bool = False


class ToolRegistryError(RuntimeError):
    pass


class ToolForbiddenError(ToolRegistryError):
    pass


class ApprovalRequiredError(ToolRegistryError):
    def __init__(self, call: ToolCall, spec: ToolSpec) -> None:
        super().__init__(f"tool {spec.name} requires preview and user approval")
        self.call = call
        self.spec = spec


class InvalidApprovalError(ToolRegistryError):
    pass


ToolHandler = Callable[[ToolCall], Awaitable[dict[str, JsonValue]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}
        self._previews: dict[str, ToolPreview] = {}
        self._grants: dict[str, ApprovalGrant] = {}
        self._receipts: dict[str, ToolReceipt] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._tools[spec.name] = (spec, handler)

    def spec(self, name: str) -> ToolSpec:
        registered = self._tools.get(name)
        if registered is None:
            raise ToolRegistryError(f"unknown tool: {name}")
        return registered[0]

    def has(self, name: str) -> bool:
        return name in self._tools

    def preview(
        self,
        call: ToolCall,
        *,
        summary: str,
        ttl_seconds: int = 900,
    ) -> ToolPreview:
        spec = self.spec(call.tool_name)
        if spec.permission != ToolPermission.HIGH_IMPACT:
            raise ToolRegistryError("only L3 high-impact calls require approval previews")
        if call.agent_role not in spec.allowed_roles:
            raise ToolForbiddenError(
                f"agent {call.agent_role} is not allowed to call {call.tool_name}"
            )
        now = datetime.now(UTC)
        preview = ToolPreview(
            id=f"preview-{secrets.token_urlsafe(12)}",
            call=call,
            permission=spec.permission,
            call_digest=self._digest(call),
            summary=summary,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._previews[preview.id] = preview
        return preview

    def approve(self, preview_id: str, *, approved_by: str) -> ApprovalGrant:
        preview = self._previews.get(preview_id)
        now = datetime.now(UTC)
        if preview is None:
            raise InvalidApprovalError("approval preview does not exist")
        if preview.expires_at <= now:
            raise InvalidApprovalError("approval preview has expired")
        grant = ApprovalGrant(
            token=f"grant-{secrets.token_urlsafe(24)}",
            preview_id=preview.id,
            call_digest=preview.call_digest,
            approved_by=approved_by,
            approved_at=now,
            expires_at=preview.expires_at,
        )
        self._grants[grant.token] = grant
        return grant

    async def invoke(self, call: ToolCall, *, approval_token: str | None = None) -> ToolReceipt:
        registered = self._tools.get(call.tool_name)
        if registered is None:
            raise ToolRegistryError(f"unknown tool: {call.tool_name}")
        spec, handler = registered
        if call.agent_role not in spec.allowed_roles:
            raise ToolForbiddenError(
                f"agent {call.agent_role} is not allowed to call {call.tool_name}"
            )
        if spec.permission == ToolPermission.FORBIDDEN:
            raise ToolForbiddenError(f"tool {call.tool_name} is forbidden")
        grant: ApprovalGrant | None = None
        if spec.permission == ToolPermission.HIGH_IMPACT:
            if not approval_token:
                raise ApprovalRequiredError(call, spec)
            grant = self._validate_grant(call, approval_token)
            self._grants[approval_token] = grant.model_copy(update={"used": True})
        started = datetime.now(UTC)
        output = await handler(call)
        receipt = ToolReceipt(
            call_id=call.id,
            tool_name=call.tool_name,
            permission=spec.permission,
            success=True,
            output=output,
            started_at=started,
            finished_at=datetime.now(UTC),
            approval_token_used=grant is not None,
            preview_id=grant.preview_id if grant else None,
            approved_by=grant.approved_by if grant else None,
            call_digest=self._digest(call),
        )
        self._receipts[receipt.call_id] = receipt
        return receipt

    def verify_receipt(self, call: ToolCall, receipt: ToolReceipt) -> bool:
        stored = self._receipts.get(call.id)
        return (
            stored is not None
            and stored == receipt
            and receipt.success
            and receipt.call_digest == self._digest(call)
            and receipt.finished_at >= receipt.started_at
        )

    def _validate_grant(self, call: ToolCall, token: str) -> ApprovalGrant:
        grant = self._grants.get(token)
        if grant is None:
            raise InvalidApprovalError("approval token is unknown")
        if grant.used:
            raise InvalidApprovalError("approval token was already used")
        if grant.expires_at <= datetime.now(UTC):
            raise InvalidApprovalError("approval token has expired")
        if grant.call_digest != self._digest(call):
            raise InvalidApprovalError("approval token does not match this exact action")
        return grant

    def _digest(self, call: ToolCall) -> str:
        canonical = json.dumps(
            call.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for spec, _ in self._tools.values())
