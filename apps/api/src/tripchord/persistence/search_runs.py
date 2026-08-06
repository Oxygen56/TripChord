"""Persistence for :class:`SearchRun` / :class:`SourceAttempt` / receipts.

The live system executes many source tasks; ``SearchRun`` binds an immutable
selection snapshot and every typed terminal outcome so a later context can
recover what actually ran without replaying the whole DAG.  Receipts are
append-only and hash-bound; the repository re-verifies the receipt hash before
returning a stored run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from tripchord.persistence.models import SearchRunRow, SourceAttemptRow, TerminalReceiptRow
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.terminal import (
    SearchRun,
    SourceAttempt,
    SourceAttemptStatus,
    SourceTerminalState,
    TerminalReceipt,
)


class SearchRunNotFoundError(LookupError):
    pass


class SearchRunConflictError(RuntimeError):
    pass


def _scope_from_key(key: str) -> ProviderScopeKey:
    provider, _, vertical = key.partition(":")
    return ProviderScopeKey(provider=provider, vertical=ProviderVertical(vertical))


def _attempt_to_dict(attempt: SourceAttempt) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "run_id": attempt.run_id,
        "scope_key": attempt.scope.key,
        "status": attempt.status.value,
        "terminal_state": attempt.terminal_state.value if attempt.terminal_state else None,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "terminal_at": attempt.terminal_at.isoformat() if attempt.terminal_at else None,
        "generation": attempt.generation,
        "failure_class": attempt.failure_class,
        "detail": attempt.detail,
    }


def _attempt_from_dict(value: dict[str, Any]) -> SourceAttempt:
    return SourceAttempt(
        attempt_id=value["attempt_id"],
        run_id=value["run_id"],
        scope=_scope_from_key(value["scope_key"]),
        status=SourceAttemptStatus(value["status"]),
        terminal_state=(
            SourceTerminalState(value["terminal_state"]) if value["terminal_state"] else None
        ),
        started_at=(
            datetime.fromisoformat(value["started_at"]) if value["started_at"] else None
        ),
        terminal_at=(
            datetime.fromisoformat(value["terminal_at"]) if value["terminal_at"] else None
        ),
        generation=value["generation"],
        failure_class=value["failure_class"],
        detail=value["detail"],
    )


def _receipt_to_dict(receipt: TerminalReceipt) -> dict[str, Any]:
    return {
        "run_id": receipt.run_id,
        "attempt_id": receipt.attempt_id,
        "scope_key": receipt.scope.key,
        "terminal_state": receipt.terminal_state.value,
        "terminal_at": receipt.terminal_at.isoformat(),
        "generation": receipt.generation,
        "evidence_sha256": receipt.evidence_sha256,
        "receipt_sha256": receipt.receipt_sha256(),
    }


def _receipt_from_dict(value: dict[str, Any]) -> TerminalReceipt:
    receipt = TerminalReceipt(
        run_id=value["run_id"],
        attempt_id=value["attempt_id"],
        scope=_scope_from_key(value["scope_key"]),
        terminal_state=SourceTerminalState(value["terminal_state"]),
        terminal_at=datetime.fromisoformat(value["terminal_at"]),
        generation=value["generation"],
        evidence_sha256=value["evidence_sha256"],
    )
    if receipt.receipt_sha256() != value["receipt_sha256"]:
        raise SearchRunConflictError(
            f"stored terminal receipt hash mismatch for attempt {value['attempt_id']}"
        )
    return receipt


def _row_to_search_run(row: SearchRunRow) -> SearchRun:
    payload = row.payload
    attempts = tuple(
        _attempt_from_dict(item)
        for item in payload.get("attempts", ())
    )
    return SearchRun(
        run_id=payload["run_id"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        snapshot_sha256=payload["snapshot_sha256"],
        attempts=attempts,
    )


class SearchRunRepository:
    """Tenant-scoped persistence for live search runs."""

    def __init__(self, session: AsyncSession, tenant_id: str = "anonymous") -> None:
        self._session = session
        self._tenant_id = tenant_id

    async def save(
        self,
        run: SearchRun,
        receipts: tuple[TerminalReceipt, ...] = (),
    ) -> SearchRun:
        existing = await self._session.scalar(
            select(SearchRunRow).where(
                SearchRunRow.id == run.run_id,
                SearchRunRow.tenant_id == self._tenant_id,
            )
        )
        if existing is not None:
            if existing.snapshot_sha256 != run.snapshot_sha256:
                raise SearchRunConflictError(
                    f"search run {run.run_id} already exists with a different snapshot"
                )
            return _row_to_search_run(existing)
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "created_at": run.created_at.isoformat(),
            "snapshot_sha256": run.snapshot_sha256,
            "attempts": [_attempt_to_dict(attempt) for attempt in run.attempts],
        }
        row = SearchRunRow(
            id=run.run_id,
            tenant_id=self._tenant_id,
            snapshot_sha256=run.snapshot_sha256,
            created_at=run.created_at,
            payload=payload,
        )
        row.attempts = [
            SourceAttemptRow(
                run_id=run.run_id,
                attempt_id=attempt.attempt_id,
                scope_key=attempt.scope.key,
                status=attempt.status.value,
                terminal_state=attempt.terminal_state.value if attempt.terminal_state else None,
                started_at=attempt.started_at,
                terminal_at=attempt.terminal_at,
                generation=attempt.generation,
                failure_class=attempt.failure_class,
                detail=attempt.detail,
            )
            for attempt in run.attempts
        ]
        row.receipts = [
            TerminalReceiptRow(
                run_id=receipt.run_id,
                attempt_id=receipt.attempt_id,
                scope_key=receipt.scope.key,
                terminal_state=receipt.terminal_state.value,
                terminal_at=receipt.terminal_at,
                generation=receipt.generation,
                evidence_sha256=receipt.evidence_sha256,
                receipt_sha256=receipt.receipt_sha256(),
            )
            for receipt in receipts
        ]
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return _row_to_search_run(row)

    async def get(self, run_id: str) -> SearchRun:
        row = await self._session.scalar(
            select(SearchRunRow)
            .where(
                SearchRunRow.id == run_id,
                SearchRunRow.tenant_id == self._tenant_id,
            )
            .options(
                selectinload(SearchRunRow.attempts),
                selectinload(SearchRunRow.receipts),
            )
        )
        if row is None:
            raise SearchRunNotFoundError(run_id)
        # Append-only receipts are hash-bound; reject any stored run whose
        # receipt hashes do not reproduce so tampered storage cannot masquerade
        # as a terminal outcome.
        for item in row.receipts:
            receipt = TerminalReceipt(
                run_id=item.run_id,
                attempt_id=item.attempt_id,
                scope=_scope_from_key(item.scope_key),
                terminal_state=SourceTerminalState(item.terminal_state),
                terminal_at=item.terminal_at,
                generation=item.generation,
                evidence_sha256=item.evidence_sha256,
            )
            if receipt.receipt_sha256() != item.receipt_sha256:
                raise SearchRunConflictError(
                    f"stored terminal receipt hash mismatch for attempt {item.attempt_id}"
                )
        return _row_to_search_run(row)

    async def list_runs(
        self,
        *,
        limit: int = 50,
        before_id: str | None = None,
    ) -> tuple[SearchRun, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("list limit must be between 1 and 200")
        statement = (
            select(SearchRunRow)
            .where(SearchRunRow.tenant_id == self._tenant_id)
            .order_by(SearchRunRow.created_at.desc(), SearchRunRow.id.desc())
            .limit(limit)
        )
        if before_id is not None:
            before_row = await self._session.scalar(
                select(SearchRunRow).where(
                    SearchRunRow.id == before_id,
                    SearchRunRow.tenant_id == self._tenant_id,
                )
            )
            if before_row is None:
                raise SearchRunNotFoundError(before_id)
            statement = (
                select(SearchRunRow)
                .where(
                    SearchRunRow.tenant_id == self._tenant_id,
                    (
                        (SearchRunRow.created_at < before_row.created_at)
                        | (
                            (SearchRunRow.created_at == before_row.created_at)
                            & (SearchRunRow.id < before_row.id)
                        )
                    ),
                )
                .order_by(SearchRunRow.created_at.desc(), SearchRunRow.id.desc())
                .limit(limit)
            )
        rows = (await self._session.scalars(statement)).all()
        return tuple(_row_to_search_run(row) for row in rows)
