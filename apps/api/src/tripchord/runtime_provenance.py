"""Runtime provenance for the live API process.

Every live worker captures, once at process start, the identity of the code it
is actually executing: the repository toplevel, the git HEAD commit, the
process start time and PID, the Python interpreter, and the content
fingerprints of the dependency lockfile and the ``live_system`` source.

Downstream Done-Gate layers compare a running API's provenance (read back
through ``/api/v1/agents/runtime``) against the provenance the *current*
checked-out tree claims and fail closed on any mismatch.  This is deliberately
stricter than a working-tree ``git status``: a worker started before a HEAD
move, or whose on-disk source changed without a restart, keeps reporting its
startup provenance and therefore cannot be certified as the current HEAD even
when the worktree happens to look clean.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ``apps/api/src/tripchord/runtime_provenance.py`` -> repository root.
_REPO_ROOT = Path(__file__).resolve().parents[4]

_GIT_ENV_KEEP = frozenset({"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"})

# Fields that must match between a running API and the current tree.  ``pid``
# is validated separately (must be a positive integer naming a live process)
# because it is inherently process-specific.
_PROVENANCE_COMPARE_FIELDS = (
    "repo_toplevel",
    "commit_sha",
    "dependency_lock_sha256",
    "live_system_source_sha256",
)

# Python identity and startup-time are hard-validated per the C-101 runtime
# contract: they are collected by the worker but are not part of the local-tree
# comparison (the tree cannot know which interpreter a remote worker used), so
# they are checked for presence and shape instead of equality.
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?")


def _git_safe_env() -> dict[str, str]:
    """Drop caller-injected GIT_DIR/GIT_WORK_TREE so provenance names the
    repository that actually runs, not one the environment redirects to."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") or key in _GIT_ENV_KEEP
    }


def _git_output(args: list[str], repo_root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            env=_git_safe_env(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


@dataclass(frozen=True)
class RuntimeProvenance:
    """One immutable snapshot of a running process's startup identity."""

    repo_toplevel: str | None
    commit_sha: str | None
    started_at: str
    pid: int
    python_version: str
    python_executable: str
    dependency_lock_sha256: str | None
    live_system_source_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_toplevel": self.repo_toplevel,
            "commit_sha": self.commit_sha,
            "started_at": self.started_at,
            "pid": self.pid,
            "python_version": self.python_version,
            "python_executable": self.python_executable,
            "dependency_lock_sha256": self.dependency_lock_sha256,
            "live_system_source_sha256": self.live_system_source_sha256,
        }


def _capture(repo_root: Path | None = None) -> RuntimeProvenance:
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    return RuntimeProvenance(
        repo_toplevel=_git_output(["rev-parse", "--show-toplevel"], root),
        commit_sha=_git_output(["rev-parse", "HEAD"], root),
        started_at=datetime.now(UTC).isoformat(),
        pid=os.getpid(),
        python_version=sys.version.split()[0],
        python_executable=sys.executable,
        dependency_lock_sha256=_sha256_file(root / "uv.lock"),
        live_system_source_sha256=_sha256_file(
            root / "apps" / "api" / "src" / "tripchord" / "agents" / "live_system.py"
        ),
    )


# Captured once at import time so it describes the process that imported this
# module (the live API worker for ``tripchord.main``), not the current tree.
PROVENANCE = _capture()


def local_expected_provenance(repo_root: Path | None = None) -> dict[str, str | None]:
    """The provenance the current checked-out tree claims *right now*.

    Re-reads git HEAD and the source/lock fingerprints fresh, so a caller can
    compare a running API's startup provenance against the live tree.
    """
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    return {
        "repo_toplevel": _git_output(["rev-parse", "--show-toplevel"], root),
        "commit_sha": _git_output(["rev-parse", "HEAD"], root),
        "dependency_lock_sha256": _sha256_file(root / "uv.lock"),
        "live_system_source_sha256": _sha256_file(
            root / "apps" / "api" / "src" / "tripchord" / "agents" / "live_system.py"
        ),
    }


def provenance_mismatches(
    reported: dict[str, Any] | None,
    expected: dict[str, Any],
) -> list[str]:
    """Compare a running API's provenance against the expected values.

    Returns a list of human-readable mismatch descriptions; an empty list means
    the running API matches the current tree.  A missing/empty provenance fails
    closed rather than passing by omission.
    """
    if not isinstance(reported, dict):
        return ["runtime carries no runtime_provenance"]
    mismatches: list[str] = []
    for key in _PROVENANCE_COMPARE_FIELDS:
        reported_value = reported.get(key)
        expected_value = expected.get(key)
        if reported_value != expected_value:
            mismatches.append(
                f"runtime provenance {key}={reported_value!r} != expected {expected_value!r}"
            )
    # Process identity: the reported PID must be a positive integer that names
    # a live process at check time — a dead worker cannot certify a running
    # code identity even if its captured fields happen to match.
    pid = reported.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        mismatches.append(f"runtime provenance pid={pid!r} is not a positive integer")
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            mismatches.append(
                f"runtime provenance pid={pid!r} does not name a live process"
            )
        except (PermissionError, OSError):
            # PermissionError still means the process exists; other transient
            # OSErrors are treated as present rather than failing the check.
            pass
    # Python identity: the worker must record a plausible interpreter version
    # and an absolute executable path.
    python_version = reported.get("python_version")
    if not isinstance(python_version, str) or not _PYTHON_VERSION_RE.match(python_version):
        mismatches.append(
            f"runtime provenance python_version={python_version!r} is missing or malformed"
        )
    python_executable = reported.get("python_executable")
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or not os.path.isabs(python_executable)
    ):
        mismatches.append(
            f"runtime provenance python_executable={python_executable!r} is missing "
            "or not an absolute path"
        )
    # Startup time format: ``started_at`` must be a parseable ISO-8601/RFC3339
    # timestamp so the worker's start instant is auditable.
    started_at = reported.get("started_at")
    if not isinstance(started_at, str) or not started_at:
        mismatches.append(f"runtime provenance started_at={started_at!r} is missing")
    else:
        try:
            datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            mismatches.append(
                f"runtime provenance started_at={started_at!r} is not a valid "
                "ISO-8601 timestamp"
            )
    return mismatches


def validate_runtime_provenance(
    runtime: dict[str, Any],
    repo_root: Path | None = None,
) -> list[str]:
    """Convenience wrapper: extract ``runtime_provenance`` from a runtime
    payload and compare it against the current tree."""
    reported = runtime.get("runtime_provenance") if isinstance(runtime, dict) else None
    expected = local_expected_provenance(repo_root)
    return provenance_mismatches(reported, expected)
