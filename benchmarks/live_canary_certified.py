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
import contextlib
import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from tripchord._secret_redact import (
    CREDENTIAL_FIELD_NAME_PATTERN,
    PatternScope,
    _mask_bare_credential_text,
    bounded_json_mask,
    mask_normalized_spans,
    registry_pattern,
    registry_patterns,
)
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

# C-122 round-18 HG-I (supervision 16:03): a provider failure must never crash
# the canary without a diagnostic trail.  The iCom public-API probe is the only
# network-touching scope, so it gets a bounded recovery replay (the search may
# fail transiently); after the replay budget is exhausted the scope is recorded
# as failed with the attempt count instead of raising, and the top-level seal
# below writes a 0600 failure diagnostic only if something escapes anyway.
_ICOM_REPLAY_ATTEMPTS = 3
_ICOM_REPLAY_DELAY_SECONDS = 0.5

# Any value that LOOKS like a bearer token / key (>=32 chars of letters, digits,
# ``_``, ``-``, ``=``) is redacted from a diagnostic summary before it can reach
# stderr or the committed evidence — a canary failure must never echo a secret.
# Every credential-shape regex this producer masks with is DERIVED from the
# single typed registry in ``tripchord._secret_redact`` (C-122 supervision
# 08:30+08:31 补充 C) — a shape / flag change lands in producer, consumer and
# final scan at once instead of three hand-synced copies.  The named variables
# stay so the mask chain below can apply each pattern with its own replacement
# (``<url>`` vs ``[REDACTED]``).
_TOKEN_SHAPE_RE = registry_pattern("token_run")

# C-122 supervision 02:56 (Block 2): the 32+ run alone misses short Bearer/JWT,
# AKIA/GitHub-style and ``token=``-short-opaque credentials that can otherwise
# hit disk (``<output>.failure.json``) or stderr.  These structured shape
# patterns mirror the consumer's ``_sanitize_canary_diag_field``
# (``scripts/run_product_done_gate.py``) — both derive from the same registry so
# the producer artifact is already sanitized BEFORE the consumer re-checks it.
_CANARY_DIAG_URL_RE = registry_pattern("url")
_CANARY_DIAG_AKIA_RE = registry_pattern("akia")
_CANARY_DIAG_PREFIX_TOKEN_RE = registry_pattern("prefix_token")
_CANARY_DIAG_BEARER_RE = registry_pattern("bearer")
_CANARY_DIAG_DOTTED_TOKEN_RE = registry_pattern("dotted_token")
# C-122 supervision 09:00: the credential FIELD NAME shape — an ASCII / full-width
# ``Session_token=abc`` / ``"Session_token":"abc"`` key-value assignment (the
# ``session_token`` family plus bare ``token=`` / ``cookie=`` / ``secret=`` /
# ``password=`` / ``passwd=`` / ``access_key=`` / ``session_key=``) must be
# masked WHOLE (name + value) before the diagnostic ever reaches stderr or the
# ``<output>.failure.json`` summary — the short value would otherwise survive
# every existing shape scan.  C-122 supervision 09:59 (Block 4): the legacy
# ``opaque_kv`` shape was REMOVED and its keys folded into this shape with the
# shared strong/weak boundary semantics.
_CANARY_DIAG_CREDENTIAL_FIELD_RE = registry_pattern("credential_field")
# C-122 supervision 03:46 (Block 1): whole-header/field redaction.  The shape
# patterns above still let a credential BODY slip through when the value is
# short or carries ``;`` / spaces — ``Cookie: session=abc; csrftoken=xyz`` keeps
# ``csrftoken=xyz``, ``Authorization: Basic dXNlcjpwYXNzd29yZA==`` keeps the
# base64 body (``_CANARY_DIAG_OPAQUE_KV_RE`` stops at the space after ``Basic``)
# and ``X-API-Key:`` / ``Proxy-Authorization:`` / ``Set-Cookie:`` are not named
# at all.  This pattern masks the WHOLE header field (name + ``:``/``=`` +
# value, up to the next newline) so the credential and its body are
# removed together — mirrors the consumer's ``_AUTH_COOKIE_PATTERN`` in
# ``scripts/run_product_done_gate.py``.  Over-redaction is the fail-closed
# direction.
# C-122 supervision 04:14: any non-empty value is masked whole — no {4,}
# character floor (``Cookie:a=b`` / ``X-API-Key:abc``) and no quote stops the
# span (``Authorization: "Basic YWJjZA=="`` / ``Set-Cookie: "sid=abc;
# HttpOnly"`` / ``X-API-Key: "abc123"`` must all collapse to ``[REDACTED]``).
# C-122 supervision 04:44: a JSON/dict QUOTED-KEY form (``{"Authorization":
# "Basic a"}`` / ``{'Set-Cookie': 'sid=abc'}``) is recognised too — an optional
# quote may sit between the field name and the ``:``/``=`` and between the
# ``:``/``=`` and the value, so a double-quoted JSON key or a single-quoted
# dict key is masked WHOLE, never split at the quote.
_CANARY_DIAG_WHOLE_HEADER_RE = registry_pattern("whole_header")
# C-122 supervision 00:06 (要求 B) + 08:30+08:31 补充 B: the SAME shape set —
# including the 32+ token run and the tracking URL — is re-checked on a
# NORMALIZED copy (NFKC + casefold, Cf/U+200B dropped) by
# ``bounded_json_mask(..., normalize_patterns=...)``: a full-width /
# zero-width-obfuscated credential (``\uff21uthorization: Basic``,
# ``Author\u200bization: ...``, full-width
# ``\uff54\uff4f\uff4b\uff45\uff4e\uff1d\uff41\uff42\uff43``) and a
# full-width / Cf-obfuscated ``HTTPS://`` URL are collapsed even though the
# ASCII regexes above stop seeing them on the raw text.  Only a COPY is
# normalized; the producer artifact text is returned with those spans masked.
_NORMALIZED_DETECTION_PATTERNS: tuple[re.Pattern[str], ...] = registry_patterns(
    PatternScope.NORMALIZED
)

# C-122 round-19 (2026-08-11 17:03 supervisor veto): the certified canary scope
# contract is DERIVED from the authoritative registry — never hardcoded.
# ``registry.certified_scopes()`` returns exactly the CERTIFIED_ACTIVE set: five
# browser Companion OTA scopes (ctrip:flight, ctrip:lodging, qunar:flight,
# qunar:lodging, tongcheng:flight) plus the iCom public-API scope
# (icom:transfer) = 6 total.  ``tongcheng:lodging`` is DISABLED in the registry
# (user skipped on 2026-08-05) and must never enter the canary — a disabled
# scope is never a required canary member and is never silently re-enabled by a
# hardcoded contract.  ``icom:transfer`` is a public-API read and never appears
# in a Companion's ``authorized_scope_keys``.
_CERTIFIED_CANARY_SCOPE_KEYS: tuple[str, ...] = tuple(
    scope.key for scope in build_default_registry().certified_scopes()
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _runtime_identity() -> dict[str, str]:
    """The interpreter that ran the canary — binds a diagnostic to the runtime."""
    return {
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "platform": sys.platform,
    }


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


def _desensitize(text: str) -> str:
    """Redact every credential SHAPE from a diagnostic message before it can
    reach stderr or the committed evidence (C-122 supervision 02:56 Block 2).

    Structured, fail-closed sanitization — not just long token runs: URLs are
    collapsed to ``<url>``, and AKIA-style AWS keys, well-known token prefixes
    (``ghp_`` / ``github_pat_`` / ``glpat-`` / ``xoxb-`` / ``sk-``), short
    ``Bearer <token>`` forms, dotted JWTs and short opaque ``token=`` /
    ``bearer=`` / ``password=`` assignments are all collapsed to ``[REDACTED]``
    (C-122 supervision 04:44: the redaction marker contract is unified with the
    consumer's ``_sanitize_canary_diag_field`` and the gate's ``_redact_output``
    — one fixed ``[REDACTED]`` marker).
    Whole header fields — ``Authorization`` / ``Proxy-Authorization`` /
    ``Cookie`` / ``Set-Cookie`` / ``X-API-Key`` — are masked name-and-value
    together, so a ``Basic`` base64 body or a ``;``-joined cookie pair can never
    survive with a partially-redacted header (C-122 supervision 03:46 Block 1).
    Mirrors the consumer's ``_sanitize_canary_diag_field`` so the producer
    artifact is already sanitized on disk and on stderr before the gate re-checks
    it.

    C-122 supervision 06:58: the whole message is BOUNDED-RECURSIVE JSON masked
    (``tripchord._secret_redact.bounded_json_mask``) — a credential smuggled
    through multiple ``json.dumps`` layers is masked at EVERY decoded level, a
    structural-start string that does not parse and a depth/node/size budget
    overflow both fail closed to ``[REDACTED]``.
    """
    return bounded_json_mask(
        text,
        mask_level=_desensitize_level,
        # C-122 supervision 00:06 (要求 B): re-check every masked level on the
        # NORMALIZED copy (NFKC + casefold, Cf/U+200B dropped) so a full-width /
        # zero-width-obfuscated credential span is collapsed even though the
        # ASCII shape regexes stopped seeing it on the raw text.
        normalize_patterns=_NORMALIZED_DETECTION_PATTERNS,
        # C-122 supervision 09:00: a STRUCTURED JSON credential field NAME
        # (``{"Session_token":"abc"}``) is masked whole too — the key would
        # otherwise survive the free-form value-only walk.
        key_patterns=(CREDENTIAL_FIELD_NAME_PATTERN,),
    )


def _desensitize_level(text: str) -> str:
    """Mask ONE free-form text level with the credential-shape regex chain."""
    # C-122 supervision 09:00 (gap 2): mask the credential-FIELD assignment on
    # the NORMALIZED copy of the RAW text FIRST.  A zero-width-split field name
    # (``Session​token:"abc"``) would otherwise be PARTIALLY masked by the ASCII
    # credential-field shape — the Cf char is a word boundary, so ``token:…`` is
    # collapsed and the ``Session`` name half survives.  The WHOLE-HEADER shape
    # runs here too so a full-width ``\uff21uthorization: Basic YWJjZA==`` masks
    # name-and-base64 together (the tightened credential-FIELD value pattern
    # stops at the space after ``Basic``).  Normalizing first folds the Cf /
    # full-width split back into ASCII and masks the whole assignment before
    # the ASCII chain can tear it apart.
    # C-122 supervision 09:28 (gap 2 regression): collapse URLs on the RAW text
    # BEFORE the normalized credential-field pre-pass.  The credential-field
    # value now runs from the first value char to a clear field boundary (spaces
    # included), so without this a trailing ``https://…`` scheme would be
    # swallowed into the value (``token=abc https``) and the URL host would
    # survive once the scheme half was redacted.  Masking the URL first turns
    # ``token=abc <url>`` into a clean ``[REDACTED] <url>``.
    text = _CANARY_DIAG_URL_RE.sub("<url>", text)
    text = mask_normalized_spans(
        text,
        (_CANARY_DIAG_WHOLE_HEADER_RE, _CANARY_DIAG_CREDENTIAL_FIELD_RE),
        marker="[REDACTED]",
    )
    text = _CANARY_DIAG_WHOLE_HEADER_RE.sub("[REDACTED]", text)
    text = _TOKEN_SHAPE_RE.sub("[REDACTED]", text)
    text = _CANARY_DIAG_AKIA_RE.sub("[REDACTED]", text)
    text = _CANARY_DIAG_PREFIX_TOKEN_RE.sub("[REDACTED]", text)
    text = _CANARY_DIAG_BEARER_RE.sub("[REDACTED]", text)
    text = _CANARY_DIAG_DOTTED_TOKEN_RE.sub("[REDACTED]", text)
    # C-122 supervision 09:59 (Block 4): the legacy ``opaque_kv`` mask is gone —
    # every key it carried is now folded into the credential-FIELD shape with
    # the shared strong/weak boundary semantics (the credential_field line below
    # does the work).
    text = _CANARY_DIAG_CREDENTIAL_FIELD_RE.sub("[REDACTED]", text)
    # C-122 round-26 Block 41 + round-27 Block 43: free-text bare values are
    # masked BEFORE the 0600 seal lands on disk.  The SAME closed registered
    # business-identifier bases the final scan accepts survive here IN THEIR
    # DOCUMENTED SCHEMA FORM (``flightOption1`` / ``refreshTokenCount1`` /
    # ``hotelAmenity3`` / ``plannerV2`` …); every other bare value (``qwerTy1``
    # / ``myFlightHotel1`` / ``mySuperSecretV1`` / ``purpleMonkeyDishwasher1``
    # …) AND a registered base in the wrong schema form (``planner1`` /
    # ``provider9``) or inside a credential-NARRATION context (``password is
    # flightOption1``) fails closed to ``[REDACTED]`` in the producer, so a
    # bare credential can never reach the sealed diagnostic unmasked.  This is
    # the ``落盘前脱敏`` half of supervision Block 41; the final scans still
    # reject a raw unknown bare value if one ever appears (defense in depth).
    text = _mask_bare_credential_text(text)
    return text


async def _icom_scope_canary() -> dict[str, Any]:
    """Real read-only public API canary for icom:transfer (no Companion needed).

    The search is network-touching and may fail transiently, so it is replayed a
    bounded number of times (recovery replay).  If the replay budget is exhausted
    the scope is recorded as FAILED with the attempt count and a desensitized
    reason — the exception never propagates (C-122 round-18 HG-I).
    """
    provider = IComTransferProvider()
    query = IComTransferQuery(
        travel_date=date.today() + timedelta(days=3),
        origin=IComLocation.AIRPORT,
        destination=IComLocation.MAAFUSHI,
        adults=2,
    )
    attempts = 0
    last_error: str | None = None
    last_exc_type: str | None = None
    try:
        result = None
        for attempts in range(1, _ICOM_REPLAY_ATTEMPTS + 1):
            try:
                result = await provider.search(query)
                break
            except Exception as exc:  # network / HTTP / provider parsing
                last_error = _desensitize(str(exc))
                last_exc_type = type(exc).__name__
                if attempts < _ICOM_REPLAY_ATTEMPTS:
                    await asyncio.sleep(_ICOM_REPLAY_DELAY_SECONDS)
        if result is None:
            return {
                "passed": False,
                "kind": "icom_public_api",
                "fresh": True,
                "authorized": True,
                "read_only": True,
                "replay_attempts": attempts,
                "exception_class": last_exc_type,
                "detail": (
                    f"icom public API search failed after {attempts} replay "
                    f"attempts: {last_error or 'unknown provider error'}"
                ),
            }
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
    caps = registry.capability_map()

    scopes: list[dict[str, Any]] = []
    status: dict[str, Any] | None = None
    bridge_ok = bool(bridge_token) and len(bridge_token) >= 32
    if bridge_ok:
        async with httpx.AsyncClient() as client:
            try:
                status = await _query_companion_status(client, api_base, bridge_token)
            except Exception as exc:  # network / HTTP / auth
                status = {"error": str(exc)}

    # C-122 round-19: iterate the certified canary scope CONTRACT derived from
    # the authoritative registry (five certified browser scopes + the iCom
    # public-API scope).
    for scope_key in _CERTIFIED_CANARY_SCOPE_KEYS:
        cap = caps[scope_key]
        # HG-I regression (round-18 gate, 08:40 UTC): ``ProviderCapability`` has
        # no ``.provider`` attribute — the provider lives on its scope key
        # (``cap.key.provider``).  The old ``cap.provider`` crashed ``evaluate``
        # mid-flight, and before the top-level seal a crashed canary was silent.
        provider = cap.key.provider
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
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, output)
    except BaseException:
        # Never leave a partial 0600 temp file behind on a failed dump.
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return output


def _seal_failure_diagnostic(
    stage: str,
    exc: BaseException,
    output: Path,
    *,
    run_id: str = "",
    tested_sha: str = "",
) -> Path:
    """Atomically write a 0600 failure diagnostic (stage, exception class,
    desensitized summary, run identity + run_id / tested_sha / runtime bindings)
    and NEVER echo a secret.

    C-122 round-18 HG-I (supervision 16:03): a canary failure must be AUDITABLE.
    When anything escapes ``evaluate`` / ``_dump``, ``main`` captures it at the
    top level, records the stage and exception class with a token-shaped-substring
    redacted summary, and seals it next to the output as ``<output>.failure.json``
    with the same 0600 atomic dump used for the report itself.  The raw exception
    text never reaches stderr or the committed trail unfiltered.

    C-122 round-19 (supervision 17:03 Block 2): the diagnostic binds the run_id
    and tested_sha the gate passed (``--run-id`` / ``--tested-sha``) plus the
    runtime identity, so the outer gate can verify the diagnostic belongs to THIS
    run at THIS revision and is fresh — a stale or mismatched diagnostic is never
    silently consumed.
    """
    diagnostic: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "diagnostic_kind": "canary_failure",
        "stage": stage,
        "exception_class": type(exc).__name__,
        "summary": _desensitize(str(exc)) if str(exc) else "no exception detail",
        "run_identity": {
            "script": Path(__file__).name,
            "output": str(output),
            "run_id": run_id,
            "tested_sha": tested_sha,
            "runtime": _runtime_identity(),
        },
        "generated_at": _now(),
    }
    diag_path = output.with_suffix(output.suffix + ".failure.json")
    return _dump(diagnostic, diag_path)


def _clear_stale_failure_diagnostic(output: Path) -> None:
    """Best-effort removal of a PRIOR run's failure diagnostic after THIS run
    succeeded.

    C-122 round-19 (supervision 17:03 Block 2 counter-example): a recovery-replay
    success must never leave an old ``<output>.failure.json`` on disk that a
    later consumer could mistake for evidence of a current failure.  A fresh
    successful report clears the stale diagnostic.
    """
    stale = output.with_suffix(output.suffix + ".failure.json")
    with contextlib.suppress(OSError):
        if stale.exists():
            stale.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--bridge-token",
        default=os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", ""),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    # C-122 round-19 (supervision 17:03 Block 2): the gate binds THIS run and THIS
    # tested revision into the canary diagnostic so a failure can be verified as
    # current-and-owned before the outer layer consumes it.
    parser.add_argument("--run-id", default="")
    parser.add_argument("--tested-sha", default="")
    args = parser.parse_args()

    try:
        report = asyncio.run(
            evaluate(api_base=args.api_base, bridge_token=args.bridge_token)
        )
    except BaseException as exc:
        # Top-level seal: the canary crashed mid-evaluate.  Never echo the raw
        # exception (it may contain a token); write the audit diagnostic and
        # fail the process so layer 5 cannot be papered over.  The seal itself is
        # best-effort — a disk that can no longer write must still fail loudly.
        with contextlib.suppress(BaseException):
            _seal_failure_diagnostic(
                "evaluate",
                exc,
                args.output,
                run_id=args.run_id,
                tested_sha=args.tested_sha,
            )
        print(
            f"canary failed during evaluate ({type(exc).__name__}): "
            f"{_desensitize(str(exc)) if str(exc) else 'no detail'}",
            file=sys.stderr,
        )
        return 1
    try:
        output = _dump(report, args.output)
    except BaseException as exc:
        with contextlib.suppress(BaseException):
            _seal_failure_diagnostic(
                "dump",
                exc,
                args.output,
                run_id=args.run_id,
                tested_sha=args.tested_sha,
            )
        print(
            f"canary failed writing evidence ({type(exc).__name__}): "
            f"{_desensitize(str(exc)) if str(exc) else 'no detail'}",
            file=sys.stderr,
        )
        return 1
    # A fresh success clears a PRIOR failed run's diagnostic so recovery-replay
    # success can never leave stale failure evidence behind (Block 2).
    _clear_stale_failure_diagnostic(args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"evidence: {output}", file=sys.stderr)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
