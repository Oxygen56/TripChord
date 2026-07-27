from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
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
    __table_args__ = (Index("ix_job_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(30))
    stage: Mapped[str] = mapped_column(String(80))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    workspace: Mapped[WorkspaceRow] = relationship(back_populates="jobs")
