#!/usr/bin/env python3
"""Certified OTA read-only canary (Done-Gate layer 5 driver).

Every ``CERTIFIED_ACTIVE`` scope in the default registry must have a real,
fresh, authorised, read-only canary before layer 5 may pass:

- ``icom:transfer`` -> a real read-only public API query (no Companion needed);
- browser scopes (``ctrip``/``qunar``/``tongcheng``) -> a fresh Companion
  heartbeat on the local Bridge whose ``authorized_scope_keys`` contains the
  scope key.  That heartbeat is the canary evidence: it proves the user's
  logged-in browser session for the scope is alive, fresh and authorised.

The open-meteo / dpm.org.cn probes live in ``live_canary.py``; the gate reports
them separately as a public-page connectivity canary and they never drive
layer 5.

Writes per-scope evidence atomically to ``--output`` and exits 0 only when every
certified scope passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from tripchord.platform.registry import build_default_registry
from tripchord.providers.browser_bridge import BRIDGE_TOKEN_HEADER
from tripchord.providers.icom_transfer import (
    IComLocation,
    IComTransferProvider,
    IComTransferQuery,
)

COMPANION_STATUS_PATH = "/browser-bridge/v1/companions/status"
COMPANION_STATUS_TIMEOUT_SECONDS = 5.0
RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_OUTPUT = RESULTS_DIR / "live-canary-certified.json"
SCHEMA_VERSION = "tripchord-certified-ota-canary-v1"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _query_companion_status(
    client: httpx.AsyncClient,
    base: str,
    bridge_token: str,
) -> dict[str, Any]:
    """Query the local Bridge companion status, returning the raw payload."""
    headers = {BRIDGE_TOKEN_HEADER: bridge_token}
    response = await client.get(
        f"{base}{COMPANION_STATUS_PATH}",
        headers=headers,
        timeout=COMPANION_STATUS_TIMEOUT_SECONDS,
    )
    if response.status_code == 401 or response.status_code == 403:
        raise RuntimeError(
            f"bridge rejected the pairing token (HTTP {response.status_code})"
        )
    response.raise_for_status()
    payload: Any = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("companion status returned a non-object payload")
    return payload


def _browser_scope_canary(
    scope_key: str,
    provider: str,
    status: dict[str, Any],
) -> dict[str, Any]:
    """Canary evidence for one browser scope from a companion status payload.

    Requires a fresh heartbeat (``is_fresh`` and ``age_seconds`` within the
    stale window) that declares the provider and the exact scope key in
    ``authorized_scope_keys``.
    """
    stale_after = status.get("stale_after_seconds")
    companions = status.get("companions")
    if not isinstance(stale_after, int) or stale_after <= 0 or not isinstance(
        companions, list
    ):
        return {
            "passed": False,
            "kind": "companion_heartbeat",
            "fresh": False,
            "authorized": False,
            "read_only": True,
            "detail": "companion status payload has an invalid structure",
        }
    matching: list[dict[str, Any]] = []
    for companion in companions:
        if not isinstance(companion, dict):
            continue
        providers = companion.get("providers")
        authorized = companion.get("authorized_scope_keys")
        age_seconds = companion.get("age_seconds")
        if not isinstance(providers, list) or not isinstance(authorized, list):
            continue
        provider_ok = provider in {item for item in providers if isinstance(item, str)}
        scope_ok = scope_key in {item for item in authorized if isinstance(item, str)}
        age_ok = (
            isinstance(age_seconds, int | float)
            and not isinstance(age_seconds, bool)
            and 0 <= age_seconds <= stale_after
        )
        if companion.get("is_fresh") is True and provider_ok and scope_ok and age_ok:
            matching.append(
                {
                    "companion_id": companion.get("companion_id"),
                    "age_seconds": age_seconds,
                    "is_fresh": companion.get("is_fresh"),
                    "authorized_scope_keys": sorted(
                        {item for item in authorized if isinstance(item, str)}
                    ),
                    "adapter_version": companion.get("adapter_version"),
                    "contract_version": companion.get("contract_version"),
                    "runtime_instance_id": companion.get("runtime_instance_id"),
                    "providers": sorted(
                        {item for item in providers if isinstance(item, str)}
                    ),
                }
            )
    if not matching:
        candidate = None
        for companion in companions:
            if not isinstance(companion, dict):
                continue
            providers = companion.get("providers")
            if isinstance(providers, list) and provider in {
                item for item in providers if isinstance(item, str)
            }:
                candidate = companion
                break
        if candidate is None:
            detail = (
                f"pending user authorization: no connected Companion declares "
                f"provider {provider!r}; pair the Companion and keep the official "
                f"OTA domains logged in, then re-run"
            )
        elif candidate.get("is_fresh") is not True:
            detail = (
                f"pending user authorization: Companion heartbeat for {provider!r} "
                "is stale (not fresh); reconnect the Companion and re-run"
            )
        else:
            authorized = sorted(
                {
                    item
                    for item in candidate.get("authorized_scope_keys", [])
                    if isinstance(item, str)
                }
            )
            detail = (
                f"pending user authorization: Companion for {provider!r} is fresh "
                f"but does not authorise scope {scope_key!r} "
                f"(authorized: {authorized or 'none'}); log in on the official "
                f"domain and re-run"
            )
        return {
            "passed": False,
            "kind": "companion_heartbeat",
            "fresh": False,
            "authorized": False,
            "read_only": True,
            "detail": detail,
        }
    best = min(matching, key=lambda item: item["age_seconds"])
    return {
        "passed": True,
        "kind": "companion_heartbeat",
        "fresh": True,
        "authorized": True,
        "read_only": True,
        "evidence": best,
        "detail": (
            f"fresh authorised Companion heartbeat {best['companion_id']} "
            f"(age {best['age_seconds']:.1f}s) declares {scope_key}"
        ),
    }


async def _icom_scope_canary() -> dict[str, Any]:
    """Real read-only public API canary for icom:transfer (no Companion needed)."""
    provider = IComTransferProvider()
    try:
        query = IComTransferQuery(
            travel_date=date.today() + timedelta(days=3),
            origin=IComLocation.AIRPORT,
            destination=IComLocation.MAAFUSHI,
            adults=2,
        )
        result = await provider.search(query)
        options = result.options
        if not options:
            return {
                "passed": False,
                "kind": "icom_public_api",
                "fresh": True,
                "authorized": True,
                "read_only": True,
                "detail": "icom public API returned no transfer options",
            }
        sample = options[0]
        evidence = {
            "searched_at": result.searched_at.isoformat(),
            "source_urls": list(result.source_urls),
            "options": len(options),
            "sample": {
                "service_name": sample.service_name,
                "departure_at": sample.departure_at.isoformat(),
                "fare_amount": str(sample.fare.amount),
                "currency": sample.fare.currency,
            },
        }
        return {
            "passed": True,
            "kind": "icom_public_api",
            "fresh": True,
            "authorized": True,
            "read_only": True,
            "evidence": evidence,
            "detail": (
                f"icom public API returned {len(options)} read-only transfer options"
            ),
        }
    finally:
        await provider.aclose()


async def evaluate(
    *,
    api_base: str,
    bridge_token: str,
) -> dict[str, Any]:
    registry = build_default_registry()
    certified = registry.certified_scopes()

    scopes: list[dict[str, Any]] = []
    status: dict[str, Any] | None = None
    bridge_ok = bool(bridge_token) and len(bridge_token) >= 32
    if bridge_ok:
        async with httpx.AsyncClient() as client:
            try:
                status = await _query_companion_status(client, api_base, bridge_token)
            except Exception as exc:  # network / HTTP / auth
                status = {"error": str(exc)}

    for cap in certified:
        scope_key = cap.key
        provider = cap.provider
        if provider == "icom":
            entry = await _icom_scope_canary()
            entry["scope"] = scope_key
            scopes.append(entry)
            continue
        if not bridge_ok:
            scopes.append(
                {
                    "scope": scope_key,
                    "passed": False,
                    "kind": "companion_heartbeat",
                    "fresh": False,
                    "authorized": False,
                    "read_only": True,
                    "detail": (
                        "pending user authorization: TRIPCHORD_BROWSER_BRIDGE_TOKEN "
                        "is absent or shorter than 32 characters; pair the Companion "
                        "and re-run"
                    ),
                }
            )
            continue
        if not isinstance(status, dict) or "companions" not in status:
            status_hint = (
                status.get("error", "no companions payload") if status else "no bridge token"
            )
            scopes.append(
                {
                    "scope": scope_key,
                    "passed": False,
                    "kind": "companion_heartbeat",
                    "fresh": False,
                    "authorized": False,
                    "read_only": True,
                    "detail": (
                        "pending user authorization: companion status is unavailable "
                        f"({status_hint})"
                    ),
                }
            )
            continue
        entry = _browser_scope_canary(scope_key, provider, status)
        entry["scope"] = scope_key
        scopes.append(entry)

    passed = all(entry["passed"] for entry in scopes)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _now(),
        "bridge_token_present": bridge_ok,
        "companion_status": status,
        "scopes": scopes,
        "passed": passed,
    }


def _dump(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(".json.tmp")
    # Seal 0600 from birth (no write-then-chmod window) and fsync before the
    # atomic rename, so the raw canary evidence is never world/group-readable
    # and never observed partially written (C-122 Fix 5).
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        # fchmod pins the exact mode on the fd so a restrictive umask (e.g.
        # 0777) cannot produce a 0000 file the owner cannot read (C-122 round-18).
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--bridge-token",
        default=os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", ""),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    report = asyncio.run(
        evaluate(api_base=args.api_base, bridge_token=args.bridge_token)
    )
    output = _dump(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"evidence: {output}", file=sys.stderr)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
