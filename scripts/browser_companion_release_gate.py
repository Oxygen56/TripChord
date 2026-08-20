from __future__ import annotations

import argparse
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tripchord.providers.browser_bridge import BrowserCompanionBuildIdentity

_JS_CONTRACTS = (
    "apps/browser-companion/tests/companion-config.test.mjs",
    "apps/browser-companion/tests/parser-contract.test.mjs",
    "apps/browser-companion/tests/content-adapter-contract.test.mjs",
    "apps/browser-companion/tests/background-lifecycle.test.mjs",
)
_RELEASE_SEAL_RELATIVE_PATH = ".tripchord-release-seal.json"
_PYTHON_CONTRACTS = (
    "apps/api/tests/test_companion_control_api.py",
    "apps/api/tests/test_companion_control_tools.py",
    "scripts/tests/test_start_live_api.py",
    "scripts/tests/test_browser_companion_release_gate.py",
)
_SECRET_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "MODEL_API_KEY",
    "TRIPCHORD_ANTHROPIC_API_KEY",
    "TRIPCHORD_BROWSER_BRIDGE_CONTROL_TOKEN",
    "TRIPCHORD_BROWSER_BRIDGE_TOKEN",
    "TRIPCHORD_MODEL_API_KEY",
)


class ReleaseGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class _FileSnapshot:
    payload: bytes
    mode: int


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed release gate for the TripChord Browser Companion. "
            "The default mode never writes release metadata."
        )
    )
    parser.add_argument(
        "--update-build-meta",
        action="store_true",
        help=(
            "explicitly regenerate src/build-meta.js before verification; "
            "manifest and runtime versions are never changed"
        ),
    )
    parser.add_argument(
        "--ci-verify-key-free",
        action="store_true",
        help=(
            "explicitly verify a source-derived release seal in a temporary copy; "
            "never writes the workstation runtime seal"
        ),
    )
    return parser.parse_args()


def _repository() -> Path:
    return Path(__file__).resolve().parents[1]


def _test_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in _SECRET_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["MODEL_PROVIDER"] = "none"
    environment["TRIPCHORD_BROWSER_BRIDGE_ENABLED"] = "false"
    return environment


def _run_checked(
    label: str,
    command: Sequence[str],
    *,
    repository: Path,
    environment: dict[str, str] | None = None,
) -> None:
    print(f"[release-gate] {label}", flush=True)
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            cwd=repository,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise ReleaseGateError(f"{label} 无法启动：缺少 {command[0]}") from exc
    if completed.returncode != 0:
        raise ReleaseGateError(f"{label} 失败，退出码 {completed.returncode}")


def _companion_directory(repository: Path) -> Path:
    return repository / "apps" / "browser-companion"


def _release_seal_path(repository: Path) -> Path:
    return _companion_directory(repository) / _RELEASE_SEAL_RELATIVE_PATH


def _snapshot_regular_file(path: Path, *, required: bool) -> _FileSnapshot | None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        if required:
            raise ReleaseGateError(f"发布事务缺少必需文件：{path}") from None
        return None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ReleaseGateError(f"发布事务拒绝非普通文件：{path}")
    try:
        return _FileSnapshot(
            payload=path.read_bytes(),
            mode=stat.S_IMODE(file_stat.st_mode),
        )
    except OSError as exc:
        raise ReleaseGateError(f"无法读取发布事务文件：{path}") from exc


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        raise ReleaseGateError(f"无法打开发布目录以同步：{directory}") from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise ReleaseGateError(f"无法持久化发布目录：{directory}") from exc
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    """Commit one generated artifact by same-directory fsync + atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, mode)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ReleaseGateError(f"无法原子写入发布文件：{path}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _remove_release_file(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(file_stat.st_mode) and not stat.S_ISLNK(file_stat.st_mode):
        raise ReleaseGateError(f"拒绝删除占用发布路径的目录：{path}")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except OSError as exc:
        raise ReleaseGateError(f"无法失效旧发布 seal：{path}") from exc


def _restore_file(path: Path, snapshot: _FileSnapshot | None) -> None:
    if snapshot is None:
        _remove_release_file(path)
        return
    _atomic_write_bytes(path, snapshot.payload, mode=snapshot.mode)


def _ensure_api_source(repository: Path) -> None:
    api_source = repository / "apps" / "api" / "src"
    if str(api_source) not in sys.path:
        sys.path.insert(0, str(api_source))


def verify_candidate_build_metadata(repository: Path) -> str:
    """Validate build-meta against source without granting reload authority."""

    _ensure_api_source(repository)
    try:
        from tripchord.agents.companion_control_tools import (
            CompanionControlToolError,
            candidate_companion_build_identity,
        )
    except ImportError as exc:
        raise ReleaseGateError("无法加载生产构建验证器；请先安装项目依赖") from exc
    try:
        identity = candidate_companion_build_identity(_companion_directory(repository))
    except CompanionControlToolError as exc:
        raise ReleaseGateError(f"Browser Companion build-meta 不是当前构建：{exc}") from exc
    return identity.build_sha256


def verify_build_metadata(repository: Path) -> str:
    """Validate source, metadata, and the post-contract atomic release seal."""

    _ensure_api_source(repository)
    try:
        from tripchord.agents.companion_control_tools import (
            CompanionControlToolError,
            verified_companion_build_identity,
        )
    except ImportError as exc:
        raise ReleaseGateError("无法加载生产构建验证器；请先安装项目依赖") from exc
    try:
        identity = verified_companion_build_identity(_companion_directory(repository))
    except CompanionControlToolError as exc:
        raise ReleaseGateError(f"Browser Companion 发布 seal 无效：{exc}") from exc
    return identity.build_sha256


def _candidate_release_seal(repository: Path) -> bytes:
    _ensure_api_source(repository)
    try:
        from tripchord.agents.companion_control_tools import (
            CompanionControlToolError,
            companion_release_seal_bytes,
        )
    except ImportError as exc:
        raise ReleaseGateError("无法加载生产构建验证器；请先安装项目依赖") from exc
    try:
        return companion_release_seal_bytes(_companion_directory(repository))
    except CompanionControlToolError as exc:
        raise ReleaseGateError(f"无法生成候选发布 seal：{exc}") from exc


def _verify_ci_candidate_without_runtime_seal(
    repository: Path,
    *,
    expected_build_sha256: str,
    candidate_seal: bytes,
) -> None:
    """Run the normal sealed verifier against an ephemeral source-derived tree.

    CI must prove the exact same seal contract as a workstation without
    materialising the owner-only runtime seal in the checkout.  The copied
    tree contains only the fixed release inputs and generated metadata; the
    temporary seal is mode 0600 and is removed with the temporary directory.
    """

    _ensure_api_source(repository)
    try:
        from tripchord.agents.companion_control_tools import (
            COMPANION_BUILD_FILE_ALLOWLIST,
            CompanionControlToolError,
            verified_companion_build_identity,
        )
    except ImportError as exc:
        raise ReleaseGateError("无法加载生产构建验证器；请先安装项目依赖") from exc

    source = _companion_directory(repository)
    with tempfile.TemporaryDirectory(prefix="tripchord-companion-ci-") as temporary_root:
        temporary_companion = Path(temporary_root)
        for relative_path in (*COMPANION_BUILD_FILE_ALLOWLIST, "src/build-meta.js"):
            source_path = source / relative_path
            destination = temporary_companion / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                source_stat = source_path.lstat()
                if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
                    raise ReleaseGateError(
                        f"CI 发布校验拒绝非普通文件：{source_path}"
                    )
                destination.write_bytes(source_path.read_bytes())
            except OSError as exc:
                raise ReleaseGateError(f"无法准备 CI 临时构建输入：{source_path}") from exc
        seal_path = temporary_companion / _RELEASE_SEAL_RELATIVE_PATH
        try:
            seal_path.write_bytes(candidate_seal)
            seal_path.chmod(0o600)
        except OSError as exc:
            raise ReleaseGateError("无法写入 CI 临时发布 seal") from exc
        try:
            identity = verified_companion_build_identity(temporary_companion)
        except CompanionControlToolError as exc:
            # Preserve the fail-closed production error boundary while adding
            # enough context to distinguish CI candidate verification failures.
            raise ReleaseGateError(f"CI 临时发布 seal 无效：{exc}") from exc
        if identity.build_sha256 != expected_build_sha256:
            raise ReleaseGateError("CI 临时发布 seal 验证了错误的构建")


def verify_ci_candidate_build_metadata(repository: Path) -> BrowserCompanionBuildIdentity:
    """Verify a source-derived candidate seal without requiring local runtime state.

    This is for key-free CI and deterministic tests only.  It deliberately
    never writes the repository's owner-only runtime seal.
    """

    _ensure_api_source(repository)
    try:
        from tripchord.agents.companion_control_tools import (
            CompanionControlToolError,
            candidate_companion_build_identity,
        )
    except ImportError as exc:
        raise ReleaseGateError("无法加载生产构建验证器；请先安装项目依赖") from exc
    try:
        identity = candidate_companion_build_identity(_companion_directory(repository))
    except CompanionControlToolError as exc:
        raise ReleaseGateError(f"Browser Companion build-meta 不是当前构建：{exc}") from exc
    _verify_ci_candidate_without_runtime_seal(
        repository,
        expected_build_sha256=identity.build_sha256,
        candidate_seal=_candidate_release_seal(repository),
    )
    return identity


def _run_contracts(repository: Path) -> None:
    environment = _test_environment()
    for contract in _JS_CONTRACTS:
        _run_checked(
            f"Companion JS 合同：{Path(contract).name}",
            ("node", contract),
            repository=repository,
            environment=environment,
        )
    _run_checked(
        "API 控制面与启动安全定向测试",
        (sys.executable, "-m", "pytest", "-q", *_PYTHON_CONTRACTS),
        repository=repository,
        environment=environment,
    )


def run_release_gate(
    *,
    repository: Path,
    update_build_meta: bool = False,
    ci_verify_key_free: bool = False,
) -> str:
    if update_build_meta and ci_verify_key_free:
        raise ReleaseGateError("--update-build-meta 与 --ci-verify-key-free 不能同时使用")
    if ci_verify_key_free:
        print("[release-gate] CI key-free 临时核对 build-meta + release seal", flush=True)
        build_sha256 = verify_ci_candidate_build_metadata(repository).build_sha256
        candidate_seal = _candidate_release_seal(repository)
        _run_contracts(repository)
        if verify_candidate_build_metadata(repository) != build_sha256:
            raise ReleaseGateError("候选构建在发布合同执行期间发生变化")
        if _candidate_release_seal(repository) != candidate_seal:
            raise ReleaseGateError("候选发布身份在合同执行期间发生变化")
        print(f"[release-gate] PASS CI key-free build_sha256={build_sha256}", flush=True)
        return build_sha256
    if not update_build_meta:
        print("[release-gate] 只读核对 build-meta + release seal", flush=True)
        build_sha256 = verify_build_metadata(repository)
        _run_contracts(repository)
        if verify_build_metadata(repository) != build_sha256:
            raise ReleaseGateError("构建在发布合同执行期间发生变化")
        print(f"[release-gate] PASS build_sha256={build_sha256}", flush=True)
        return build_sha256

    companion = _companion_directory(repository)
    metadata_path = companion / "src" / "build-meta.js"
    seal_path = _release_seal_path(repository)
    previous_metadata = _snapshot_regular_file(metadata_path, required=True)
    previous_seal = _snapshot_regular_file(seal_path, required=False)
    _remove_release_file(seal_path)
    print("[release-gate] 旧 release seal 已失效，自动重载保持关闭", flush=True)
    try:
        _run_checked(
            "生成未封印候选 build-meta",
            ("node", "apps/browser-companion/scripts/update-build-meta.mjs"),
            repository=repository,
        )
        build_sha256 = verify_candidate_build_metadata(repository)
        candidate_seal = _candidate_release_seal(repository)
        _run_contracts(repository)
        if verify_candidate_build_metadata(repository) != build_sha256:
            raise ReleaseGateError("候选构建在发布合同执行期间发生变化")
        if _candidate_release_seal(repository) != candidate_seal:
            raise ReleaseGateError("候选发布身份在合同执行期间发生变化")
        _atomic_write_bytes(seal_path, candidate_seal, mode=0o600)
        if verify_build_metadata(repository) != build_sha256:
            raise ReleaseGateError("原子 seal 写入后未能验证相同构建")
    except BaseException:
        try:
            _restore_file(metadata_path, previous_metadata)
            _restore_file(seal_path, previous_seal)
        except ReleaseGateError as restore_exc:
            raise ReleaseGateError(
                "发布失败且旧状态恢复失败；当前构建保持不可发布"
            ) from restore_exc
        raise
    print(f"[release-gate] PASS + SEALED build_sha256={build_sha256}", flush=True)
    return build_sha256


def main() -> None:
    args = _arguments()
    try:
        run_release_gate(
            repository=_repository(),
            update_build_meta=args.update_build_meta,
            ci_verify_key_free=args.ci_verify_key_free,
        )
    except ReleaseGateError as exc:
        print(f"[release-gate] FAIL: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
