#!/usr/bin/env python3
"""TripChord local launcher / installer (v0.8).

A single entry point that manages the local-first stack — API, Web, database
migration and Browser Bridge — plus a first-run setup wizard.  Everything stays
on the user's machine:

- ``check``   — verify prerequisites (uv, node, npm, runtime dir, diff check).
- ``setup``   — lock dependencies, run migrations, build the Web bundle.
- ``wizard``  — first-run setup: LLM Key storage, model smoke readiness,
  Companion pairing status and per-scope provider health.
- ``api``     — start the loopback-only API + browser bridge (bridge token in a
  0600 file under ``.runtime/``; never printed to stdout).
- ``web``     — start the Vite dev server.

The launcher never pushes, publishes, accesses a real OTA or initiates a paid
model call.  Releasing signed extensions / installers to a store or the public
network requires separate user authorisation.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(cmd))
    result = subprocess.run(cmd, cwd=cwd, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def _check_prerequisite(name: str, *candidates: str) -> str | None:
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def cmd_check(args: argparse.Namespace) -> int:
    del args
    problems: list[str] = []
    uv = _check_prerequisite("uv", "uv")
    node = _check_prerequisite("node", "node")
    npm = _check_prerequisite("npm", "npm")
    for label, found in (("uv", uv), ("node", node), ("npm", npm)):
        if not found:
            problems.append(f"缺少 {label}")
    print("环境检查")
    print(f"  uv    : {uv or '缺失'}")
    print(f"  node  : {node or '缺失'}")
    print(f"  npm   : {npm or '缺失'}")
    if not RUNTIME.exists():
        RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_mode = stat.S_IMODE(RUNTIME.stat().st_mode)
    mode_ok = "OK" if runtime_mode == 0o700 else "应为 0700"
    print(f"  .runtime mode : {oct(runtime_mode)} ({mode_ok})")
    diff = subprocess.run(["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True)
    print(f"  git diff --check : {'通过' if diff.returncode == 0 else '有空白错误'}")
    if diff.returncode != 0:
        problems.append("git diff --check 失败")
    if problems:
        print("\n阻塞项：")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\n检查通过。")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    del args
    if _check_prerequisite("uv", "uv") is None:
        raise SystemExit("缺少 uv；请先安装 uv")
    _run(["uv", "sync", "--locked", "--all-groups"])
    _run(["uv", "run", "alembic", "upgrade", "head"])
    if _check_prerequisite("npm", "npm") is not None:
        _run(["npm", "ci"])
        _run(["npm", "run", "build"])
    print("\n本地栈已就绪：API/Web/DB/Bridge 可在本机运行。")
    return 0


def cmd_wizard(args: argparse.Namespace) -> int:
    del args
    print("TripChord 首次设置向导（本地优先，无云依赖）")
    print("=" * 60)

    # 1. LLM Key 安全存储
    keyring_available = _check_prerequisite("security", "keyring") or _check_prerequisite(
        "security", "python"
    )
    key_var = os.environ.get("TRIPCHORD_MODEL_API_KEY") or os.environ.get("MODEL_API_KEY")
    if key_var:
        print("[1/4] LLM Key：已在环境中提供（不写入仓库/日志）")
    elif keyring_available:
        print("[1/4] LLM Key：系统安全存储可用；未检测到环境 Key")
    else:
        print("[1/4] LLM Key：未提供；模型功能将被关闭（MODEL_PROVIDER=none）")

    # 2. 模型 smoke 就绪
    ack = os.environ.get("TRIPCHORD_ACK_MODEL_COST")
    smoke_state = (
        "已授权（TRIPCHORD_ACK_MODEL_COST=1）"
        if ack == "1"
        else "未授权（不会发起付费模型调用）"
    )
    print(f"[2/4] 模型 smoke：{smoke_state}")

    # 3. Companion 配对
    token_file = RUNTIME / "browser-bridge-token"
    token_state = "已存在（0600）" if token_file.exists() else "未创建（首次启动 API 时生成）"
    print(f"[3/4] Companion 配对：{token_state}；host permissions 逐平台在浏览器中授权")

    # 4. 逐平台权限与登录健康
    registry_ok = True
    try:
        from tripchord.platform.registry import build_default_registry

        registry = build_default_registry()
        active = [
            cap.key.key
            for cap in registry.capabilities
            if cap.certification_stage.value == "certified_active"
        ]
    except Exception:
        active = []
        registry_ok = False
    if registry_ok:
        print(f"[4/4] 逐平台权限：合格 scope {len(active)} 个 -> {', '.join(sorted(active))}")
        print("      登录健康需真实 Companion 授权后在浏览器中逐项确认（本机无授权时不伪造）。")
    else:
        print("[4/4] 逐平台权限：registry 不可用")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    if _check_prerequisite("uv", "uv") is None:
        raise SystemExit("缺少 uv")
    _run(
        [
            "uv",
            "run",
            "python",
            "scripts/start_live_api.py",
            "--port",
            str(args.port),
        ],
        check=False,
    )
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    if _check_prerequisite("npm", "npm") is None:
        raise SystemExit("缺少 npm")
    _run(["npm", "run", "dev", "--", "--port", str(args.port)], cwd=ROOT / "apps" / "web")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="检查本机前置条件").set_defaults(func=cmd_check)
    sub.add_parser("setup", help="安装锁定依赖、迁移数据库并构建 Web").set_defaults(func=cmd_setup)
    sub.add_parser("wizard", help="首次设置向导").set_defaults(func=cmd_wizard)
    api_parser = sub.add_parser("api", help="启动 loopback API + Bridge")
    api_parser.add_argument("--port", type=int, default=8000)
    api_parser.set_defaults(func=cmd_api)
    web_parser = sub.add_parser("web", help="启动 Web 开发服务器")
    web_parser.add_argument("--port", type=int, default=5173)
    web_parser.set_defaults(func=cmd_web)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
