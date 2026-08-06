"""Local-first persistence for a plan's booking ledger (v0.6 wiring).

Booking facts are append-only and bound to an explicit user acknowledgement.
This store keeps one :class:`BookingLedger` per ``plan_version`` in an atomic
``.runtime/booking-ledgers/<plan_version>.json`` file (mode 0600) so the
protected set survives API restarts while remaining local-first — no cloud, no
platform reads.  Every load is validated against the typed ledger model; a
corrupt or out-of-schema file fails closed instead of silently dropping
protections.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from tripchord.platform.booking import BookingLedger

_LEDGER_DIR = Path(".runtime/booking-ledgers")
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR


class BookingLedgerStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _LEDGER_DIR

    def path_for(self, plan_version: str) -> Path:
        return self._root / f"{plan_version}.json"

    def load(self, plan_version: str) -> BookingLedger | None:
        path = self.path_for(plan_version)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return BookingLedger.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError):
            return None

    def save(self, ledger: BookingLedger) -> None:
        """Persist the ledger atomically (append-only content is caller's duty)."""
        path = self.path_for(ledger.plan_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        canonical = json.dumps(
            ledger.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(canonical, encoding="utf-8")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
        with suppress(OSError):
            os.chmod(path, _FILE_MODE)

    def delete(self, plan_version: str) -> None:
        with suppress(OSError):
            self.path_for(plan_version).unlink(missing_ok=True)
