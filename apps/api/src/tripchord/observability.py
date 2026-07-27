from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from uuid import uuid4

from fastapi import Request, Response
from starlette.middleware.base import RequestResponseEndpoint


@dataclass(frozen=True)
class RequestMetricKey:
    method: str
    route: str
    status: int


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._request_counts: dict[RequestMetricKey, int] = defaultdict(int)
        self._request_duration_sum: dict[RequestMetricKey, float] = defaultdict(float)
        self._job_counts: dict[str, int] = defaultdict(int)

    def observe_request(self, method: str, route: str, status: int, duration: float) -> None:
        key = RequestMetricKey(method=method, route=route, status=status)
        with self._lock:
            self._request_counts[key] += 1
            self._request_duration_sum[key] += duration

    def observe_job(self, status: str) -> None:
        with self._lock:
            self._job_counts[status] += 1

    def render(self) -> str:
        lines = [
            "# HELP tripchord_http_requests_total Total HTTP requests.",
            "# TYPE tripchord_http_requests_total counter",
        ]
        with self._lock:
            for key in sorted(
                self._request_counts,
                key=lambda item: (item.route, item.method, item.status),
            ):
                labels = self._labels(
                    method=key.method,
                    route=key.route,
                    status=str(key.status),
                )
                count = self._request_counts[key]
                duration = self._request_duration_sum[key]
                lines.append(f"tripchord_http_requests_total{{{labels}}} {count}")
                lines.append(
                    f"tripchord_http_request_duration_seconds_sum{{{labels}}} {duration:.9f}"
                )
                lines.append(
                    f"tripchord_http_request_duration_seconds_count{{{labels}}} {count}"
                )
            lines.extend(
                [
                    "# HELP tripchord_planning_jobs_total Planning job terminal/retry events.",
                    "# TYPE tripchord_planning_jobs_total counter",
                ]
            )
            for status, count in sorted(self._job_counts.items()):
                lines.append(
                    f'tripchord_planning_jobs_total{{status="{self._escape(status)}"}} {count}'
                )
        return "\n".join(lines) + "\n"

    def _labels(self, **labels: str) -> str:
        return ",".join(
            f'{name}="{self._escape(value)}"' for name, value in labels.items()
        )

    def _escape(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


metrics = MetricsRegistry()


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s")


async def observe_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    supplied_id = request.headers.get("X-Request-ID", "")
    request_id = supplied_id if 0 < len(supplied_id) <= 100 else str(uuid4())
    started = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    finally:
        duration = perf_counter() - started
        route_object = request.scope.get("route")
        route = getattr(route_object, "path", request.url.path)
        metrics.observe_request(request.method, route, status_code, duration)
        logging.getLogger("tripchord.access").info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "route": route,
                    "status": status_code,
                    "duration_ms": round(duration * 1000, 3),
                },
                separators=(",", ":"),
            )
        )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response
