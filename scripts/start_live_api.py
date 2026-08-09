from __future__ import annotations

import argparse
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

_TOKEN_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start the loopback-only TripChord live API and browser bridge.",
    )
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def _secure_runtime_directory(runtime_directory: Path) -> None:
    try:
        current = runtime_directory.lstat()
    except FileNotFoundError:
        runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        current = runtime_directory.lstat()

    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise SystemExit(f"运行时目录必须是真实目录，不能是符号链接：{runtime_directory}")
    if current.st_uid != os.getuid():
        raise SystemExit(f"运行时目录不属于当前用户：{runtime_directory}")
    runtime_directory.chmod(0o700)
    if stat.S_IMODE(runtime_directory.lstat().st_mode) != 0o700:
        raise SystemExit(f"无法把运行时目录限制为 0700：{runtime_directory}")


def _validate_bridge_token(token: str, token_file: Path) -> str:
    candidate = token.strip()
    if not _TOKEN_PATTERN.fullmatch(candidate):
        raise SystemExit(f"配对密钥文件内容无效，拒绝启动：{token_file}")
    return candidate


def _read_existing_bridge_token(token_file: Path) -> str:
    try:
        before_open = token_file.lstat()
    except FileNotFoundError as exc:
        raise SystemExit(f"配对密钥文件在读取前消失：{token_file}") from exc
    if stat.S_ISLNK(before_open.st_mode) or not stat.S_ISREG(before_open.st_mode):
        raise SystemExit(f"配对密钥必须是真实普通文件，不能是符号链接：{token_file}")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_file, flags)
    except OSError as exc:
        raise SystemExit(f"无法安全打开配对密钥文件：{token_file}") from exc
    try:
        opened = os.fstat(descriptor)
        after_open = token_file.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after_open.st_mode)
            or opened.st_dev != after_open.st_dev
            or opened.st_ino != after_open.st_ino
        ):
            raise SystemExit(f"配对密钥文件在读取期间发生替换：{token_file}")
        if opened.st_uid != os.getuid():
            raise SystemExit(f"配对密钥文件不属于当前用户：{token_file}")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise SystemExit(f"配对密钥文件权限必须严格为 0600：{token_file}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return _validate_bridge_token(stream.read(), token_file)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def load_or_create_bridge_token(
    token_file: Path,
    *,
    token_factory: Callable[[], str] | None = None,
) -> tuple[str, bool]:
    """Return a persistent local bridge secret without ever echoing it.

    The boolean is true only when this call created the file. Existing secrets
    are accepted only when they are current-user-owned regular files with exact
    mode 0600. This intentionally fails closed instead of silently repairing a
    secret file that may already have been exposed.
    """

    runtime_directory = token_file.parent
    _secure_runtime_directory(runtime_directory)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(token_file, flags, 0o600)
    except FileExistsError:
        return _read_existing_bridge_token(token_file), False
    except OSError as exc:
        raise SystemExit(f"无法安全创建配对密钥文件：{token_file}") from exc

    try:
        token = _validate_bridge_token(
            (token_factory or (lambda: secrets.token_hex(32)))(),
            token_file,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(f"{token}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        token_file.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return token, True


def _load_model_api_key(runtime_directory: Path, environment: dict[str, str]) -> None:
    """Inject the model API key from the 0600 key file when env does not carry it.

    The launchd job holds no secret in its plist (B2 hardening); the key file is
    a current-user-owned regular file with exact mode 0600.  The key is only ever
    placed into the uvicorn subprocess environment — never printed or logged.
    """
    if environment.get("MODEL_API_KEY") or environment.get("TRIPCHORD_MODEL_API_KEY"):
        return
    key_file = runtime_directory / "model-api-key"
    try:
        before = key_file.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        return
    if before.st_uid != os.getuid():
        return
    if stat.S_IMODE(before.st_mode) != 0o600:
        return
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if key:
        environment["MODEL_API_KEY"] = key
        environment["TRIPCHORD_MODEL_API_KEY"] = key


def run_live_api(*, port: int, repository: Path | None = None) -> int:
    if not 1 <= port <= 65_535:
        raise SystemExit("--port must be between 1 and 65535")

    repository = repository or Path(__file__).resolve().parents[1]
    companion = repository / "apps" / "browser-companion"
    runtime_directory = repository / ".runtime"
    token_file = runtime_directory / "browser-bridge-token"
    token, created = load_or_create_bridge_token(token_file)
    bridge_url = f"http://127.0.0.1:{port}/browser-bridge"

    environment = os.environ.copy()
    environment["TRIPCHORD_BROWSER_BRIDGE_ENABLED"] = "true"
    environment["TRIPCHORD_BROWSER_BRIDGE_TOKEN"] = token
    environment["TRIPCHORD_BROWSER_COMPANION_AUTO_RELOAD_ENABLED"] = "true"
    _load_model_api_key(runtime_directory, environment)

    print("TripChord 实时只读服务即将启动。", flush=True)
    print(f"Chrome 加载目录：{companion}", flush=True)
    print(f"Companion 地址：{bridge_url}", flush=True)
    state = "首次安全创建" if created else "已安全复用"
    print(f"配对密钥文件：{token_file}（{state}，权限 0600，不在终端回显）", flush=True)
    print(f"复制令牌：pbcopy < '{token_file}'", flush=True)
    print("密钥会跨 API 重启保留；首次配对后无需再次粘贴。", flush=True)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tripchord.main:app",
            "--app-dir",
            "apps/api/src",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        check=False,
        cwd=repository,
        env=environment,
    )
    return completed.returncode


def main() -> None:
    args = _arguments()
    raise SystemExit(run_live_api(port=args.port))


if __name__ == "__main__":
    main()
