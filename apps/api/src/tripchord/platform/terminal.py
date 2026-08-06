"""Unified source terminal states and completion barrier (v0.3).

A :class:`SearchRun` freezes the selection snapshot and records every
:class:`SourceAttempt`.  Each attempt ends in exactly one
:class:`SourceTerminalState` (or stays ``running``).  The
:class:`CompletionBarrier` waits for **every** selected source to reach a
typed terminal state — success or a real typed failure — and only then
releases the settle node.  A ``dependency_blocked`` placeholder or a still
``running`` attempt never releases the barrier.

The barrier intentionally does not require ``success=True`` for every source:
a ``timed_out`` / ``login_required`` / ``cancelled`` source is a legitimate
terminal state, but it never contributes a quote.  Honest publication is a
separate concern handled by the coverage gate.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import ProviderScopeKey


def _canonical_utc_iso(value: datetime) -> str:
    """Normalise a datetime to UTC for canonical hashing.

    SQLite-backed persistence returns naive datetimes; treating a naive value
    as UTC keeps the receipt hash stable across a save/load round trip.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class SourceTerminalState(StrEnum):
    """Typed terminal state for any source attempt, across all verticals."""

    QUOTE_FOUND = "quote_found"
    CONFIRMED_EMPTY = "confirmed_empty"
    BOUNDED_NO_EXACT_QUOTE = "bounded_no_exact_quote"
    LOGIN_REQUIRED = "login_required"
    CAPTCHA_REQUIRED = "captcha_required"
    DOM_DRIFT = "dom_drift"
    PROVIDER_ERROR = "provider_error"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def has_planner_quote(self) -> bool:
        """Only ``quote_found`` may contribute a normalisable price."""
        return self is SourceTerminalState.QUOTE_FOUND


class SourceAttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    TERMINAL = "terminal"


class SourceAttempt(DomainModel):
    """One source task attempt bound to a scope and a run."""

    attempt_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    status: SourceAttemptStatus = SourceAttemptStatus.QUEUED
    terminal_state: SourceTerminalState | None = None
    started_at: datetime | None = None
    terminal_at: datetime | None = None
    generation: int = Field(default=0, ge=0)
    failure_class: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def validate_terminal(self) -> SourceAttempt:
        if self.status is SourceAttemptStatus.TERMINAL:
            if self.terminal_state is None or self.terminal_at is None:
                raise ValueError("terminal attempts require terminal_state and terminal_at")
        else:
            if self.terminal_state is not None or self.terminal_at is not None:
                raise ValueError("non-terminal attempts must not carry terminal fields")
        return self


class TerminalReceipt(DomainModel):
    """Append-only receipt proving one attempt reached a typed terminal state."""

    run_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    terminal_state: SourceTerminalState
    terminal_at: datetime
    generation: int = Field(ge=0)
    evidence_sha256: str | None = None

    def receipt_sha256(self) -> str:
        canonical = {
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "scope": self.scope.key,
            "terminal_state": self.terminal_state.value,
            "terminal_at": _canonical_utc_iso(self.terminal_at),
            "generation": self.generation,
            "evidence_sha256": self.evidence_sha256,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class SearchRun(DomainModel):
    """A run binds an immutable selection snapshot and collects attempts."""

    run_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    snapshot_sha256: str = Field(min_length=64, max_length=64)
    attempts: tuple[SourceAttempt, ...] = ()

    def attempt_by_id(self, attempt_id: str) -> SourceAttempt | None:
        return next((a for a in self.attempts if a.attempt_id == attempt_id), None)

    def running_count(self) -> int:
        return sum(1 for a in self.attempts if a.status is SourceAttemptStatus.RUNNING)

    def queued_count(self) -> int:
        return sum(1 for a in self.attempts if a.status is SourceAttemptStatus.QUEUED)


class ScopeCancellationTombstone(DomainModel):
    """A tombstone proving one scope was cancelled at a run generation.

    Once a scope is cancelled mid-run, ordinary retries, publication refresh,
    failover, delayed wake-up and event replan must all check the tombstone:
    a result whose attempt generation is at or below the tombstone generation
    is late and must never reach the Planner.  Only a brand-new attempt with a
    strictly newer generation (a genuinely fresh, newly authorised execution)
    may be reconsidered, and only when the scope was re-selected by the user.
    """

    run_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    cancelled_generation: int = Field(ge=0)
    cancelled_at: datetime
    reason: str = Field(min_length=1, max_length=400)

    def rejects(self, attempt_generation: int) -> bool:
        """A late attempt from at or below the cancelled generation is stale."""
        return attempt_generation <= self.cancelled_generation


class ScopeCancellationTombstoneRegistry(DomainModel):
    """Append-only registry of per-scope cancellation tombstones for one run."""

    run_id: str = Field(min_length=1)
    tombstones: tuple[ScopeCancellationTombstone, ...] = ()

    def tombstone_for(self, scope: ProviderScopeKey) -> ScopeCancellationTombstone | None:
        matches = [
            tombstone
            for tombstone in self.tombstones
            if tombstone.scope.key == scope.key
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: item.cancelled_generation)

    def rejects(self, scope: ProviderScopeKey, attempt_generation: int) -> bool:
        tombstone = self.tombstone_for(scope)
        if tombstone is None:
            return False
        return tombstone.rejects(attempt_generation)

    def cancelled_scopes(self) -> tuple[ProviderScopeKey, ...]:
        return tuple(
            dict.fromkeys(tombstone.scope for tombstone in self.tombstones)
        )


class CompletionBarrier(DomainModel):
    """Deterministic ALL_TERMINAL gate over a frozen set of source attempts.

    ``released`` is true only when every selected attempt reached a typed
    terminal state.  ``unresolved_attempt_ids`` lists attempts still
    queued/running.  A source that never executed (e.g. ``dependency_blocked``
    placeholder) stays non-terminal and therefore holds the barrier open, so it
    can never be published as if it were a finished search.
    """

    run_id: str = Field(min_length=1)
    selected_attempts: tuple[SourceAttempt, ...] = Field(min_length=1)
    deadline_at: datetime | None = None
    timeout_profile_version: str = "deterministic-v1"

    @property
    def released(self) -> bool:
        return all(
            attempt.status is SourceAttemptStatus.TERMINAL
            for attempt in self.selected_attempts
        )

    @property
    def unresolved_attempt_ids(self) -> tuple[str, ...]:
        return tuple(
            attempt.attempt_id
            for attempt in self.selected_attempts
            if attempt.status is not SourceAttemptStatus.TERMINAL
        )

    def quote_provider_scopes(self) -> tuple[ProviderScopeKey, ...]:
        return tuple(
            attempt.scope
            for attempt in self.selected_attempts
            if attempt.terminal_state is SourceTerminalState.QUOTE_FOUND
        )


def materialize_timed_out_attempts(
    attempts: tuple[SourceAttempt, ...],
    *,
    deadline: datetime,
    now: datetime,
) -> tuple[SourceAttempt, ...]:
    """Deterministically convert attempts past the deadline into ``timed_out``.

    Only queued/running attempts whose ``deadline`` has passed are materialised;
    this is how the barrier avoids infinite waits without an LLM extending any
    deadline.
    """
    materialized: list[SourceAttempt] = []
    for attempt in attempts:
        if attempt.status is SourceAttemptStatus.TERMINAL:
            materialized.append(attempt)
            continue
        if now >= deadline:
            materialized.append(
                attempt.model_copy(
                    update={
                        "status": SourceAttemptStatus.TERMINAL,
                        "terminal_state": SourceTerminalState.TIMED_OUT,
                        "terminal_at": now,
                        "detail": "deterministic deadline reached",
                    }
                )
            )
        else:
            materialized.append(attempt)
    return tuple(materialized)
