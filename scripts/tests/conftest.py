"""Test-session hardening: real host secrets never reach gate tests.

C-122 round-18 security review: a failing traceback previously expanded real
inherited secret environment values verbatim into the log.  This autouse
session-scoped fixture snapshots every secret-bearing environment variable the
gate can read (model API keys, bridge token, supplier credentials, cookies,
sessions) and clears them for the whole test session, restoring them at session
end — so a failing test can only ever reveal dummy values the tests themselves
set via monkeypatch, never the host's real credentials.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest

from scripts import run_product_done_gate as gate

_SECRET_ENV_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|passwd|password|cookie|session|auth)"
)
_EXTRA_SECRET_ENV = frozenset(
    {
        "COOKIE",
        "SESSION",
        "SESSION_ID",
        "SESSIONID",
        "AUTHORIZATION",
        "AUTH",
        "PAYLOAD",
    }
)


@pytest.fixture(scope="session", autouse=True)
def _clear_secret_env() -> None:
    """Snapshot and clear every secret-bearing env var for the whole session.

    ``_SECRET_ENV_CANDIDATES`` covers the model/supplier/bridge keys the gate
    explicitly reads; the regex plus the extra set catch any other host secret
    (cookies, session ids, auth headers) so no real value can reach a test.
    Values are restored after the session so the developer's shell is not left
    clobbered.
    """
    candidates = set(gate._SECRET_ENV_CANDIDATES) | set(_EXTRA_SECRET_ENV)
    saved: dict[str, str | None] = {}
    for name in list(os.environ):
        if name in candidates or _SECRET_ENV_RE.search(name):
            saved[name] = os.environ.pop(name, None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture(scope="session", autouse=True)
def _isolate_runtime_persistence(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """R27 Block 46: a test session never touches the LIVE durable runtime
    files.  The gate's live-state SQLite preflight defaults to
    ``ROOT/tripchord.db`` and the bridge-state lease preflight defaults to
    ``ROOT/.runtime/browser-bridge-state.json``; a clean-env pytest run must
    default BOTH to a temporary path instead, so the live files are never
    opened or written (their bytes + mtime stay invariant across the run).  A
    session-scoped temp dir is created once and shared by every test; an
    explicit caller override (a test that sets ``TRIPCHORD_DATABASE_URL`` /
    ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH`` via monkeypatch) still wins for the
    duration of that test.
    """
    tmp = tmp_path_factory.mktemp("tripchord-runtime-isolation")
    os.environ[
        "TRIPCHORD_DATABASE_URL"
    ] = f"sqlite+aiosqlite:///{tmp / 'tripchord-test.db'}"
    os.environ[
        "TRIPCHORD_BROWSER_BRIDGE_STATE_PATH"
    ] = str(tmp / "browser-bridge-state.json")
    yield
    os.environ.pop("TRIPCHORD_DATABASE_URL", None)
    os.environ.pop("TRIPCHORD_BROWSER_BRIDGE_STATE_PATH", None)
