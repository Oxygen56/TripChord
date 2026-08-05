from __future__ import annotations

import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tripchord.agents.memory import (
    MemoryAccessContext,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    PrivacyBoundary,
)
from tripchord.agents.models import AgentRole
from tripchord.agents.persistent_memory import (
    CorruptionPolicy,
    PersistentMemoryLoadError,
    PersistentMemoryStore,
    PersistentMemoryWriteError,
)


def _preference(
    record_id: str,
    *,
    tenant_id: str = "tenant-a",
    user_id: str = "user-a",
    captured_at: datetime,
    expires_at: datetime | None = None,
    version: int = 1,
    sensitive: bool = False,
) -> MemoryRecord:
    return MemoryRecord(
        id=record_id,
        version=version,
        kind=MemoryKind.USER_PREFERENCE,
        scope=MemoryScope.USER,
        privacy=PrivacyBoundary.USER_PRIVATE,
        tenant_id=tenant_id,
        user_id=user_id,
        topic="user_preference",
        subject="breakfast",
        payload={"required": True},
        source="user-confirmed",
        captured_at=captured_at,
        expires_at=expires_at,
        allowed_roles=(AgentRole.CONTEXT, AgentRole.CP_SAT_PLANNER),
        sensitive=sensitive,
        tags=("breakfast",),
    )


def _access(
    *, tenant_id: str = "tenant-a", user_id: str = "user-a"
) -> MemoryAccessContext:
    return MemoryAccessContext(
        tenant_id=tenant_id,
        user_id=user_id,
        agent_role=AgentRole.CONTEXT,
    )


def test_persistent_store_survives_restart_and_keeps_access_boundaries(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "memory" / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("a", captured_at=now))
    store.upsert(
        _preference(
            "b",
            tenant_id="tenant-b",
            user_id="user-b",
            captured_at=now,
        )
    )

    restarted = PersistentMemoryStore(state_path, now=now + timedelta(minutes=1))

    assert restarted.get("a", _access(), now=now) is not None
    assert restarted.get("b", _access(), now=now) is None
    assert (
        restarted.get(
            "a",
            _access(tenant_id="tenant-b", user_id="user-a"),
            now=now,
        )
        is None
    )
    assert {item.id for item in restarted.query(MemoryQuery(), _access(), now=now)} == {"a"}
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_restart_drops_expired_records_and_rewrites_snapshot(tmp_path: Path) -> None:
    captured = datetime(2026, 8, 1, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=captured)
    store.upsert(
        _preference(
            "expired",
            captured_at=captured,
            expires_at=captured + timedelta(hours=1),
        )
    )
    store.upsert(_preference("active", captured_at=captured))

    restarted = PersistentMemoryStore(
        state_path,
        now=captured + timedelta(hours=2),
    )
    document = json.loads(state_path.read_text(encoding="utf-8"))

    assert restarted.get("expired", _access(), now=captured + timedelta(hours=2)) is None
    assert restarted.get("active", _access(), now=captured + timedelta(hours=2)) is not None
    assert [item["id"] for item in document["records"]] == ["active"]


def test_sensitive_records_are_memory_only_by_default(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("normal", captured_at=now))
    store.upsert(_preference("sensitive", captured_at=now, sensitive=True))

    assert store.get("sensitive", _access(), now=now) is None
    assert (
        store.get(
            "sensitive",
            _access().model_copy(update={"include_sensitive": True}),
            now=now,
        )
        is not None
    )
    restarted = PersistentMemoryStore(state_path, now=now)
    assert restarted.get("normal", _access(), now=now) is not None
    assert (
        restarted.get(
            "sensitive",
            _access().model_copy(update={"include_sensitive": True}),
            now=now,
        )
        is None
    )


def test_corrupt_snapshot_fails_closed_by_default(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text('{"schema_version":1,"records":[]}', encoding="utf-8")

    with pytest.raises(PersistentMemoryLoadError, match="incomplete"):
        PersistentMemoryStore(state_path)


def test_corrupt_snapshot_can_be_quarantined_without_loading_it(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")

    store = PersistentMemoryStore(
        state_path,
        corruption_policy=CorruptionPolicy.QUARANTINE,
    )

    assert store.query(MemoryQuery(), _access()) == ()
    assert not state_path.exists()
    quarantined = tuple(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "not-json"
    assert stat.S_IMODE(quarantined[0].stat().st_mode) == 0o600


def test_state_path_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    state_path = tmp_path / "state.json"
    state_path.symlink_to(target)

    with pytest.raises(PersistentMemoryLoadError, match="symlink"):
        PersistentMemoryStore(state_path)


def test_checksum_detects_tampering(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("preference", captured_at=now))
    document = json.loads(state_path.read_text(encoding="utf-8"))
    document["records"][0]["payload"]["required"] = False
    state_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PersistentMemoryLoadError, match="checksum mismatch"):
        PersistentMemoryStore(state_path, now=now)


def test_failed_write_rolls_back_in_process_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("preference", captured_at=now))

    def fail_before_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("tripchord.agents.persistent_memory.os.replace", fail_before_replace)
    with pytest.raises(PersistentMemoryWriteError):
        store.upsert(_preference("preference", captured_at=now, version=2))

    current = store.get("preference", _access(), now=now)
    assert current is not None
    assert current.version == 1
    assert tuple(tmp_path.glob(".state.json.tmp-*")) == ()

    monkeypatch.undo()
    restarted = PersistentMemoryStore(state_path, now=now)
    durable = restarted.get("preference", _access(), now=now)
    assert durable is not None
    assert durable.version == 1


def test_delete_is_durable_and_cross_user_delete_is_hidden(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("owned", captured_at=now))

    assert store.delete("owned", _access(user_id="user-b")) is False
    assert store.get("owned", _access(), now=now) is not None
    assert store.delete("owned", _access()) is True
    assert store.get("owned", _access(), now=now) is None

    restarted = PersistentMemoryStore(state_path, now=now)
    assert restarted.get("owned", _access(), now=now) is None


def test_failed_delete_write_rolls_back_in_process_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 3, 8, tzinfo=UTC)
    state_path = tmp_path / "state.json"
    store = PersistentMemoryStore(state_path, now=now)
    store.upsert(_preference("owned", captured_at=now))

    def fail_before_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("tripchord.agents.persistent_memory.os.replace", fail_before_replace)
    with pytest.raises(PersistentMemoryWriteError):
        store.delete("owned", _access())

    assert store.get("owned", _access(), now=now) is not None
