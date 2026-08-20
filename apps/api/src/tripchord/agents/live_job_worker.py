"""Real subprocess execution boundary for live planning job worker commands.

The live planning registry executes a ``LiveJobWorkerCommand`` inside a real OS
process running this module, so the hard-stop watchdog can PROVE the operation's
external side effects permanently froze: it SIGKILLs the whole process group and
confirms every member (leader AND any grandchild the entry forked) is gone via
``os.killpg(pgid, 0)``, and any probe file the worker was appending stops
growing. An in-process coroutine that swallows ``CancelledError`` can never be
proven dead; a worker subprocess can.

Process group / orphan identity:
- The worker immediately calls ``os.setsid()``, becoming the leader of a fresh
  session and process group (PGID == PID). Any process the entry spawns (e.g. a
  stubborn grandchild) inherits that group, so a hard stop can kill the whole
  tree — a dead parent worker never leaves a live grandchild behind.
- The registry spawns the worker with ``start_new_session=True`` as well, so the
  group exists even before this script runs.
- The worker writes a durable marker file (``--marker-file``) containing its
  PGID + a unique marker nonce + probe path ATOMICALLY before running the entry,
  and removes it on clean exit. If the API process is SIGKILLed mid-run the
  worker is orphaned but its marker file survives; a cold start authenticates
  the group via the marker (``ps -o command= -g <pgid>`` must contain the nonce)
  before killing it — a reused PGID owned by an unrelated process is never
  killed.

The worker loads its entry callable by FILE PATH (``importlib``), so any
self-contained module-level function — including a test helper — can run here
without polluting the registry's import path. The entry is called with the
command's ``args`` (plus ``probe_path`` when provided) and its JSON result is
written to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import secrets
import sys
from collections.abc import Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    """Atomically write ``payload`` as JSON to ``path`` (temp + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    parent_stat = path.parent.stat()
    if (
        path.parent.is_symlink()
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o777 != 0o700
    ):
        raise OSError("unsafe worker marker directory")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


_RECEIPT_IDENTITY_KEYS = (
    "pid",
    "pgid",
    "marker",
    "probe_path",
    "tenant_id",
    "lease_owner",
    "lease_generation",
    "job_id",
)


def _load_entry(module_path: str, entry: str) -> Any:
    spec = importlib.util.spec_from_file_location("live_job_worker_entry", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load worker module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    target = getattr(module, entry, None)
    if target is None:
        raise RuntimeError(f"worker entry not found: {entry!r}")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="live planning job worker")
    parser.add_argument("--module-path", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--args-json", required=True)
    parser.add_argument("--probe-path", default="")
    parser.add_argument("--marker", default="")
    parser.add_argument("--marker-file", default="")
    args = parser.parse_args(argv)
    # Own session + process group: the PGID equals this PID and every process the
    # entry forks (grandchildren) inherits it, so a hard stop can kill the whole
    # tree and never leave a live descendant behind after the parent dies.
    #
    # The registry spawns this worker with ``start_new_session=True``, so the
    # process is ALREADY a session/process-group leader (PGID == PID) and a
    # second ``os.setsid()`` would fail with ``PermissionError: Operation not
    # permitted``. Only create a fresh session when the worker was launched
    # without one (direct invocation / older spawn path).
    if os.getpgrp() != os.getpid():
        os.setsid()
    marker_file: Path | None = None
    try:
        marker_kwargs = json.loads(args.args_json)
    except json.JSONDecodeError:
        marker_kwargs = {}
    if not isinstance(marker_kwargs, dict):
        marker_kwargs = {}
    if args.marker and args.marker_file:
        marker_file = Path(args.marker_file)
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            marker_file,
            {
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "marker": args.marker,
                "probe_path": args.probe_path,
                "job_id": marker_kwargs.get("job_id"),
                "tenant_id": marker_kwargs.get("tenant_id"),
                "lease_owner": marker_kwargs.get("lease_owner"),
                "lease_generation": marker_kwargs.get("lease_generation"),
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
    exit_code = 0
    try:
        kwargs = json.loads(args.args_json)
        if not isinstance(kwargs, dict):
            raise RuntimeError("worker args must be a JSON object")
        if args.probe_path:
            kwargs["probe_path"] = args.probe_path
        fn = _load_entry(args.module_path, args.entry)
        maybe = fn(**kwargs)
        result = (
            asyncio.run(cast(Coroutine[Any, Any, Any], maybe))
            if inspect.isawaitable(maybe)
            else maybe
        )
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.flush()
    except BaseException as exc:
        # The registry reads stderr for diagnostics; a failure is a non-zero exit.
        #
        # C-146 P0-1: surface the REAL operation's failure provenance so the
        # parent registry can reconstruct a typed failure instead of a generic
        # "worker exited with N". Only the exception CLASS and (for an HTTP
        # failure) the status code are emitted — never the raw message, which may
        # contain user content. The registry sanitizes further via its own
        # safe-failure diagnostic before the label is ever published.
        marker_payload: dict[str, object] = {"class": type(exc).__name__}
        exception_chain: list[str] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen and len(exception_chain) < 8:
            seen.add(id(current))
            exception_chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        marker_payload["exception_chain"] = exception_chain
        if type(exc).__name__ == "HTTPException" and hasattr(exc, "status_code"):
            marker_payload["status_code"] = int(exc.status_code)
        api_main = sys.modules.get("tripchord.main")
        trace_sink = getattr(api_main, "model_trace_sink", None)
        records = getattr(trace_sink, "records", ())
        if isinstance(records, (list, tuple)):
            marker_payload["model_traces"] = [
                {
                    "role": trace.role.value,
                    "provider": trace.provider,
                    "model": trace.model,
                    "success": trace.success,
                    "error_class": trace.error_class,
                }
                for trace in records[-32:]
            ]
        sys.stderr.write(
            "TRIPCHORD_WORKER_FAILURE:"
            + json.dumps(marker_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        sys.stderr.write("live planning job worker failed\n")
        exit_code = 1
    finally:
        # A clean worker exit leaves an authenticated terminal receipt. The
        # parent removes it only after it proves the whole process group is
        # gone; an abrupt kill leaves the original live marker instead.
        if marker_file is not None:
            # Never inherit the on-disk marker: a reaper may have replaced it
            # with an authenticated-cleanup receipt lacking worker identity.
            source = {
                "pid": os.getpid(),
                "pgid": os.getpgrp(),
                "marker": args.marker,
                "probe_path": args.probe_path,
                "tenant_id": marker_kwargs.get("tenant_id"),
                "lease_owner": marker_kwargs.get("lease_owner"),
                "lease_generation": marker_kwargs.get("lease_generation"),
                "job_id": marker_kwargs.get("job_id"),
            }
            receipt = {key: source.get(key) for key in _RECEIPT_IDENTITY_KEYS}
            receipt.update(
                {
                    "schema": "tripchord.live-terminal-exit.v1",
                    "job_id": marker_kwargs.get("job_id"),
                    "terminal_exit": True,
                    "exit_code": exit_code,
                    "exited_at": datetime.now(UTC).isoformat(),
                    "digest": "",
                }
            )
            canonical = json.dumps(
                {key: value for key, value in receipt.items() if key != "digest"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            receipt["digest"] = hashlib.sha256(canonical).hexdigest()
            with suppress(OSError):
                _atomic_write(marker_file, receipt)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
