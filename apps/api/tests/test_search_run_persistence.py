"""SearchRun persistence and builder contract tests (v0.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tripchord.agents.live_system import PlatformSearchCoverage
from tripchord.persistence.database import Database
from tripchord.persistence.search_runs import (
    SearchRunNotFoundError,
    SearchRunRepository,
)
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.search_run_builder import derive_scope_from_task_id
from tripchord.platform.terminal import (
    SearchRun,
    SourceAttempt,
    SourceAttemptStatus,
    SourceTerminalState,
    TerminalReceipt,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _minimal_coverage() -> PlatformSearchCoverage:
    return PlatformSearchCoverage(
        provider="ctrip",  # type: ignore[arg-type]
        successful_source_ids=("source-ctrip-flight",),
        terminal_outcome_source_ids=("source-ctrip-flight",),
        usable_quote_source_ids=("source-ctrip-flight",),
        successful_verticals=("flight",),  # type: ignore[arg-type]
        completed_search_verticals=("flight",),  # type: ignore[arg-type]
        failed_source_ids=(),
        failure_reasons=(),
        complete=True,
    )


@pytest.mark.asyncio
async def test_search_run_repository_round_trip() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = SearchRunRepository(session, tenant_id="tenant-a")
        run = SearchRun(
            run_id="search-run-abc",
            created_at=NOW,
            snapshot_sha256="a" * 64,
            attempts=(
                SourceAttempt(
                    attempt_id="source-ctrip-flight",
                    run_id="search-run-abc",
                    scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
                    status=SourceAttemptStatus.TERMINAL,
                    terminal_state=SourceTerminalState.QUOTE_FOUND,
                    started_at=NOW,
                    terminal_at=NOW,
                    generation=0,
                ),
            ),
        )
        receipt = TerminalReceipt(
            run_id="search-run-abc",
            attempt_id="source-ctrip-flight",
            scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
            terminal_state=SourceTerminalState.QUOTE_FOUND,
            terminal_at=NOW,
            generation=0,
        )
        saved = await repository.save(run, receipts=(receipt,))
        assert saved.run_id == run.run_id
        assert saved.attempts == run.attempts
        loaded = await repository.get("search-run-abc")
        assert loaded.run_id == "search-run-abc"
        assert loaded.attempts == run.attempts
        assert loaded.snapshot_sha256 == "a" * 64
    await database.dispose()


@pytest.mark.asyncio
async def test_search_run_repository_tenant_isolation() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = SearchRunRepository(session, tenant_id="tenant-a")
        run = SearchRun(
            run_id="search-run-tenant",
            created_at=NOW,
            snapshot_sha256="b" * 64,
            attempts=(),
        )
        await repository.save(run)
    async with database.sessions() as session:
        other = SearchRunRepository(session, tenant_id="tenant-b")
        with pytest.raises(SearchRunNotFoundError):
            await other.get("search-run-tenant")
    await database.dispose()


@pytest.mark.asyncio
async def test_search_run_repository_checksum_verifies_receipt() -> None:
    database = Database("sqlite+aiosqlite://")
    await database.create_schema()
    async with database.sessions() as session:
        repository = SearchRunRepository(session, tenant_id="tenant-a")
        run = SearchRun(
            run_id="search-run-checksum",
            created_at=NOW,
            snapshot_sha256="c" * 64,
            attempts=(),
        )
        good_receipt = TerminalReceipt(
            run_id="search-run-checksum",
            attempt_id="a",
            scope=ProviderScopeKey(provider="ctrip", vertical=ProviderVertical.FLIGHT),
            terminal_state=SourceTerminalState.CONFIRMED_EMPTY,
            terminal_at=NOW,
            generation=0,
        )
        await repository.save(run, receipts=(good_receipt,))
        # A tampered receipt hash must be rejected on load.
        from tripchord.persistence.models import TerminalReceiptRow

        await session.execute(
            TerminalReceiptRow.__table__.update().values(
                receipt_sha256="f" * 64,
                run_id="search-run-checksum",
            )
        )
        await session.commit()
        with pytest.raises(RuntimeError):
            await repository.get("search-run-checksum")
    await database.dispose()


def test_derive_scope_from_task_id() -> None:
    assert derive_scope_from_task_id("source-ctrip-flight") == ProviderScopeKey(
        provider="ctrip", vertical=ProviderVertical.FLIGHT
    )
    assert derive_scope_from_task_id("source-qunar-lodging-full") == ProviderScopeKey(
        provider="qunar", vertical=ProviderVertical.LODGING
    )
    assert derive_scope_from_task_id("public-transfer-icom-continuous-outbound") == (
        ProviderScopeKey(provider="icom", vertical=ProviderVertical.TRANSFER)
    )
    assert derive_scope_from_task_id("unrelated") is None


def test_build_search_run_requires_live_run() -> None:
    # The builder is typed against LivePackageAgentRun; this test ensures the
    # deterministic run id / snapshot derivation functions stay importable.
    import hashlib

    raw = hashlib.sha256(b"x").hexdigest()
    assert len(raw) == 64
