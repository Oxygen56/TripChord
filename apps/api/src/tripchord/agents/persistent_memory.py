from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import uuid4

from tripchord.agents.memory import MemoryAccessContext, MemoryRecord, MemoryStore

_SCHEMA_VERSION: Final = 1
_FILE_MODE: Final = 0o600
_DIRECTORY_MODE: Final = 0o700


class CorruptionPolicy(StrEnum):
    """How startup handles a snapshot that cannot be trusted."""

    FAIL_CLOSED = "fail_closed"
    QUARANTINE = "quarantine"


class PersistentMemoryError(RuntimeError):
    """Base error raised by the local durable-memory adapter."""


class PersistentMemoryLoadError(PersistentMemoryError):
    """Raised when an existing snapshot cannot be validated in full."""


class PersistentMemoryWriteError(PersistentMemoryError):
    """Raised when an atomic snapshot replacement does not complete."""


class PersistentMemoryStore(MemoryStore):
    """Single-process JSON persistence for :class:`MemoryStore`.

    The inherited store remains the source of truth for tenant, user, trip,
    session and role visibility.  This adapter writes a checksummed snapshot
    after every mutation.  It deliberately does not claim cross-process
    coordination, encryption at rest, or a distributed durability guarantee.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        corruption_policy: CorruptionPolicy = CorruptionPolicy.FAIL_CLOSED,
        persist_sensitive: bool = False,
        now: datetime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        raw_path = Path(state_path).expanduser()
        if raw_path.is_symlink():
            raise PersistentMemoryLoadError("memory state path must not be a symlink")
        self._state_path = raw_path.resolve(strict=False)
        self._corruption_policy = corruption_policy
        self._persist_sensitive = persist_sensitive
        self._clock = clock or (lambda: datetime.now(UTC))
        reference = now or self._clock()
        if reference.tzinfo is None:
            raise ValueError("persistent-memory clock must be timezone-aware")
        self._prepare_parent()
        self._restore(reference.astimezone(UTC))

    @property
    def state_path(self) -> Path:
        return self._state_path

    @property
    def persists_sensitive_records(self) -> bool:
        return self._persist_sensitive

    def upsert(self, record: MemoryRecord) -> None:
        """Update memory and disk as one in-process transaction."""

        with self._lock:
            before = dict(self._records)
            super().upsert(record)
            try:
                self._persist_locked()
            except Exception:
                self._records = before
                raise

    def purge_expired(self, *, now: datetime | None = None) -> int:
        """Purge expired records and durably record the removal."""

        reference = now or self._clock()
        if reference.tzinfo is None:
            raise ValueError("persistent-memory clock must be timezone-aware")
        with self._lock:
            before = dict(self._records)
            removed = super().purge_expired(now=reference)
            if not removed:
                return 0
            try:
                self._persist_locked()
            except Exception:
                self._records = before
                raise
            return removed

    def delete(self, record_id: str, access: MemoryAccessContext) -> bool:
        """Revoke memory and durably record the removal as one transaction."""

        with self._lock:
            before = dict(self._records)
            removed = super().delete(record_id, access)
            if not removed:
                return False
            try:
                self._persist_locked()
            except Exception:
                self._records = before
                raise
            return True

    def _prepare_parent(self) -> None:
        if self._state_path.exists() and self._state_path.is_symlink():
            raise PersistentMemoryLoadError("memory state path must not be a symlink")
        parent = self._state_path.parent
        parent.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        if not parent.is_dir():
            raise PersistentMemoryLoadError("memory state parent must be a directory")

    def _restore(self, now: datetime) -> None:
        if not self._state_path.exists():
            return
        try:
            records = self._read_validated_snapshot()
        except Exception as exc:
            if self._corruption_policy == CorruptionPolicy.QUARANTINE:
                self._quarantine_corrupt_snapshot()
                return
            if isinstance(exc, PersistentMemoryLoadError):
                raise
            raise PersistentMemoryLoadError("memory snapshot validation failed") from exc

        active: dict[str, MemoryRecord] = {}
        needs_rewrite = False
        for record in records:
            if not record.is_fresh(now):
                needs_rewrite = True
                continue
            if record.sensitive and not self._persist_sensitive:
                needs_rewrite = True
                continue
            active[record.id] = record
        with self._lock:
            self._records = active
            if needs_rewrite:
                self._persist_locked()

    def _read_validated_snapshot(self) -> tuple[MemoryRecord, ...]:
        if self._state_path.is_symlink():
            raise PersistentMemoryLoadError("memory state path must not be a symlink")
        try:
            file_stat = self._state_path.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise PersistentMemoryLoadError("memory state path must be a regular file")
            os.chmod(self._state_path, _FILE_MODE)
            document = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PersistentMemoryLoadError("memory snapshot is unreadable") from exc
        if not isinstance(document, dict):
            raise PersistentMemoryLoadError("memory snapshot root must be an object")
        if document.get("schema_version") != _SCHEMA_VERSION:
            raise PersistentMemoryLoadError("unsupported memory snapshot schema")
        raw_records = document.get("records")
        digest = document.get("records_sha256")
        if not isinstance(raw_records, list) or not isinstance(digest, str):
            raise PersistentMemoryLoadError("memory snapshot envelope is incomplete")
        expected = self._records_digest(raw_records)
        if not hmac.compare_digest(digest, expected):
            raise PersistentMemoryLoadError("memory snapshot checksum mismatch")

        records: list[MemoryRecord] = []
        seen_ids: set[str] = set()
        try:
            for item in raw_records:
                record = MemoryRecord.model_validate(item)
                if record.id in seen_ids:
                    raise PersistentMemoryLoadError("memory snapshot contains duplicate ids")
                seen_ids.add(record.id)
                records.append(record)
        except PersistentMemoryLoadError:
            raise
        except Exception as exc:
            raise PersistentMemoryLoadError("memory snapshot contains an invalid record") from exc
        return tuple(records)

    def _persist_locked(self) -> None:
        records = [
            record.model_dump(mode="json")
            for record in sorted(self._records.values(), key=lambda item: item.id)
            if self._persist_sensitive or not record.sensitive
        ]
        document = {
            "schema_version": _SCHEMA_VERSION,
            "records_sha256": self._records_digest(records),
            "records": records,
        }
        payload = self._canonical_json(document) + b"\n"
        temporary_path = self._state_path.with_name(
            f".{self._state_path.name}.tmp-{uuid4().hex}"
        )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._state_path)
            os.chmod(self._state_path, _FILE_MODE)
            self._fsync_parent()
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            raise PersistentMemoryWriteError("atomic memory snapshot write failed") from exc

    def _quarantine_corrupt_snapshot(self) -> None:
        quarantine_path = self._state_path.with_name(
            f"{self._state_path.name}.corrupt-{self._clock().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex}"
        )
        try:
            os.replace(self._state_path, quarantine_path)
            os.chmod(quarantine_path, _FILE_MODE)
            self._fsync_parent()
        except OSError as exc:
            raise PersistentMemoryLoadError(
                "memory snapshot is corrupt and could not be quarantined"
            ) from exc

    def _fsync_parent(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(self._state_path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _records_digest(cls, records: Sequence[object]) -> str:
        return hashlib.sha256(cls._canonical_json(records)).hexdigest()

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
