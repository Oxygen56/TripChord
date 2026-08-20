from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SearchRunRow(Base):
    """One persisted :class:`tripchord.platform.terminal.SearchRun`.

    ``payload`` keeps the full typed run (snapshot SHA + attempts) so a stored
    run can be revalidated; the relational columns let a tenant list and page
    runs without decoding every payload.
    """

    __tablename__ = "search_runs"
    __table_args__ = (Index("ix_search_runs_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempts: Mapped[list[SourceAttemptRow]] = relationship(
        back_populates="search_run",
        cascade="all, delete-orphan",
        order_by="SourceAttemptRow.attempt_id",
    )
    receipts: Mapped[list[TerminalReceiptRow]] = relationship(
        back_populates="search_run",
        cascade="all, delete-orphan",
        order_by="TerminalReceiptRow.attempt_id",
    )


class SourceAttemptRow(Base):
    __tablename__ = "source_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(String(120))
    scope_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20))
    terminal_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    failure_class: Mapped[str | None] = mapped_column(String(120), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(400), nullable=True)
    search_run: Mapped[SearchRunRow] = relationship(back_populates="attempts")


class TerminalReceiptRow(Base):
    __tablename__ = "terminal_receipts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("search_runs.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[str] = mapped_column(String(120))
    scope_key: Mapped[str] = mapped_column(String(160))
    terminal_state: Mapped[str] = mapped_column(String(40))
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generation: Mapped[int] = mapped_column(Integer)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receipt_sha256: Mapped[str] = mapped_column(String(64))
    search_run: Mapped[SearchRunRow] = relationship(back_populates="receipts")


class ProviderSelectionRow(Base):
    """Persisted per-scope user selection (v0.2 deviation: DB-backed store)."""

    __tablename__ = "provider_selection"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_key",
            name="uq_provider_selection_tenant_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    scope_key: Mapped[str] = mapped_column(String(160))
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class WorkspaceRow(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_workspace_tenant_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    plans: Mapped[list[PlanRow]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="PlanRow.version",
    )
    events: Mapped[list[EventRow]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="EventRow.created_at",
    )
    jobs: Mapped[list[JobRow]] = relationship(
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="JobRow.created_at",
    )


class PlanRow(Base):
    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_plan_workspace_version"),
        Index("ix_plan_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    workspace: Mapped[WorkspaceRow] = relationship(back_populates="plans")


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_event_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    workspace: Mapped[WorkspaceRow] = relationship(back_populates="events")


class JobRow(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_job_workspace_created", "workspace_id", "created_at"),
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_job_workspace_idempotency"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(80))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(36), index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    workspace: Mapped[WorkspaceRow] = relationship(back_populates="jobs")


class LivePlanningJobRow(Base):
    """Authoritative JSON snapshot for the long-running live planner control plane."""

    __tablename__ = "live_planning_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_live_planning_job_tenant_idempotency",
        ),
        Index("ix_live_planning_jobs_tenant_updated", "tenant_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_sha256: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    command_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    cancel_target_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cancel_target_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A reaper owns this fence while it authenticates and stops an orphaned
    # worker.  A row with an active reaper is deliberately not claimable.
    reap_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reap_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reap_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reap_target_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reap_target_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reap_controller: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reap_pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reap_marker_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reap_proof_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reap_proof_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reap_authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reap_death_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LivePlanningPairResultRow(Base):
    """Durable per-date-pair result/checkpoint used for restart resume."""

    __tablename__ = "live_planning_pair_results"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "job_id", "date_pair_id", name="uq_live_pair_result_identity"
        ),
        Index("ix_live_pair_results_job_sequence", "tenant_id", "job_id", "sequence"),
    )

    tenant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    date_pair_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    execution_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_owner: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class LiveMonitorRow(Base):
    """Persisted :class:`tripchord.agents.live_monitor.LiveMonitorStatus`.

    The status payload (relational columns plus ``boundary`` text) lets a later
    context recover an opt-in live-quote monitor after a process restart; check
    history is stored in :class:`LiveMonitorCheckRow`.  ``boundary`` is stored
    verbatim so a reconstructed status stays faithful to the policy text that
    was in force when the monitor ran.
    """

    __tablename__ = "live_monitors"
    __table_args__ = (Index("ix_live_monitors_tenant_created", "tenant_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[str] = mapped_column(String(100), index=True)
    state: Mapped[str] = mapped_column(String(20))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    max_checks: Mapped[int] = mapped_column(Integer)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    check_count: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    boundary: Mapped[str] = mapped_column(Text, default="")
    checks: Mapped[list[LiveMonitorCheckRow]] = relationship(
        back_populates="monitor",
        cascade="all, delete-orphan",
        order_by="LiveMonitorCheckRow.sequence",
    )


class LiveMonitorCheckRow(Base):
    __tablename__ = "live_monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_id: Mapped[str] = mapped_column(
        ForeignKey("live_monitors.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_component_id: Mapped[str] = mapped_column(String(200))
    event_id: Mapped[str] = mapped_column(String(200))
    applied_disposition: Mapped[str | None] = mapped_column(String(60), nullable=True)
    decision_state: Mapped[str] = mapped_column(String(60))
    package_changed: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(Text, default="")
    monitor: Mapped[LiveMonitorRow] = relationship(back_populates="checks")
