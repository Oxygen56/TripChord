"""Hermetic test-session isolation from the host runtime's bridge state.

The Done-Gate runner executes the API test-suite inside an environment that
sets ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH`` to the *live* runtime bridge
state file (``.runtime/browser-bridge-state.json``).  ``BrowserTaskBridge``
honours that env var by default and silently restores the host's
in-flight/queued/claimed leases into every bridge constructed while the var is
set — including inside module-scoped fixtures.  That made the cancellation and
pairing-token counter-examples environment-dependent: the exact same code
passed in a bare shell and failed under the gate only because the host happened
to hold residual leases.

This fixture clears the live bridge-state path for the whole test session
*before* any module-scoped fixture constructs a bridge, then restores it on
teardown.  Persistence tests that genuinely need a file-backed store already
pass an explicit ``state_store`` (usually under ``tmp_path``), so they are
unaffected — they never relied on the process env.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

BRIDGE_STATE_ENV = "TRIPCHORD_BROWSER_BRIDGE_STATE_PATH"


@pytest.fixture(scope="session", autouse=True)
def _hermetic_host_bridge_state() -> Iterator[None]:
    original = os.environ.get(BRIDGE_STATE_ENV)
    os.environ.pop(BRIDGE_STATE_ENV, None)
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(BRIDGE_STATE_ENV, None)
        else:
            os.environ[BRIDGE_STATE_ENV] = original
