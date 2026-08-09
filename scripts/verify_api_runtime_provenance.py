#!/usr/bin/env python3
"""Hard-verify that a running TripChord live API is executing the current HEAD.

Reads the running API's runtime provenance through
``/api/v1/agents/runtime``, computes the provenance the current checked-out
tree claims, and exits:

* ``0`` — every compared field matches (the running code is the current
  committed tree);
* ``2`` — any field mismatches, the API is unreachable, or it carries no
  provenance.

The check is deliberately stricter than a working-tree ``git status``: a
worker started before a HEAD move, or whose on-disk source changed without a
restart, still reports its startup provenance and fails this check even when
the worktree looks clean.

Exit 2 also covers the counter-example family the Done-Gate demands — old API
process still running, HEAD changed without restart, and wrong source
fingerprint — all surface as ``runtime_provenance.* != expected.*``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx
from tripchord.runtime_provenance import local_expected_provenance, provenance_mismatches

_RUNTIME_ENDPOINT = "/api/v1/agents/runtime"


def _fetch_runtime(base: str, timeout: float) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        response = client.get(f"{base.rstrip('/')}{_RUNTIME_ENDPOINT}")
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("runtime endpoint returned a non-object payload")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--quiet", action="store_true", help="only print the verdict")
    args = parser.parse_args(argv)

    try:
        runtime = _fetch_runtime(args.api_base, args.timeout)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"passed": False, "error": f"cannot reach API: {exc}"},
                sort_keys=True,
            )
        )
        return 2

    expected = local_expected_provenance()
    reported = runtime.get("runtime_provenance")
    mismatches = provenance_mismatches(reported, expected)
    result: dict[str, Any] = {
        "passed": not mismatches,
        "expected": expected,
        "reported": reported,
        "mismatches": mismatches,
    }
    if not args.quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            json.dumps(
                {"passed": not mismatches, "mismatches": mismatches},
                sort_keys=True,
            )
        )
    return 0 if not mismatches else 2


if __name__ == "__main__":
    sys.exit(main())
