#!/usr/bin/env python3
"""Clean-Chrome + local fixture browser E2E for the TripChord SPA (v0.9).

Boots a local replay-mode API on an ephemeral SQLite database and a static
server that serves the built SPA (``apps/web/dist``) while proxying ``/api``,
``/health`` and ``/ready`` to the API, then drives a *clean* headless Chrome
(no extensions, no profile) through the DevTools protocol to verify:

  1. the four-stage workflow-steps nav renders with the requirement step
     active and the correct aria label;
  2. the replay planning form submits against the local fixture backend and a
     persisted plan renders (day blocks + timeline items + replay truth note);
  3. the workflow step advances to the plan stage and no error banner appears.

No Playwright/Puppeteer dependency: the page is driven over CDP with the
``websockets`` package (already present through ``uvicorn[standard]``).

Writes ``benchmarks/results/browser-e2e.json`` and a screenshot to
``benchmarks/results/browser-e2e-screenshot.png``.  Exit code 0 only when all
assertions pass.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import http.server
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "apps" / "web" / "dist"
RESULTS_DIR = ROOT / "benchmarks" / "results"
OUTPUT_PATH = RESULTS_DIR / "browser-e2e.json"
SCREENSHOT_PATH = RESULTS_DIR / "browser-e2e-screenshot.png"
EVIDENCE_SCHEMA = "tripchord-browser-e2e-v1"
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)
# Endpoints the SPA reaches with relative paths; everything else on the static
# origin is a built asset and is served from disk.
_PROXY_PREFIXES = ("/api", "/browser-bridge", "/health", "/ready")

_CDP_TIMEOUT = 3.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_chrome() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium")
    return found


def _loopback_client() -> httpx.Client:
    # Loopback only: never route through the ambient HTTP(S)_PROXY.
    return httpx.Client(base_url="http://127.0.0.1", trust_env=False)


def _wait_for_http(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    with _loopback_client() as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url, timeout=2.0)
                if response.status_code < 500:
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
    raise RuntimeError(f"endpoint {url} did not become ready: {last_error}")


class _StaticAndProxyHandler(http.server.BaseHTTPRequestHandler):
    """Serve the built SPA from disk; stream-proxy API endpoints to the API."""

    protocol_version = "HTTP/1.1"
    api_port: int = 0

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s\n" % (format % args))

    def _proxy(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection", "accept-encoding"}
            }
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                self.api_port,
                timeout=30,
            )
            try:
                connection.request(
                    self.command,
                    self.path,
                    body=body,
                    headers=headers,
                )
                response = connection.getresponse()
            except Exception as exc:
                self._send_simple(502, f"proxy to API failed: {exc}".encode())
                return
            try:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() in {"transfer-encoding", "connection", "content-length"}:
                        continue
                    self.send_header(key, value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    self.wfile.write(b"%X\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            finally:
                connection.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_simple(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            path = "/index.html"
        candidate = (WEB_DIST / path.lstrip("/")).resolve()
        try:
            candidate.relative_to(WEB_DIST.resolve())
        except ValueError:
            self._send_simple(403, b"forbidden")
            return
        if not candidate.is_file():
            self._send_simple(404, b"not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        body = candidate.read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:
        if any(self.path.startswith(prefix) for prefix in _PROXY_PREFIXES):
            self._proxy()
        else:
            self._serve_static()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()


class _CdpClient:
    """Minimal CDP client for request/response calls over one websocket."""

    def __init__(self, socket_url: str) -> None:
        self._socket_url = socket_url
        self._ws: Any = None
        self._next_id = 0

    async def __aenter__(self) -> _CdpClient:
        self._ws = await websockets.connect(
            self._socket_url,
            max_size=2**28,
            open_timeout=_CDP_TIMEOUT,
            proxy=None,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._ws.close()

    async def send(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        message_id = self._next_id
        await self._ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    async def evaluate(self, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"page evaluation failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")


def _start_api(database_path: Path, port: int) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["TRIPCHORD_DATABASE_URL"] = f"sqlite+aiosqlite:///{database_path}"
    environment["TRIPCHORD_ENV"] = "test"
    environment["TRIPCHORD_AUTH_REQUIRED"] = "false"
    process = subprocess.Popen(
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
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return process


def _start_web(port: int, api_port: int) -> http.server.ThreadingHTTPServer:
    handler = type("_Handler", (_StaticAndProxyHandler,), {"api_port": api_port})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    return server


def _read_debug_port(profile_dir: Path, timeout: float = 30.0) -> int:
    marker = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.is_file():
            lines = marker.read_text(encoding="utf-8").splitlines()
            if lines and lines[0].strip().isdigit():
                return int(lines[0].strip())
        time.sleep(0.2)
    raise RuntimeError("Chrome did not publish a DevToolsActivePort")


async def _snapshot_page(client: _CdpClient) -> dict[str, Any]:
    expression = """
    (() => {
      const steps = [...document.querySelectorAll('.workflow-step')];
      return {
        readyState: document.readyState,
        title: document.title,
        stepLabels: steps.map(
          s => s.querySelector('.workflow-step-body strong')?.textContent?.trim() ?? ''),
        stepDetails: steps.map(
          s => s.querySelector('.workflow-step-body small')?.textContent?.trim() ?? ''),
        activeIndex: steps.findIndex(s => s.classList.contains('active')),
        navLabel: document.querySelector('nav.workflow-steps')
          ?.getAttribute('aria-label') ?? '',
        statusPill: document.querySelector('.status-pill')?.textContent?.trim() ?? '',
        replayTabSelected: [...document.querySelectorAll('.mode-switch button')]
          .some(b => b.getAttribute('aria-selected') === 'true'
            && b.textContent?.includes('回放演示')),
        hasSubmit: [...document.querySelectorAll('button[type=submit]')]
          .some(b => b.textContent?.includes('开始回放规划')),
        panelStep: document.querySelector('.plan-panel-step')?.textContent?.trim() ?? '',
        planTitle: document.querySelector('.plan-header h2')?.textContent?.trim() ?? '',
        dayBlocks: document.querySelectorAll('.day-block').length,
        timelineItems: document.querySelectorAll('.timeline-item').length,
        truthNote: document.querySelector('.truth-note')?.textContent?.trim() ?? '',
        agentConsole: !!document.querySelector('.agent-console'),
        errorBanner: document.querySelector('.error-banner')?.textContent?.trim() ?? '',
      };
    })()
    """
    value = await client.evaluate(expression)
    return value if isinstance(value, dict) else {}


async def _run_browser_e2e(web_url: str, debug_port: int) -> dict[str, Any]:
    # Use the existing blank page target (clean Chrome starts with about:blank).
    with _loopback_client() as client:
        response = client.get(f"http://127.0.0.1:{debug_port}/json/list", timeout=5.0)
        response.raise_for_status()
        targets = response.json()
    page = next((item for item in targets if item.get("type") == "page"), None)
    if page is None:
        raise RuntimeError("no page target available in clean Chrome")
    ws_url = page["webSocketDebuggerUrl"]

    async with _CdpClient(ws_url) as client:
        await client.send("Page.enable")
        await client.send("Runtime.enable")
        await client.send("Page.navigate", {"url": web_url})

        # Phase 1: the SPA shell with the workflow-steps nav.
        initial: dict[str, Any] | None = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            snapshot = await _snapshot_page(client)
            if (
                snapshot["readyState"] == "complete"
                and snapshot["title"]
                and snapshot["stepLabels"]
            ):
                initial = snapshot
                break
            await asyncio.sleep(0.25)
        if initial is None:
            raise AssertionError("SPA did not render the workflow-steps nav")

        # Phase 2: point the replay form at a destination the offline catalog
        # supports (the SPA default 马累 is a live-mode scenario), then submit.
        filled = await client.evaluate(
            """
            (() => {
              const setValue = (input, value) => {
                const proto = input.tagName === 'TEXTAREA'
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, value);
                input.dispatchEvent(new Event('input', { bubbles: true }));
              };
              const fields = {
                '从哪里出发': '上海',
                '去哪里': '北京',
                '出发日期': '2026-10-02',
                '返程日期': '2026-10-03',
                '兴趣偏好': '历史',
                '必须安排': '故宫',
              };
              for (const [text, value] of Object.entries(fields)) {
                const label = [...document.querySelectorAll('form.trip-form label')]
                  .find(item => item.textContent?.trim().startsWith(text));
                const input = label?.querySelector('input, textarea');
                if (!input) return false;
                setValue(input, value);
              }
              return true;
            })()
            """
        )
        if not filled:
            raise AssertionError("replay form fields not found")
        await asyncio.sleep(0.2)
        clicked = await client.evaluate(
            """
            (() => {
              const btn = [...document.querySelectorAll('button[type=submit]')]
                .find(b => b.textContent?.includes('开始回放规划'));
              if (!btn) return false;
              btn.click();
              return true;
            })()
            """
        )
        if not clicked:
            raise AssertionError("replay submit button not found")

        # Phase 3: the persisted plan renders.
        final: dict[str, Any] | None = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            snapshot = await _snapshot_page(client)
            if snapshot["planTitle"] and snapshot["dayBlocks"] > 0:
                final = snapshot
                break
            await asyncio.sleep(0.5)
        if final is None:
            last = await _snapshot_page(client)
            raise AssertionError(
                f"plan did not render; last snapshot: {json.dumps(last, ensure_ascii=False)}"
            )

        screenshot = await client.send("Page.captureScreenshot", {"format": "png"})
        return {"initial": initial, "final": final, "screenshot_data": screenshot["data"]}


def _assertions(initial: dict[str, Any], final: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "detail": detail})

    labels_ok = initial["stepLabels"] == ["需求", "平台", "进度", "方案"]
    record(
        "workflow_steps_four_stages",
        labels_ok,
        "labels=" + "/".join(initial["stepLabels"]),
    )
    record(
        "workflow_nav_aria_label",
        initial["navLabel"] == "自由行规划工作流步骤",
        f"aria-label={initial['navLabel']!r}",
    )
    record(
        "requirement_step_active_initially",
        initial["activeIndex"] == 0,
        f"activeIndex={initial['activeIndex']}",
    )
    record(
        "replay_mode_selected",
        bool(initial["replayTabSelected"]),
        f"statusPill={initial['statusPill']!r}",
    )
    record("replay_submit_present", bool(initial["hasSubmit"]), "")

    record(
        "plan_stage_active",
        final["activeIndex"] == 3,
        f"activeIndex={final['activeIndex']}",
    )
    record(
        "plan_panel_step_four",
        "STEP 4 · 方案" in final["panelStep"],
        f"panelStep={final['panelStep']!r}",
    )
    record("plan_day_blocks", final["dayBlocks"] > 0, f"dayBlocks={final['dayBlocks']}")
    record(
        "plan_timeline_items",
        final["timelineItems"] > 0,
        f"timelineItems={final['timelineItems']}",
    )
    record(
        "replay_truth_note_visible",
        "回放" in final["truthNote"] and "沙箱" in final["truthNote"],
        f"truthNote={final['truthNote']!r}",
    )
    record("no_error_banner", final["errorBanner"] == "", f"error={final['errorBanner']!r}")
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-port", type=int, default=0)
    parser.add_argument("--web-port", type=int, default=0)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument(
        "--output-json",
        type=Path,
        default=OUTPUT_PATH,
        help="atomic evidence JSON output path (default: benchmarks/results/browser-e2e.json)",
    )
    parser.add_argument(
        "--output-screenshot",
        type=Path,
        default=SCREENSHOT_PATH,
        help="screenshot PNG output path (default: benchmarks/results/browser-e2e-screenshot.png)",
    )
    args = parser.parse_args()
    output_path = args.output_json
    screenshot_path_out = args.output_screenshot

    chrome = _find_chrome()
    if chrome is None:
        print("SKIP: no Google Chrome / Chromium binary found", flush=True)
        return 2
    if not WEB_DIST.is_dir():
        print(f"SKIP: built SPA missing at {WEB_DIST}", flush=True)
        return 2

    api_port = args.api_port or _free_port()
    web_port = args.web_port or _free_port()

    with tempfile.TemporaryDirectory(prefix="tripchord-e2e-") as temporary:
        temp_dir = Path(temporary)
        database_path = temp_dir / "e2e.db"
        profile_dir = temp_dir / "chrome-profile"

        api = _start_api(database_path, api_port)
        web_server = _start_web(web_port, api_port)
        import threading

        web_thread = threading.Thread(target=web_server.serve_forever, daemon=True)
        web_thread.start()
        debug_port: int | None = None
        chrome_process: subprocess.Popen[bytes] | None = None
        try:
            api_url = f"http://127.0.0.1:{api_port}/ready"
            _wait_for_http(api_url)
            web_url = f"http://127.0.0.1:{web_port}/"

            chrome_process = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-default-apps",
                    "--mute-audio",
                    "--remote-debugging-port=0",
                    f"--user-data-dir={profile_dir}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            debug_port = _read_debug_port(profile_dir)

            checks: list[dict[str, Any]] = []
            screenshot_path: str | None = None
            result: dict[str, Any] = {}
            try:
                evidence = asyncio.run(_run_browser_e2e(f"{web_url}", debug_port))
                checks = _assertions(evidence["initial"], evidence["final"])
                passed = all(item["passed"] for item in checks)
                screenshot_data = evidence["screenshot_data"]
                screenshot_bytes = __import__("base64").b64decode(screenshot_data.encode("ascii"))
                screenshot_path_out.parent.mkdir(parents=True, exist_ok=True)
                screenshot_path_out.write_bytes(screenshot_bytes)
                screenshot_path = str(screenshot_path_out.relative_to(ROOT))
                result = {
                    "schema_version": EVIDENCE_SCHEMA,
                    "generated_at": _now(),
                    "passed": passed,
                    "summary": (
                        "all browser E2E assertions passed"
                        if passed
                        else "browser E2E assertions failed"
                    ),
                    "checks": checks,
                    "screenshot": screenshot_path,
                }
            except Exception as exc:
                checks = [
                    {
                        "name": "browser_e2e_run",
                        "passed": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                ]
                result = {
                    "schema_version": EVIDENCE_SCHEMA,
                    "generated_at": _now(),
                    "passed": False,
                    "summary": "browser E2E run failed",
                    "checks": checks,
                    "screenshot": None,
                }
            finally:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            return 0 if result["passed"] else 1
        finally:
            if chrome_process is not None:
                chrome_process.terminate()
                try:
                    chrome_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    chrome_process.kill()
            web_server.shutdown()
            api.terminate()
            try:
                api.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api.kill()
            if debug_port is not None and not args.keep_artifacts:
                pass  # temp dir cleans up Chrome profile


if __name__ == "__main__":
    raise SystemExit(main())
