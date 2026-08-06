"""Local-first store for revalidation receipts and official handoffs (v0.5 wiring).

Official handoffs are short-lived (5 minutes) and single-use.  This store keeps
the last receipt + checklist per ``plan x component`` in memory with a TTL, and
mirrors the handoff ledger to an atomic JSON file so a restart cannot resurrect
an already-used handoff.  Nothing here ever creates a booked state.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tripchord.platform.handoff import OfficialHandoff, RevalidationReceipt

_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass
class _ComponentHandoffRecord:
    plan_version: str
    component_id: str
    receipt: RevalidationReceipt | None = None
    checklist: object | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    used_handoff_ids: set[str] = field(default_factory=set)


class HandoffStore:
    """In-memory (TTL-bounded) + JSON-mirrored store of handoffs per component."""

    def __init__(self, path: Path | None = None, *, ttl_seconds: int = 3600) -> None:
        self._path = path or Path(".runtime/handoffs.json")
        self._ttl_seconds = ttl_seconds
        self._records: dict[tuple[str, str], _ComponentHandoffRecord] = {}
        self._used_ids: set[str] = set()
        self._load()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        used = payload.get("used_handoff_ids", [])
        if isinstance(used, list):
            self._used_ids = {str(item) for item in used}

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            {"used_handoff_ids": sorted(self._used_ids)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(canonical, encoding="utf-8")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, self._path)
        with suppress(OSError):
            os.chmod(self._path, _FILE_MODE)

    # -- record access -------------------------------------------------------

    def _key(self, plan_version: str, component_id: str) -> tuple[str, str]:
        return (plan_version, component_id)

    def get(
        self,
        plan_version: str,
        component_id: str,
    ) -> _ComponentHandoffRecord | None:
        record = self._records.get(self._key(plan_version, component_id))
        if record is None:
            return None
        now = datetime.now(UTC)
        if (now - record.created_at).total_seconds() > self._ttl_seconds:
            self._records.pop(self._key(plan_version, component_id), None)
            return None
        return record

    def put(
        self,
        *,
        plan_version: str,
        component_id: str,
        receipt: RevalidationReceipt | None,
        checklist: object | None,
    ) -> None:
        key = self._key(plan_version, component_id)
        existing = self.get(plan_version, component_id)
        if existing is None:
            existing = _ComponentHandoffRecord(
                plan_version=plan_version, component_id=component_id
            )
        if receipt is not None:
            existing.receipt = receipt
        if checklist is not None:
            existing.checklist = checklist
        existing.created_at = datetime.now(UTC)
        self._records[key] = existing

    def mark_handoff_used(self, handoff_id: str) -> bool:
        """Mark a handoff single-used (returns False if already used)."""
        if handoff_id in self._used_ids:
            return False
        self._used_ids.add(handoff_id)
        self._persist()
        return True

    def is_handoff_used(self, handoff_id: str) -> bool:
        return handoff_id in self._used_ids

    def consume_handoff(self, handoff: OfficialHandoff) -> bool:
        """Atomically consume a handoff: single-use semantics.

        Returns True when the handoff was still valid and is now used; False
        when it was already used, expired or not usable.  This is the only path
        that transitions a handoff to ``used`` — and it never produces a booked
        state.
        """
        if not handoff.is_usable():
            return False
        return self.mark_handoff_used(handoff.handoff_id)
