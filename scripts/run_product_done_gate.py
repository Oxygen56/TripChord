#!/usr/bin/env python3
"""Machine-executable six-layer product Done-Gate (v1.0).

Runs the local engineering layers (reproducibility, replay, clean-Chrome
malicious fixtures, authorized model smoke) and honestly reports the real
platform canary layer as pending user authorization when no live Companion /
authorized OTA session is present.

The gate writes ``benchmarks/results/product-v1-done-gate.json`` atomically.
``passed=true`` is only allowed when every layer applicable to the current
declaration passes.  HTTP job success, test success, model call success or all
Source terminal states never alone trigger a pass.

Layer reference (contract section 六 v1.0):
  1. fresh clone / lockfile / migration / wheel / web / container / installer
     reproducible;
  2. replay mode full dynamic selection -> all-terminal -> cross-platform plan
     -> handoff -> user confirm -> protected replan;
  3. clean Chrome + local malicious fixture: permissions, pairing, background,
     URL and Prompt Injection;
  4. OpenAI-compatible model (authorized env) required-model smoke + structured
     Agent chain;
  5. every declared-certified real provider x vertical has a fresh authorised
     read-only canary;
  6. full-platform real E2E only when all external conditions are met; when not
     met, report exactly which external gate is not met, never forge it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "benchmarks" / "results"
OUTPUT_PATH = RESULTS_DIR / "product-v1-done-gate.json"
EVIDENCE_SCHEMA = "tripchord-product-v1-done-gate"

_MODEL_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TRIPCHORD_MODEL_API_KEY")


@dataclass
class LayerResult:
    name: str
    passed: bool
    skipped: bool = False
    detail: str = ""
    sub_checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class GateReport:
    schema_version: str
    generated_at: str
    commit_sha: str | None
    layers: list[LayerResult]
    passed: bool
    summary: str
    boundary: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return result.stdout.strip() or None
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


def _run(
    cmd: list[str],
    *,
    cwd: Path = ROOT,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout[-2000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "command not found"


def layer1_reproducibility() -> LayerResult:
    """Fresh-clone reproducibility: migration + web build + API import."""
    checks: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite+aiosqlite:///{tmp}/gate.db"
        env = dict(os.environ)
        env["TRIPCHORD_DATABASE_URL"] = db_url
        code, _ = _run(
            ["uv", "run", "alembic", "upgrade", "head"],
            timeout=300,
            cwd=ROOT,
            env=env,
        )
        checks.append({"name": "alembic_upgrade_head", "passed": code == 0})
        code2, _ = _run(
            ["uv", "run", "alembic", "check"],
            timeout=300,
            cwd=ROOT,
            env=env,
        )
        checks.append({"name": "alembic_check", "passed": code2 == 0})
        code3, out3 = _run(["npm", "run", "build"])
        checks.append({"name": "web_build", "passed": code3 == 0, "detail": out3[-200:]})
        code4, _ = _run(["uv", "run", "python", "-c", "import tripchord.main"])
        checks.append({"name": "api_import", "passed": code4 == 0})
        code5, _ = _run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "pytest",
                "apps/api/tests/test_secret_redaction.py",
                "-q",
            ],
            timeout=600,
        )
        checks.append({"name": "secret_redaction", "passed": code5 == 0})
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="1_reproducibility",
        passed=passed,
        detail="migration upgrade/check, web build, API import, secret redaction",
        sub_checks=checks,
    )


def layer2_replay() -> LayerResult:
    """Replay-mode core benchmarks (no real OTA access)."""
    checks: list[dict[str, Any]] = []
    commands = (
        ("benchmarks.evaluate", "benchmark_verifier"),
        ("benchmarks.evaluate_planning", "benchmark_planning"),
        ("benchmarks.evaluate_repair", "benchmark_repair"),
        ("benchmarks.evaluate_events", "benchmark_events"),
    )
    for module, label in commands:
        code, out = _run(["uv", "run", "python", "-m", module], timeout=600)
        passed = code == 0
        checks.append(
            {"name": label, "passed": passed, "detail": out[-300:] if not passed else ""}
        )
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="2_replay",
        passed=passed,
        detail="verifier/planning/repair/events benchmarks",
        sub_checks=checks,
    )


def layer3_clean_chrome_fixtures() -> LayerResult:
    """Clean-Chrome malicious fixture gates (browser bridge + handoff URL policy)."""
    checks: list[dict[str, Any]] = []
    code, out = _run(
        ["uv", "run", "pytest", "apps/api/tests/test_official_handoff.py", "-q"],
        timeout=600,
    )
    checks.append(
        {"name": "handoff_url_policy", "passed": code == 0, "detail": out[-300:] if code else ""}
    )
    code2, out2 = _run(
        [
            "uv",
            "run",
            "pytest",
            "apps/api/tests/test_browser_bridge.py",
            "-q",
            "-k",
            "permission or pair or background or redirect",
        ],
        timeout=600,
    )
    checks.append(
        {
            "name": "browser_bridge_permissions",
            "passed": code2 == 0,
            "detail": out2[-300:] if code2 else "",
        }
    )
    passed = all(item["passed"] for item in checks)
    return LayerResult(
        name="3_clean_chrome_fixtures",
        passed=passed,
        detail="handoff URL policy + browser bridge permission fixtures",
        sub_checks=checks,
    )


def layer4_model_smoke() -> LayerResult:
    """OpenAI-compatible required-model smoke when a key is authorised."""
    authorized = any(os.environ.get(var) for var in _MODEL_ENV_VARS)
    if not authorized:
        return LayerResult(
            name="4_model_smoke",
            passed=False,
            skipped=True,
            detail="no model API key authorised in environment; skipped (not failed)",
        )
    code, out = _run(
        ["uv", "run", "python", "scripts/run_model_runtime_smoke.py"],
        timeout=600,
    )
    return LayerResult(
        name="4_model_smoke",
        passed=code == 0,
        detail=out[-500:] if code else "required-model smoke passed",
    )


def layer5_real_canary() -> LayerResult:
    """Every declared-certified real provider x vertical needs a live canary.

    This layer cannot pass without a user-authorised, logged-in Companion on the
    local machine.  When the bridge token is absent we report the precise
    external gate that is not met rather than forging a pass.
    """
    bridge_token = os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN")
    if not bridge_token:
        return LayerResult(
            name="5_real_canary",
            passed=False,
            detail=(
                "pending user authorization: set TRIPCHORD_BROWSER_BRIDGE_TOKEN, "
                "pair the Companion and keep the official OTA domains logged in; "
                "then re-run this gate"
            ),
        )
    code, out = _run(
        ["uv", "run", "python", "benchmarks/live_canary.py"],
        timeout=900,
    )
    return LayerResult(
        name="5_real_canary",
        passed=code == 0,
        detail=out[-500:] if code else "live read-only canary passed",
    )


def layer6_full_e2e() -> LayerResult:
    """Full-platform real E2E only when every external condition is met."""
    bridge_token = os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN")
    if not bridge_token:
        return LayerResult(
            name="6_full_e2e",
            passed=False,
            detail=(
                "pending user authorization: full real E2E requires the same "
                "authorised Companion session as layer 5; not attempted"
            ),
        )
    return LayerResult(
        name="6_full_e2e",
        passed=False,
        detail="layer 6 is gated behind layer 5; run after the real canary passes",
    )


def _applicable(layers: list[LayerResult]) -> list[LayerResult]:
    return [layer for layer in layers if not layer.skipped]


def run_gate(*, commit: str | None) -> GateReport:
    layers = [
        layer1_reproducibility(),
        layer2_replay(),
        layer3_clean_chrome_fixtures(),
        layer4_model_smoke(),
        layer5_real_canary(),
        layer6_full_e2e(),
    ]
    applicable = _applicable(layers)
    passed = bool(applicable) and all(layer.passed for layer in applicable)
    summary = (
        "all applicable Done-Gate layers passed"
        if passed
        else "one or more Done-Gate layers are not satisfied"
    )
    boundary = (
        "本次判定仅覆盖当前发布声明适用的本地工程门；真实平台 canary 与全平台 "
        "E2E 需用户授权官方域名并保持登录态后才能声明通过。"
        "HTTP 任务成功、测试成功、模型调用成功或全部 Source 终态均不单独构成通过。"
    )
    return GateReport(
        schema_version=EVIDENCE_SCHEMA,
        generated_at=_now(),
        commit_sha=commit,
        layers=layers,
        passed=passed,
        summary=summary,
        boundary=boundary,
    )


def _dump(report: GateReport, output_path: Path = OUTPUT_PATH) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(".json.tmp")
    payload = json.dumps(
        asdict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print the verdict")
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="atomic output JSON path",
    )
    args = parser.parse_args()

    report = run_gate(commit=_commit_sha())
    output_path = _dump(report, args.output)

    if args.quiet:
        print(
            json.dumps(
                {"passed": report.passed, "summary": report.summary},
                sort_keys=True,
            )
        )
        return 0 if report.passed else 2

    print(f"TripChord product v1.0 Done-Gate  {report.generated_at}")
    print(f"commit: {report.commit_sha or 'unknown'}")
    for layer in report.layers:
        marker = "PASS" if layer.passed else ("SKIP" if layer.skipped else "FAIL")
        print(f"  [{marker}] {layer.name}  {layer.detail}")
    print(f"\nverdict: {report.summary}")
    print(f"boundary: {report.boundary}")
    print(f"evidence: {output_path}")
    return 0 if report.passed else 2


if __name__ == "__main__":
    sys.exit(main())
