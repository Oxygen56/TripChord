from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_SCHEMA_VERSION = "tripchord-formal-live-source-v3"
_BINDING_SCHEMA_VERSION = "tripchord-formal-live-source-binding-v3"
_CHALLENGE_SCHEMA_VERSION = "tripchord-formal-live-source-challenge-v2"
_RECEIPT_SCHEMA_VERSION = "tripchord-formal-live-source-authority-receipt-v2"
_ANCHOR_VERSION = "tripchord-formal-source-anchor-v1"
_PUBLIC_KEY_DER = base64.b64decode(
    "MCowBQYDK2VwAyEAnzhuCoYECUY1LsjPfT+yI4NZjs8r1BBUcu5DPFNdNg8=",
    validate=True,
)
_AUTHORITY_KEY_ID = hashlib.sha256(_PUBLIC_KEY_DER).hexdigest()
_STARTUP_CAPABILITY = object()
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_PRIVATE_KEY = _REPO_ROOT / ".runtime" / "formal-source-authority-private.pem"
_DEFAULT_LEDGER = _REPO_ROOT / ".runtime" / "formal-source-challenges-v2.json"

_BROWSER_MOUNT = "/browser-bridge"
_BROWSER_PATHS = {
    "browser_heartbeat": f"{_BROWSER_MOUNT}/v1/companions/heartbeat",
    "browser_claim": f"{_BROWSER_MOUNT}/v1/tasks/claim",
    "browser_complete": f"{_BROWSER_MOUNT}/v1/tasks/{{task_id}}/complete",
}
_ICOM_PATH_ORDER = (
    "/api/v1/public/trips/schedules",
    "/api/v1/public/ferry-fares/schedule-base-price",
    "/api/v1/public/policy-sections",
)
_ICOM_PATHS = frozenset(_ICOM_PATH_ORDER)
_COMPOSITION_TYPES = {
    "bridge": "tripchord.providers.browser_bridge.BrowserTaskBridge",
    "icom_provider": "tripchord.providers.icom_transfer.IComTransferProvider",
    "live_system": "tripchord.agents.live_system.LivePackageAgentSystem",
    "flexible_system": "tripchord.agents.flexible_live_system.FlexibleLiveAgentSystem",
}
_CHALLENGE_CONTEXT_FIELDS = {
    "run_id",
    "tested_commit_sha",
    "runtime_identity",
    "request_sha256",
    "candidate_set_sha256",
    "scenario_sha256",
}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha256(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{label} must be lowercase 64-hex")
    return value


def _require_commit(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ValueError(f"{label} must be lowercase 40-hex")
    return value


def _require_aware_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} has no timezone")
    return parsed.astimezone(UTC)


def _sign(private_key: Ed25519PrivateKey, value: object) -> str:
    return base64.b64encode(private_key.sign(_canonical_bytes(value))).decode("ascii")


def _verify_signature(value: object, signature: object, label: str) -> None:
    if not isinstance(signature, str):
        raise ValueError(f"{label} has no authority signature")
    try:
        raw = base64.b64decode(signature, validate=True)
        public_key = serialization.load_der_public_key(_PUBLIC_KEY_DER)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("fixed formal source anchor is not Ed25519")
        public_key.verify(raw, _canonical_bytes(value))
    except (ValueError, InvalidSignature) as exc:
        raise ValueError(f"{label} lacks the fixed production authority proof") from exc


def _signed_payload(value: Mapping[str, object]) -> dict[str, object]:
    return {key: item for key, item in value.items() if key != "signature"}


def _challenge_proof_payload(challenge: Mapping[str, object]) -> dict[str, object]:
    runtime = _runtime_identity(challenge["runtime_identity"])
    return {
        "purpose": "tripchord-formal-live-source-challenge-proof-v1",
        "schema_version": challenge["schema_version"],
        "anchor_version": challenge["anchor_version"],
        "authority_key_id": challenge["authority_key_id"],
        "challenge_id": challenge["challenge_id"],
        "nonce_digest": challenge["nonce_digest"],
        "run_id": challenge["run_id"],
        "tested_commit_sha": challenge["tested_commit_sha"],
        "runtime_identity_sha256": _sha256(runtime),
        "request_sha256": challenge["request_sha256"],
        "candidate_set_sha256": challenge["candidate_set_sha256"],
        "scenario_sha256": challenge["scenario_sha256"],
        "issued_at": challenge["issued_at"],
        "expires_at": challenge["expires_at"],
    }


def _receipt_proof_payload(receipt: Mapping[str, object]) -> dict[str, object]:
    runtime = _runtime_identity(receipt["runtime_identity"])
    return {
        "purpose": "tripchord-formal-live-source-authority-receipt-proof-v1",
        "schema_version": receipt["schema_version"],
        "anchor_version": receipt["anchor_version"],
        "authority_key_id": receipt["authority_key_id"],
        "challenge_id": receipt["challenge_id"],
        "nonce_digest": receipt["nonce_digest"],
        "binding_digest": receipt["binding_digest"],
        "run_id": receipt["run_id"],
        "tested_commit_sha": receipt["tested_commit_sha"],
        "runtime_identity_sha256": _sha256(runtime),
        "pre_event_count": receipt["pre_event_count"],
        "post_event_count": receipt["post_event_count"],
        "delta_digest": receipt["delta_digest"],
        "issued_at": receipt["issued_at"],
        "verified_at": receipt["verified_at"],
    }


def _qualified_type(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def formal_composition_contract(commit_sha: str) -> dict[str, object]:
    _require_commit(commit_sha, "formal composition commit_sha")
    return {
        "entrypoint": "tripchord.main._install_browser_bridge",
        "commit_sha": commit_sha,
        "mount_path": _BROWSER_MOUNT,
        "types": dict(_COMPOSITION_TYPES),
        "wiring": {
            "mounted_app_owns_bridge": True,
            "live_system_owns_bridge": True,
            "live_system_owns_icom_provider": True,
            "flexible_system_owns_live_system": True,
        },
    }


def _composition_sha256(install_id: str, composition: object) -> str:
    return _sha256({"install_id": install_id, "composition": composition})


def _protected_regular_file(
    path: Path,
    label: str,
    *,
    missing_ok: bool = False,
) -> bytes | None:
    """Read one owner-only file through the descriptor that was validated.

    ``lstat()`` followed by ``Path.read_bytes()`` is a TOCTOU window.  Opening
    with ``O_NOFOLLOW`` and validating the resulting descriptor also rejects
    symlinks and hardlinks without trusting a pathname-level preflight.
    """

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeError(f"{label} is unavailable") from None
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"{label} must be a regular non-symlink file")
        if (
            stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise RuntimeError(
                f"{label} must be owner-only mode 0600 with exactly one link"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or after.st_size != info.st_size
            or after.st_mtime_ns != info.st_mtime_ns
        ):
            raise RuntimeError(f"{label} changed while it was being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_owner_only_text(path: Path, label: str, *, minimum_length: int) -> str:
    """Load a local control value safely.

    Mode 0600 protects against other OS users; it is deliberately not claimed
    as isolation from arbitrary code already running under this same uid.
    """
    try:
        raw = _protected_regular_file(path, label)
        if raw is None:  # pragma: no cover - missing_ok is false above
            raise RuntimeError(f"{label} is unavailable")
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} is not UTF-8") from exc
    if len(value) < minimum_length:
        raise RuntimeError(f"{label} is too short")
    return value


def _exact_int(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} is invalid")
    return value


def _nonempty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is invalid")
    return value


def _exact_string_list(value: object, label: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{label} is invalid")
    return list(value)


def _runtime_identity(value: object) -> dict[str, object]:
    fields = {
        "pid",
        "started_at",
        "repo_toplevel",
        "commit_sha",
        "dependency_lock_sha256",
        "live_system_source_sha256",
        "python_version",
        "python_executable",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("formal source runtime identity has an invalid shape")
    if (
        not isinstance(value["pid"], int)
        or isinstance(value["pid"], bool)
        or value["pid"] <= 0
    ):
        raise ValueError("formal source runtime pid is invalid")
    _require_aware_time(value["started_at"], "formal source runtime started_at")
    _require_commit(value["commit_sha"], "formal source runtime commit_sha")
    for key in ("repo_toplevel", "python_version", "python_executable"):
        if not isinstance(value[key], str) or not value[key]:
            raise ValueError(f"formal source runtime {key} is invalid")
    for key in ("dependency_lock_sha256", "live_system_source_sha256"):
        _require_sha256(value[key], f"formal source runtime {key}")
    return dict(value)


def _validate_challenge(challenge: object) -> dict[str, object]:
    fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "challenge_id",
        "nonce_digest",
        "run_id",
        "tested_commit_sha",
        "runtime_identity",
        "request_sha256",
        "candidate_set_sha256",
        "scenario_sha256",
        "issued_at",
        "expires_at",
        "signature",
    }
    if not isinstance(challenge, dict) or set(challenge) != fields:
        raise ValueError("formal source challenge has an invalid shape")
    if challenge["schema_version"] != _CHALLENGE_SCHEMA_VERSION:
        raise ValueError("formal source challenge schema is invalid")
    if (
        challenge["anchor_version"] != _ANCHOR_VERSION
        or challenge["authority_key_id"] != _AUTHORITY_KEY_ID
    ):
        raise ValueError("formal source challenge uses a foreign verification anchor")
    try:
        UUID(str(challenge["challenge_id"]))
    except (ValueError, TypeError) as exc:
        raise ValueError("formal source challenge identity is invalid") from exc
    for key in (
        "nonce_digest",
        "request_sha256",
        "candidate_set_sha256",
        "scenario_sha256",
    ):
        _require_sha256(challenge[key], f"formal source challenge {key}")
    _require_commit(challenge["tested_commit_sha"], "formal source challenge commit")
    if not isinstance(challenge["run_id"], str) or not challenge["run_id"]:
        raise ValueError("formal source challenge run_id is invalid")
    runtime = _runtime_identity(challenge["runtime_identity"])
    if runtime["commit_sha"] != challenge["tested_commit_sha"]:
        raise ValueError("formal source challenge commit differs from runtime")
    issued = _require_aware_time(challenge["issued_at"], "challenge issued_at")
    expires = _require_aware_time(challenge["expires_at"], "challenge expires_at")
    if expires <= issued or expires - issued > timedelta(hours=2):
        raise ValueError("formal source challenge validity window is invalid")
    _verify_signature(
        _challenge_proof_payload(challenge),
        challenge["signature"],
        "formal source challenge",
    )
    return dict(challenge)


def _event_payload(event: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"receipt_sha256", "signature"}
    }


def _validate_event(
    event: object,
    *,
    previous: str,
    sequence: int,
    challenge: Mapping[str, object],
    install_id: str,
    composition_sha256: str,
) -> dict[str, object]:
    fields = {
        "sequence",
        "kind",
        "method",
        "path",
        "subject_ids",
        "details",
        "response_sha256",
        "observed_at",
        "challenge_id",
        "nonce_digest",
        "context",
        "previous_receipt_sha256",
        "receipt_sha256",
        "signature",
    }
    if not isinstance(event, dict) or set(event) != fields:
        raise ValueError("formal source event has an invalid shape")
    if (
        not isinstance(event["sequence"], int)
        or isinstance(event["sequence"], bool)
        or event["sequence"] != sequence
        or event["previous_receipt_sha256"] != previous
    ):
        raise ValueError("formal source event chain is not contiguous")
    if (
        event["challenge_id"] != challenge["challenge_id"]
        or event["nonce_digest"] != challenge["nonce_digest"]
    ):
        raise ValueError("formal source event is bound to a foreign challenge")
    expected_event_context = {
        key: challenge[key] for key in _CHALLENGE_CONTEXT_FIELDS
    }
    expected_event_context.update(
        {
            "challenge_id": challenge["challenge_id"],
            "nonce_digest": challenge["nonce_digest"],
            "install_id": install_id,
            "composition_sha256": composition_sha256,
        }
    )
    if event["context"] != expected_event_context:
        raise ValueError("formal source event carries a foreign run/runtime/install context")
    if not isinstance(event["details"], dict):
        raise ValueError("formal source event details are not an exact object")
    subjects = event["subject_ids"]
    if (
        not isinstance(subjects, list)
        or any(not isinstance(item, str) or not item for item in subjects)
        or len(subjects) != len(set(subjects))
    ):
        raise ValueError("formal source event subjects are invalid")
    kind = event["kind"]
    if kind in _BROWSER_PATHS:
        if (
            event["method"] != "POST"
            or event["path"] != _BROWSER_PATHS[kind]
            or event["response_sha256"] is not None
        ):
            raise ValueError("formal source Browser event is not the exact mounted HTTP call")
    elif kind == "icom_public_get":
        if event["method"] != "GET" or event["path"] not in _ICOM_PATHS:
            raise ValueError("formal source iCom event is outside the exact public GET contract")
        _require_sha256(
            event["response_sha256"], "formal source iCom response_sha256"
        )
    else:
        raise ValueError("formal source event kind is unknown")
    observed = _require_aware_time(event["observed_at"], "event observed_at")
    issued = _require_aware_time(challenge["issued_at"], "challenge issued_at")
    expires = _require_aware_time(challenge["expires_at"], "challenge expires_at")
    if observed < issued or observed > expires:
        raise ValueError("formal source event lies outside its challenge lifetime")
    if event["receipt_sha256"] != _sha256(_event_payload(event)):
        raise ValueError("formal source event digest is invalid")
    _verify_signature(
        {
            "purpose": "tripchord-formal-live-source-event-v3",
            "receipt_sha256": event["receipt_sha256"],
        },
        event["signature"],
        "formal source event",
    )
    return dict(event)


def _validate_snapshot(
    snapshot: object, challenge: Mapping[str, object]
) -> dict[str, object]:
    fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "install_id",
        "composition",
        "composition_sha256",
        "runtime_identity",
        "challenge",
        "challenge_id",
        "nonce_digest",
        "event_count",
        "chain_sha256",
        "last_heartbeat",
        "events",
        "signature",
    }
    if not isinstance(snapshot, dict) or set(snapshot) != fields:
        raise ValueError("formal source snapshot has an invalid shape")
    if (
        snapshot["schema_version"] != _SCHEMA_VERSION
        or snapshot["anchor_version"] != _ANCHOR_VERSION
        or snapshot["authority_key_id"] != _AUTHORITY_KEY_ID
    ):
        raise ValueError("formal source snapshot verification anchor is invalid")
    install_id = _nonempty_string(snapshot["install_id"], "formal source install_id")
    try:
        UUID(install_id)
    except ValueError as exc:
        raise ValueError("formal source install_id is invalid") from exc
    if (
        snapshot["challenge"] != challenge
        or snapshot["challenge_id"] != challenge["challenge_id"]
        or snapshot["nonce_digest"] != challenge["nonce_digest"]
    ):
        raise ValueError("formal source snapshot is bound to a foreign challenge")
    runtime = _runtime_identity(snapshot["runtime_identity"])
    if runtime != challenge["runtime_identity"]:
        raise ValueError("formal source snapshot runtime differs from challenge")
    composition = snapshot["composition"]
    if (
        not isinstance(composition, dict)
        or composition
        != formal_composition_contract(str(challenge["tested_commit_sha"]))
    ):
        raise ValueError("formal source snapshot composition is invalid")
    composition_sha256 = _require_sha256(
        snapshot["composition_sha256"], "formal source composition digest"
    )
    if composition_sha256 != _composition_sha256(
        install_id, composition
    ):
        raise ValueError("formal source snapshot composition digest is invalid")
    events = snapshot["events"]
    if (
        not isinstance(events, list)
        or not isinstance(snapshot["event_count"], int)
        or isinstance(snapshot["event_count"], bool)
        or snapshot["event_count"] != len(events)
    ):
        raise ValueError("formal source snapshot event count is invalid")
    previous = str(snapshot["composition_sha256"])
    checked: list[dict[str, object]] = []
    for sequence, event in enumerate(events, start=1):
        item = _validate_event(
            event,
            previous=previous,
            sequence=sequence,
            challenge=challenge,
            install_id=install_id,
            composition_sha256=composition_sha256,
        )
        previous = str(item["receipt_sha256"])
        checked.append(item)
    if snapshot["chain_sha256"] != previous:
        raise ValueError("formal source snapshot chain digest is invalid")
    expected_heartbeat = next(
        (item for item in reversed(checked) if item["kind"] == "browser_heartbeat"),
        None,
    )
    if snapshot["last_heartbeat"] != expected_heartbeat:
        raise ValueError("formal source snapshot heartbeat is not chain-bound")
    _verify_signature(_signed_payload(snapshot), snapshot["signature"], "formal source snapshot")
    return dict(snapshot)


def _validate_business_event_details(events: Sequence[Mapping[str, object]]) -> None:
    """Cross-check transport receipts with their exact business identities."""
    claimed: dict[str, dict[str, object]] = {}
    completed: set[str] = set()
    heartbeat_identity: dict[str, object] | None = None
    icom_by_query: dict[tuple[str, str], list[Mapping[str, object]]] = {}

    def exact_object(value: object, fields: set[str], label: str) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(f"{label} has an invalid shape")
        return value

    def query_contract(value: object, label: str) -> dict[str, object]:
        query = exact_object(
            value,
            {
                "origin",
                "destination",
                "start_date",
                "end_date",
                "adults",
                "rooms",
                "currency",
                "origin_code",
                "destination_code",
                "search_url",
                "options",
            },
            label,
        )
        if query["origin"] is not None:
            _nonempty_string(query["origin"], f"{label} origin")
        _nonempty_string(query["destination"], f"{label} destination")
        for field in ("start_date", "end_date"):
            value = query[field]
            if value is not None:
                try:
                    datetime.strptime(_nonempty_string(value, f"{label} {field}"), "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError(f"{label} {field} is invalid") from exc
        _exact_int(query["adults"], f"{label} adults", minimum=1)
        _exact_int(query["rooms"], f"{label} rooms", minimum=1)
        if query["currency"] not in {"CNY", "USD"}:
            raise ValueError(f"{label} currency is invalid")
        for field in ("origin_code", "destination_code"):
            value = query[field]
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 3
                or not value.isascii()
                or not value.isalpha()
                or value != value.upper()
            ):
                raise ValueError(f"{label} {field} is invalid")
        search_url = query["search_url"]
        if search_url is not None:
            parsed = urlsplit(_nonempty_string(search_url, f"{label} search_url"))
            if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
                raise ValueError(f"{label} search_url is invalid")
        if not isinstance(query["options"], dict):
            raise ValueError(f"{label} options are invalid")
        return query

    for event in events:
        kind = event["kind"]
        details = event["details"]
        if not isinstance(details, dict):
            raise ValueError("formal event details are not an object")
        if kind == "browser_heartbeat":
            if set(details) != {"request", "heartbeat"}:
                raise ValueError("formal heartbeat details have an invalid shape")
            request = exact_object(
                details["request"],
                {
                    "companion_id",
                    "providers",
                    "authorized_scope_keys",
                    "adapter_version",
                    "contract_version",
                    "runtime_instance_id",
                },
                "formal heartbeat request",
            )
            heartbeat = exact_object(
                details["heartbeat"],
                {
                    "companion_id",
                    "providers",
                    "last_seen",
                    "age_seconds",
                    "is_fresh",
                    "authorized_scope_keys",
                    "adapter_version",
                    "contract_version",
                    "build_identity",
                    "runtime_instance_id",
                },
                "formal heartbeat response",
            )
            identity_fields = (
                "companion_id",
                "providers",
                "authorized_scope_keys",
                "adapter_version",
                "contract_version",
                "runtime_instance_id",
            )
            identity = {key: heartbeat.get(key) for key in identity_fields}
            if any(request.get(key) != identity[key] for key in identity_fields):
                raise ValueError("formal heartbeat request and fresh Companion differ")
            if (
                heartbeat.get("is_fresh") is not True
                or heartbeat.get("companion_id") not in event["subject_ids"]
                or event["subject_ids"] != [heartbeat.get("companion_id")]
            ):
                raise ValueError("formal heartbeat is not the exact fresh mounted Companion")
            heartbeat_time = _require_aware_time(
                heartbeat["last_seen"], "formal heartbeat last_seen"
            )
            heartbeat_observed = _require_aware_time(
                event["observed_at"], "formal heartbeat observed_at"
            )
            if not timedelta(0) <= heartbeat_observed - heartbeat_time <= timedelta(seconds=1):
                raise ValueError("formal heartbeat freshness time differs from receipt")
            if (
                not isinstance(heartbeat["age_seconds"], float)
                or heartbeat["age_seconds"] < 0
            ):
                raise ValueError("formal heartbeat age_seconds is invalid")
            _nonempty_string(heartbeat["companion_id"], "formal companion_id")
            providers = _exact_string_list(
                heartbeat["providers"], "formal heartbeat providers", nonempty=True
            )
            scopes = _exact_string_list(
                heartbeat["authorized_scope_keys"],
                "formal heartbeat authorized scopes",
                nonempty=True,
            )
            if providers != sorted(providers) or scopes != sorted(scopes):
                raise ValueError("formal heartbeat provider/scope order is not canonical")
            for field in ("adapter_version", "contract_version", "runtime_instance_id"):
                _nonempty_string(heartbeat[field], f"formal heartbeat {field}")
            build = exact_object(
                heartbeat["build_identity"],
                {
                    "protocol_version",
                    "manifest_version",
                    "build_sha256",
                    "content_runtime_version",
                },
                "formal heartbeat build identity",
            )
            for field in ("protocol_version", "manifest_version", "content_runtime_version"):
                _nonempty_string(build[field], f"formal heartbeat {field}")
            _require_sha256(build["build_sha256"], "formal heartbeat build_sha256")
            identity["build_identity"] = build
            if heartbeat_identity is not None and identity != heartbeat_identity:
                raise ValueError("formal run contains a cross-Companion heartbeat")
            heartbeat_identity = identity
        elif kind == "browser_claim":
            if set(details) != {"request", "leases"}:
                raise ValueError("formal claim details have an invalid shape")
            request = exact_object(
                details["request"],
                {
                    "companion_id",
                    "providers",
                    "authorized_scope_keys",
                    "adapter_version",
                    "contract_version",
                    "limit",
                    "build_identity",
                    "runtime_instance_id",
                },
                "formal claim request",
            )
            leases = details["leases"]
            if not isinstance(leases, list) or not leases:
                raise ValueError("formal claim request/leases are invalid")
            if heartbeat_identity is None:
                raise ValueError("formal claim precedes the fresh Companion heartbeat")
            for key in (
                "companion_id",
                "providers",
                "authorized_scope_keys",
                "adapter_version",
                "contract_version",
                "runtime_instance_id",
            ):
                if request.get(key) != heartbeat_identity.get(key):
                    raise ValueError("formal claim uses a foreign Companion identity")
            if request["build_identity"] != heartbeat_identity.get("build_identity"):
                raise ValueError("formal claim build identity differs from heartbeat")
            limit = _exact_int(request["limit"], "formal claim limit", minimum=1)
            if limit > 6 or len(leases) > limit:
                raise ValueError("formal claim lease count exceeds its exact limit")
            ids: list[str] = []
            for lease in leases:
                lease = exact_object(
                    lease,
                    {
                        "task_id",
                        "provider",
                        "kind",
                        "query",
                        "timeout_seconds",
                        "claimed_at",
                        "lease_expires_at",
                    },
                    "formal claim lease",
                )
                task_id = lease.get("task_id")
                if not isinstance(task_id, str) or not task_id or task_id in claimed:
                    raise ValueError("formal claim has a duplicate/invalid task")
                if lease["provider"] not in {"ctrip", "fliggy", "qunar", "tongcheng"}:
                    raise ValueError("formal claim lease provider is invalid")
                if lease["kind"] not in {"flight", "lodging"}:
                    raise ValueError("formal claim lease kind is invalid")
                query_contract(lease["query"], "formal claim lease query")
                _exact_int(lease["timeout_seconds"], "formal claim timeout", minimum=1)
                claimed_at = _require_aware_time(lease["claimed_at"], "formal claim claimed_at")
                expires_at = _require_aware_time(
                    lease["lease_expires_at"], "formal claim lease_expires_at"
                )
                if expires_at <= claimed_at:
                    raise ValueError("formal claim lease lifetime is invalid")
                if claimed_at > _require_aware_time(
                    event["observed_at"], "formal claim observed_at"
                ):
                    raise ValueError("formal claim lease begins after its receipt")
                claimed[task_id] = dict(lease)
                ids.append(task_id)
            if ids != event["subject_ids"]:
                raise ValueError("formal claim subjects differ from signed leases")
        elif kind == "browser_complete":
            if set(details) != {"task_id", "completion", "snapshot", "result_sha256"}:
                raise ValueError("formal completion details have an invalid shape")
            task_id = details["task_id"]
            completion = exact_object(
                details["completion"],
                {"state", "quotes", "failure"},
                "formal completion body",
            )
            snapshot = exact_object(
                details["snapshot"],
                {
                    "id",
                    "provider",
                    "kind",
                    "query",
                    "state",
                    "created_at",
                    "updated_at",
                    "attempt_count",
                    "claimed_by",
                    "claimed_at",
                    "quotes",
                    "failure",
                    "reused_from_task_id",
                    "reuse_age_seconds",
                    "inflight_coalesced",
                },
                "formal completion snapshot",
            )
            if (
                not isinstance(task_id, str)
                or task_id not in claimed
                or task_id in completed
                or event["subject_ids"] != [task_id]
            ):
                raise ValueError("formal completion has a foreign/duplicate task")
            lease = claimed[task_id]
            for left, right in (
                ("id", "task_id"),
                ("provider", "provider"),
                ("kind", "kind"),
                ("query", "query"),
            ):
                if snapshot.get(left) != lease.get(right):
                    raise ValueError("formal completion is cross-task/provider/query")
            if (
                completion["state"] != snapshot["state"]
                or snapshot["state"] != "succeeded"
                or not isinstance(completion["quotes"], list)
                or not completion["quotes"]
                or completion["failure"] is not None
                or completion["quotes"] != snapshot["quotes"]
                or snapshot["failure"] is not None
                or snapshot["claimed_by"] != heartbeat_identity.get("companion_id")
                or snapshot["claimed_at"] != lease["claimed_at"]
                or snapshot["inflight_coalesced"] is not False
            ):
                raise ValueError("formal completion state transition is invalid")
            _exact_int(snapshot["attempt_count"], "formal completion attempt_count", minimum=1)
            created_at = _require_aware_time(snapshot["created_at"], "formal task created_at")
            updated_at = _require_aware_time(snapshot["updated_at"], "formal task updated_at")
            if updated_at < created_at:
                raise ValueError("formal completion timestamps are invalid")
            completion_observed = _require_aware_time(
                event["observed_at"], "formal completion observed_at"
            )
            if not timedelta(0) <= completion_observed - updated_at <= timedelta(seconds=1):
                raise ValueError("formal completion time differs from receipt")
            if snapshot["reused_from_task_id"] is not None:
                _nonempty_string(
                    snapshot["reused_from_task_id"], "formal completion reused task"
                )
            reuse_age = snapshot["reuse_age_seconds"]
            if reuse_age is not None and (
                not isinstance(reuse_age, float) or reuse_age < 0
            ):
                raise ValueError("formal completion reuse age is invalid")
            _require_sha256(details["result_sha256"], "formal completion result SHA")
            if details["result_sha256"] != _sha256(snapshot):
                raise ValueError("formal completion result SHA is invalid")
            completed.add(task_id)
        elif kind == "icom_public_get":
            if set(details) != {
                "query_task_id",
                "query_identity",
                "url",
                "path",
                "query",
                "travel_date",
                "captured_at",
                "raw_response_sha256",
                "normalized_evidence_sha256",
            }:
                raise ValueError("formal iCom details have an invalid shape")
            url = details["url"]
            query = details["query"]
            task_id = _nonempty_string(
                details["query_task_id"], "formal iCom query_task_id"
            )
            query_identity = exact_object(
                details["query_identity"],
                {"travel_date", "origin", "destination", "adults"},
                "formal iCom query identity",
            )
            if not isinstance(url, str) or not isinstance(query, dict):
                raise ValueError("formal iCom URL/query identity is invalid")
            parsed = urlsplit(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if len(pairs) != len(dict(pairs)):
                raise ValueError("formal iCom URL carries duplicate query keys")
            parsed_query = dict(pairs)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.fragment
                or parsed.path != event["path"]
                or parsed.path != details["path"]
                or parsed_query != query
            ):
                raise ValueError("formal iCom URL/path/query is not exact")
            if event["path"] == "/api/v1/public/trips/schedules":
                if set(query) != {"date"} or details["travel_date"] != query["date"]:
                    raise ValueError("formal iCom schedule date identity is invalid")
                if details["travel_date"] != query_identity["travel_date"]:
                    raise ValueError("formal iCom schedule differs from query identity")
            elif query or details["travel_date"] is not None:
                raise ValueError("formal iCom non-schedule GET carries a foreign query/date")
            try:
                datetime.strptime(
                    _nonempty_string(
                        query_identity["travel_date"], "formal iCom travel date"
                    ),
                    "%Y-%m-%d",
                )
            except ValueError as exc:
                raise ValueError("formal iCom travel date is invalid") from exc
            if query_identity["origin"] not in {"Airport", "Maafushi"} or (
                query_identity["destination"] not in {"Airport", "Maafushi"}
            ) or query_identity["origin"] == query_identity["destination"]:
                raise ValueError("formal iCom direction is invalid")
            _exact_int(query_identity["adults"], "formal iCom adults", minimum=1)
            captured_at = _require_aware_time(
                details["captured_at"], "formal iCom captured_at"
            )
            icom_observed = _require_aware_time(
                event["observed_at"], "formal iCom observed_at"
            )
            if not timedelta(0) <= icom_observed - captured_at <= timedelta(seconds=60):
                raise ValueError("formal iCom capture time differs from receipt")
            if details["raw_response_sha256"] != event["response_sha256"]:
                raise ValueError("formal iCom raw response SHA differs from receipt")
            _require_sha256(
                details["normalized_evidence_sha256"],
                "formal iCom normalized evidence",
            )
            key = (task_id, _sha256(query_identity))
            icom_by_query.setdefault(key, []).append(event)
    if not claimed or completed != set(claimed):
        raise ValueError("formal task claim/complete membership is not exact")
    if not icom_by_query:
        raise ValueError("formal run contains no iCom query graph")
    for (task_id, _query_digest), query_events in icom_by_query.items():
        paths = [str(item["path"]) for item in query_events]
        sequences = [int(item["sequence"]) for item in query_events]
        if (
            paths != list(_ICOM_PATH_ORDER)
            or sequences != list(range(sequences[0], sequences[0] + len(sequences)))
        ):
            raise ValueError(
                f"formal iCom query {task_id!r} does not bind every exact public GET"
            )


def validate_formal_source_evidence(
    binding: object,
    authority_receipt: object | None = None,
    challenge: object | None = None,
    *,
    expected_context: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not isinstance(binding, dict):
        raise ValueError("formal source binding is missing or not an exact object")
    if authority_receipt is None:
        authority_receipt = binding.get("authority_receipt")
    if challenge is None:
        challenge = binding.get("challenge")
    checked_challenge = _validate_challenge(challenge)
    fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "install_id",
        "composition",
        "composition_sha256",
        "runtime_identity",
        "challenge",
        "pre_event_count",
        "pre_chain_sha256",
        "post_event_count",
        "post_chain_sha256",
        "companion_heartbeat_receipt",
        "receipts",
        "binding_digest",
        "authority_receipt",
    }
    if set(binding) != fields:
        raise ValueError("formal source binding has an invalid shape")
    if (
        binding["schema_version"] != _BINDING_SCHEMA_VERSION
        or binding["anchor_version"] != _ANCHOR_VERSION
        or binding["authority_key_id"] != _AUTHORITY_KEY_ID
    ):
        raise ValueError("formal source binding verification anchor is invalid")
    if binding["challenge"] != checked_challenge:
        raise ValueError("formal source binding challenge differs from signed challenge")
    if binding["runtime_identity"] != checked_challenge["runtime_identity"]:
        raise ValueError("formal source binding runtime differs from challenge")
    install_id = _nonempty_string(binding["install_id"], "formal source install_id")
    try:
        UUID(install_id)
    except ValueError as exc:
        raise ValueError("formal source install_id is invalid") from exc
    composition = binding["composition"]
    if (
        not isinstance(composition, dict)
        or composition
        != formal_composition_contract(str(checked_challenge["tested_commit_sha"]))
    ):
        raise ValueError("formal source binding composition is invalid")
    composition_sha256 = _require_sha256(
        binding["composition_sha256"], "formal source composition digest"
    )
    if composition_sha256 != _composition_sha256(
        install_id, composition
    ):
        raise ValueError("formal source binding composition digest is invalid")
    pre = binding["pre_event_count"]
    post = binding["post_event_count"]
    receipts = binding["receipts"]
    if (
        not isinstance(pre, int)
        or isinstance(pre, bool)
        or pre < 0
        or not isinstance(post, int)
        or isinstance(post, bool)
        or pre != 0
        or post <= pre
        or not isinstance(receipts, list)
        or post - pre != len(receipts)
    ):
        raise ValueError("formal source binding delta range is invalid")
    previous = _require_sha256(binding["pre_chain_sha256"], "formal source pre-chain")
    checked_events: list[dict[str, object]] = []
    for sequence, event in enumerate(receipts, start=pre + 1):
        item = _validate_event(
            event,
            previous=previous,
            sequence=sequence,
            challenge=checked_challenge,
            install_id=install_id,
            composition_sha256=composition_sha256,
        )
        previous = str(item["receipt_sha256"])
        checked_events.append(item)
    if binding["post_chain_sha256"] != previous:
        raise ValueError("formal source binding post-chain is invalid")
    heartbeat = binding["companion_heartbeat_receipt"]
    if (
        not isinstance(heartbeat, dict)
        or heartbeat not in checked_events
        or heartbeat.get("kind") != "browser_heartbeat"
    ):
        raise ValueError("formal source binding heartbeat is not in this run delta")
    kinds = [item["kind"] for item in checked_events]
    if not kinds or kinds[0] != "browser_heartbeat":
        raise ValueError("formal source run delta does not start with its fresh heartbeat")
    claimed = [
        subject
        for item in checked_events
        if item["kind"] == "browser_claim"
        for subject in item["subject_ids"]
    ]
    completed = [
        subject
        for item in checked_events
        if item["kind"] == "browser_complete"
        for subject in item["subject_ids"]
    ]
    if (
        not claimed
        or sorted(claimed) != sorted(completed)
        or len(claimed) != len(set(claimed))
    ):
        raise ValueError("formal source claim/complete task transition is not exact")
    _validate_business_event_details(checked_events)
    unsigned_binding = {
        key: item
        for key, item in binding.items()
        if key not in {"binding_digest", "authority_receipt"}
    }
    if binding["binding_digest"] != _sha256(unsigned_binding):
        raise ValueError("formal source binding digest is invalid")
    receipt_fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "challenge_id",
        "nonce_digest",
        "binding_digest",
        "run_id",
        "tested_commit_sha",
        "runtime_identity",
        "pre_event_count",
        "post_event_count",
        "delta_digest",
        "issued_at",
        "verified_at",
        "signature",
    }
    if not isinstance(authority_receipt, dict) or set(authority_receipt) != receipt_fields:
        raise ValueError("formal source authority receipt is missing or invalid")
    if authority_receipt != binding["authority_receipt"]:
        raise ValueError("formal source authority receipt differs from binding")
    expected_receipt = {
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "anchor_version": _ANCHOR_VERSION,
        "authority_key_id": _AUTHORITY_KEY_ID,
        "challenge_id": checked_challenge["challenge_id"],
        "nonce_digest": checked_challenge["nonce_digest"],
        "binding_digest": binding["binding_digest"],
        "run_id": checked_challenge["run_id"],
        "tested_commit_sha": checked_challenge["tested_commit_sha"],
        "runtime_identity": checked_challenge["runtime_identity"],
        "pre_event_count": pre,
        "post_event_count": post,
        "delta_digest": _sha256(receipts),
        "issued_at": checked_challenge["issued_at"],
        "verified_at": authority_receipt.get("verified_at"),
    }
    if _signed_payload(authority_receipt) != expected_receipt:
        raise ValueError("formal source authority receipt fields are not bound")
    verified_at = _require_aware_time(
        authority_receipt["verified_at"], "receipt verified_at"
    )
    issued_at = _require_aware_time(checked_challenge["issued_at"], "challenge issued_at")
    expires_at = _require_aware_time(
        checked_challenge["expires_at"], "challenge expires_at"
    )
    latest_event = max(
        _require_aware_time(item["observed_at"], "event observed_at")
        for item in checked_events
    )
    if verified_at < issued_at or verified_at < latest_event or verified_at > expires_at:
        raise ValueError("formal source authority receipt time is outside the run")
    _verify_signature(
        _receipt_proof_payload(authority_receipt),
        authority_receipt["signature"],
        "formal source authority receipt",
    )
    if expected_context is not None:
        missing = _CHALLENGE_CONTEXT_FIELDS - set(expected_context)
        if missing:
            raise ValueError("formal source expected context is incomplete")
        for key in _CHALLENGE_CONTEXT_FIELDS:
            if checked_challenge[key] != expected_context[key]:
                raise ValueError(f"formal source evidence replays a foreign {key}")
    return dict(binding)


def formal_source_evidence_summary(
    binding: object,
    authority_receipt: object,
    challenge: object,
    *,
    expected_context: Mapping[str, object],
) -> dict[str, object]:
    """Validate raw evidence, then emit the only compact/final-safe projection."""

    checked = validate_formal_source_evidence(
        binding,
        authority_receipt,
        challenge,
        expected_context=expected_context,
    )
    checked_challenge = _validate_challenge(challenge)
    if not isinstance(authority_receipt, dict):
        raise ValueError("formal source authority receipt is invalid")
    runtime = _runtime_identity(checked_challenge["runtime_identity"])
    runtime_digest = _sha256(runtime)
    summary = {
        "schema_version": "tripchord-formal-live-source-summary-v1",
        "anchor_version": checked_challenge["anchor_version"],
        "authority_key_id": checked_challenge["authority_key_id"],
        "challenge_id": checked_challenge["challenge_id"],
        "nonce_digest": checked_challenge["nonce_digest"],
        "binding_digest": checked["binding_digest"],
        "delta_digest": authority_receipt["delta_digest"],
        "run_id": checked_challenge["run_id"],
        "tested_commit_sha": checked_challenge["tested_commit_sha"],
        "runtime_identity_sha256": runtime_digest,
        "request_sha256": checked_challenge["request_sha256"],
        "candidate_set_sha256": checked_challenge["candidate_set_sha256"],
        "scenario_sha256": checked_challenge["scenario_sha256"],
        "composition_sha256": checked["composition_sha256"],
        "pre_event_count": checked["pre_event_count"],
        "post_event_count": checked["post_event_count"],
        "issued_at": checked_challenge["issued_at"],
        "expires_at": checked_challenge["expires_at"],
        "verified_at": authority_receipt["verified_at"],
        "challenge_signature": checked_challenge["signature"],
        "authority_receipt_signature": authority_receipt["signature"],
    }
    validate_formal_source_summary(summary, expected_context=expected_context)
    return summary


def validate_formal_source_summary(
    summary: object,
    *,
    expected_context: Mapping[str, object],
) -> dict[str, object]:
    fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "challenge_id",
        "nonce_digest",
        "binding_digest",
        "delta_digest",
        "run_id",
        "tested_commit_sha",
        "runtime_identity_sha256",
        "request_sha256",
        "candidate_set_sha256",
        "scenario_sha256",
        "composition_sha256",
        "pre_event_count",
        "post_event_count",
        "issued_at",
        "expires_at",
        "verified_at",
        "challenge_signature",
        "authority_receipt_signature",
    }
    if not isinstance(summary, dict) or set(summary) != fields:
        raise ValueError("formal source summary has an invalid shape")
    if summary["schema_version"] != "tripchord-formal-live-source-summary-v1":
        raise ValueError("formal source summary schema is invalid")
    required = _CHALLENGE_CONTEXT_FIELDS - {"runtime_identity"}
    if required - set(expected_context):
        raise ValueError("formal source summary expected context is incomplete")
    if "runtime_identity" in expected_context:
        runtime_digest = _sha256(
            _runtime_identity(expected_context["runtime_identity"])
        )
    else:
        runtime_digest = _require_sha256(
            expected_context.get("runtime_identity_sha256"),
            "formal source summary expected runtime digest",
        )
    expected_projection = {
        "anchor_version": _ANCHOR_VERSION,
        "authority_key_id": _AUTHORITY_KEY_ID,
        "run_id": expected_context["run_id"],
        "tested_commit_sha": expected_context["tested_commit_sha"],
        "runtime_identity_sha256": runtime_digest,
        "request_sha256": expected_context["request_sha256"],
        "candidate_set_sha256": expected_context["candidate_set_sha256"],
        "scenario_sha256": expected_context["scenario_sha256"],
    }
    if any(summary[key] != value for key, value in expected_projection.items()):
        raise ValueError("formal source summary challenge projection is inconsistent")
    for key in ("binding_digest", "delta_digest", "composition_sha256"):
        _require_sha256(summary[key], f"formal source summary {key}")
    _nonempty_string(summary["challenge_id"], "formal source summary challenge_id")
    try:
        UUID(str(summary["challenge_id"]))
    except (ValueError, TypeError) as exc:
        raise ValueError("formal source summary challenge_id is invalid") from exc
    _require_sha256(summary["nonce_digest"], "formal source summary nonce_digest")
    pre = _exact_int(summary["pre_event_count"], "formal summary pre count")
    post = _exact_int(summary["post_event_count"], "formal summary post count")
    if pre != 0 or post <= pre:
        raise ValueError("formal source summary event range is invalid")
    challenge_proof = {
        "purpose": "tripchord-formal-live-source-challenge-proof-v1",
        "schema_version": _CHALLENGE_SCHEMA_VERSION,
        "anchor_version": summary["anchor_version"],
        "authority_key_id": summary["authority_key_id"],
        "challenge_id": summary["challenge_id"],
        "nonce_digest": summary["nonce_digest"],
        "run_id": summary["run_id"],
        "tested_commit_sha": summary["tested_commit_sha"],
        "runtime_identity_sha256": summary["runtime_identity_sha256"],
        "request_sha256": summary["request_sha256"],
        "candidate_set_sha256": summary["candidate_set_sha256"],
        "scenario_sha256": summary["scenario_sha256"],
        "issued_at": summary["issued_at"],
        "expires_at": summary["expires_at"],
    }
    _verify_signature(
        challenge_proof,
        summary["challenge_signature"],
        "formal source summary challenge",
    )
    receipt_proof = {
        "purpose": "tripchord-formal-live-source-authority-receipt-proof-v1",
        "schema_version": _RECEIPT_SCHEMA_VERSION,
        "anchor_version": summary["anchor_version"],
        "authority_key_id": summary["authority_key_id"],
        "challenge_id": summary["challenge_id"],
        "nonce_digest": summary["nonce_digest"],
        "binding_digest": summary["binding_digest"],
        "run_id": summary["run_id"],
        "tested_commit_sha": summary["tested_commit_sha"],
        "runtime_identity_sha256": summary["runtime_identity_sha256"],
        "pre_event_count": pre,
        "post_event_count": post,
        "delta_digest": summary["delta_digest"],
        "issued_at": summary["issued_at"],
        "verified_at": summary["verified_at"],
    }
    _verify_signature(
        receipt_proof,
        summary["authority_receipt_signature"],
        "formal source summary authority receipt",
    )
    verified = _require_aware_time(summary["verified_at"], "formal summary verified_at")
    issued = _require_aware_time(summary["issued_at"], "formal summary issued_at")
    expires = _require_aware_time(summary["expires_at"], "formal summary expires_at")
    if verified < issued or verified > expires:
        raise ValueError("formal source summary verification time is invalid")
    return dict(summary)


def validate_formal_source_binding(binding: object) -> dict[str, object]:
    """Offline validation using only the repository-fixed public anchor."""
    return validate_formal_source_evidence(binding)


def validate_formal_source_challenge(challenge: object) -> dict[str, object]:
    """Verify a challenge offline against the fixed public anchor."""
    return _validate_challenge(challenge)


def validate_formal_source_snapshot(snapshot: object) -> dict[str, object]:
    if not isinstance(snapshot, dict):
        raise ValueError("formal source snapshot is not an object")
    challenge = snapshot.get("challenge")
    return _validate_snapshot(snapshot, _validate_challenge(challenge))


class FormalLiveSourceAuthority:
    """Production signer; only the protected API-startup loader can construct it."""

    def __init__(
        self,
        *,
        commit_sha: str,
        private_key: Ed25519PrivateKey | None = None,
        ledger_path: Path | None = None,
        runtime_identity: Mapping[str, object] | None = None,
        now: Callable[[], datetime] | None = None,
        _startup_capability: object | None = None,
    ) -> None:
        if _startup_capability is not _STARTUP_CAPABILITY:
            raise TypeError("formal API startup is the only authority constructor")
        if private_key is None or ledger_path is None or runtime_identity is None:
            raise TypeError("formal API startup authority inputs are required")
        self._private_key = private_key
        self._ledger_path = ledger_path
        self._runtime_identity = _runtime_identity(dict(runtime_identity))
        self._composition = formal_composition_contract(commit_sha)
        stable = hashlib.sha256(_PUBLIC_KEY_DER + str(_REPO_ROOT).encode()).digest()
        self._install_id = str(UUID(bytes=stable[:16]))
        self._composition_sha256 = _composition_sha256(
            self._install_id, self._composition
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._state_lock = threading.RLock()
        self._bound = False
        self._active_challenge: dict[str, object] | None = None
        self._baseline: dict[str, object] | None = None
        self._events: list[dict[str, object]] = []
        self._chain_sha256 = self._composition_sha256
        self._last_heartbeat: dict[str, object] | None = None

    def bind(
        self,
        *,
        target_app: object,
        bridge: object,
        icom_provider: object,
        live_system: object,
        flexible_system: object,
    ) -> None:
        if self._bound:
            raise RuntimeError("formal live source authority is already bound")
        actual = {
            "bridge": _qualified_type(bridge),
            "icom_provider": _qualified_type(icom_provider),
            "live_system": _qualified_type(live_system),
            "flexible_system": _qualified_type(flexible_system),
        }
        if (
            actual != _COMPOSITION_TYPES
            or getattr(live_system, "_bridge", None) is not bridge
            or getattr(live_system, "_icom_provider", None) is not icom_provider
            or getattr(flexible_system, "_live", None) is not live_system
        ):
            raise RuntimeError("formal live source composition is not the exact production graph")
        mounted = tuple(
            route
            for route in getattr(getattr(target_app, "router", None), "routes", ())
            if getattr(route, "path", None) == _BROWSER_MOUNT
        )
        state = (
            getattr(
                getattr(getattr(mounted[0], "app", None), "state", None),
                "browser_task_bridge",
                None,
            )
            if len(mounted) == 1
            else None
        )
        if len(mounted) != 1 or state is not bridge:
            raise RuntimeError("formal live source composition is not mounted by the API entry")
        self._bound = True

    def issue_challenge(
        self, context: object, *, lifetime_seconds: int = 3600
    ) -> dict[str, object]:
        with self._state_lock:
            return self._issue_challenge_locked(
                context,
                lifetime_seconds=lifetime_seconds,
            )

    def _issue_challenge_locked(
        self, context: object, *, lifetime_seconds: int
    ) -> dict[str, object]:
        if not self._bound:
            raise RuntimeError("formal source authority is not composition-bound")
        if self._active_challenge is not None:
            raise ValueError("formal source authority already has an active challenge")
        if (
            not isinstance(lifetime_seconds, int)
            or isinstance(lifetime_seconds, bool)
            or not 1 <= lifetime_seconds <= 7200
        ):
            raise ValueError("formal source challenge lifetime is invalid")
        if not isinstance(context, dict) or set(context) != _CHALLENGE_CONTEXT_FIELDS:
            raise ValueError("formal source challenge context has an invalid shape")
        runtime = _runtime_identity(context["runtime_identity"])
        if (
            runtime != self._runtime_identity
            or context["tested_commit_sha"] != self._composition["commit_sha"]
        ):
            raise ValueError("formal source challenge runtime/commit is not this API process")
        for key in ("request_sha256", "candidate_set_sha256", "scenario_sha256"):
            _require_sha256(context[key], f"formal source challenge {key}")
        if not isinstance(context["run_id"], str) or not context["run_id"]:
            raise ValueError("formal source challenge run_id is invalid")
        with self._ledger_lock():
            ledger = self._read_ledger()
            now = self._utc_now()
            for row in ledger.values():
                if row.get("state") == "issued" and now > _require_aware_time(
                    row["expires_at"], "formal source ledger expires_at"
                ):
                    row["state"] = "expired"
                    row["expired_at"] = now.isoformat()
            if any(row.get("state") == "issued" for row in ledger.values()):
                raise ValueError("formal source authority already has an active challenge")
            if any(row.get("run_id") == context["run_id"] for row in ledger.values()):
                raise ValueError("formal source run_id already has a challenge")
            issued = now
            challenge: dict[str, object] = {
                "schema_version": _CHALLENGE_SCHEMA_VERSION,
                "anchor_version": _ANCHOR_VERSION,
                "authority_key_id": _AUTHORITY_KEY_ID,
                "challenge_id": str(uuid4()),
                "nonce_digest": hashlib.sha256(os.urandom(32)).hexdigest(),
                **context,
                "issued_at": issued.isoformat(),
                "expires_at": (
                    issued + timedelta(seconds=lifetime_seconds)
                ).isoformat(),
            }
            challenge["signature"] = _sign(
                self._private_key,
                _challenge_proof_payload(challenge),
            )
            ledger[str(challenge["challenge_id"])] = {
                "run_id": context["run_id"],
                "state": "issued",
                "challenge_digest": _sha256(challenge),
                "issued_at": challenge["issued_at"],
                "expires_at": challenge["expires_at"],
            }
            self._write_ledger(ledger)
        self._active_challenge = challenge
        self._events = []
        self._chain_sha256 = self._composition_sha256
        self._last_heartbeat = None
        self._baseline = self.snapshot()
        return {"challenge": dict(challenge), "before": dict(self._baseline)}

    def record_browser_http(
        self,
        kind: str,
        *,
        subject_ids: Sequence[str],
        details: Mapping[str, object] | None = None,
    ) -> None:
        if kind not in _BROWSER_PATHS:
            raise ValueError("unknown formal Browser event")
        self._record(
            kind=kind,
            method="POST",
            path=_BROWSER_PATHS[kind],
            subject_ids=subject_ids,
            details=details or {},
            response_sha256=None,
        )

    def record_icom_http(
        self,
        path: str,
        *,
        response_sha256: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if path not in _ICOM_PATHS:
            raise ValueError("unknown formal iCom path")
        self._record(
            kind="icom_public_get",
            method="GET",
            path=path,
            subject_ids=(),
            details=details or {},
            response_sha256=response_sha256,
        )

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, object]:
        if not self._bound or self._active_challenge is None:
            raise RuntimeError("formal source snapshot requires an active signed challenge")
        challenge = self._active_challenge
        snapshot: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "anchor_version": _ANCHOR_VERSION,
            "authority_key_id": _AUTHORITY_KEY_ID,
            "install_id": self._install_id,
            "composition": self._composition,
            "composition_sha256": self._composition_sha256,
            "runtime_identity": self._runtime_identity,
            "challenge": challenge,
            "challenge_id": challenge["challenge_id"],
            "nonce_digest": challenge["nonce_digest"],
            "event_count": len(self._events),
            "chain_sha256": self._chain_sha256,
            "last_heartbeat": self._last_heartbeat,
            "events": list(self._events),
        }
        snapshot["signature"] = _sign(self._private_key, snapshot)
        return snapshot

    def public_status(self) -> dict[str, object]:
        """Non-signing status safe for the ordinary authenticated runtime route."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "anchor_version": _ANCHOR_VERSION,
            "authority_key_id": _AUTHORITY_KEY_ID,
            "install_id": self._install_id,
            "composition": self._composition,
            "composition_sha256": self._composition_sha256,
            "runtime_identity": self._runtime_identity,
            "challenge_active": self._active_challenge is not None,
        }

    def finalize(self, context: object) -> dict[str, object]:
        with self._state_lock:
            return self._finalize_locked(context)

    def _finalize_locked(self, context: object) -> dict[str, object]:
        if self._active_challenge is None or self._baseline is None:
            raise ValueError("formal source finalize has no active challenge")
        challenge = _validate_challenge(self._active_challenge)
        if (
            not isinstance(context, dict)
            or set(context) != _CHALLENGE_CONTEXT_FIELDS
            or any(context[key] != challenge[key] for key in _CHALLENGE_CONTEXT_FIELDS)
        ):
            raise ValueError("formal source finalize context differs from challenge")
        if self._utc_now() > _require_aware_time(
            challenge["expires_at"], "challenge expires_at"
        ):
            with self._ledger_lock():
                ledger = self._read_ledger()
                row = ledger.get(str(challenge["challenge_id"]))
                if isinstance(row, dict) and row.get("state") == "issued":
                    row["state"] = "expired"
                    row["expired_at"] = self._utc_now().isoformat()
                    self._write_ledger(ledger)
            self._active_challenge = None
            self._baseline = None
            raise ValueError("formal source challenge expired before consumption")
        after = self._snapshot_locked()
        pre = _validate_snapshot(self._baseline, challenge)
        post = _validate_snapshot(after, challenge)
        receipts = post["events"][int(pre["event_count"]) :]
        binding: dict[str, object] = {
            "schema_version": _BINDING_SCHEMA_VERSION,
            "anchor_version": _ANCHOR_VERSION,
            "authority_key_id": _AUTHORITY_KEY_ID,
            "install_id": self._install_id,
            "composition": self._composition,
            "composition_sha256": self._composition_sha256,
            "runtime_identity": self._runtime_identity,
            "challenge": challenge,
            "pre_event_count": pre["event_count"],
            "pre_chain_sha256": pre["chain_sha256"],
            "post_event_count": post["event_count"],
            "post_chain_sha256": post["chain_sha256"],
            "companion_heartbeat_receipt": self._last_heartbeat,
            "receipts": receipts,
        }
        binding["binding_digest"] = _sha256(binding)
        verified_at = self._utc_now().isoformat()
        receipt: dict[str, object] = {
            "schema_version": _RECEIPT_SCHEMA_VERSION,
            "anchor_version": _ANCHOR_VERSION,
            "authority_key_id": _AUTHORITY_KEY_ID,
            "challenge_id": challenge["challenge_id"],
            "nonce_digest": challenge["nonce_digest"],
            "binding_digest": binding["binding_digest"],
            "run_id": challenge["run_id"],
            "tested_commit_sha": challenge["tested_commit_sha"],
            "runtime_identity": challenge["runtime_identity"],
            "pre_event_count": pre["event_count"],
            "post_event_count": post["event_count"],
            "delta_digest": _sha256(receipts),
            "issued_at": challenge["issued_at"],
            "verified_at": verified_at,
        }
        receipt["signature"] = _sign(
            self._private_key,
            _receipt_proof_payload(receipt),
        )
        binding["authority_receipt"] = receipt
        validate_formal_source_evidence(
            binding, receipt, challenge, expected_context=context
        )
        with self._ledger_lock():
            ledger = self._read_ledger()
            row = ledger.get(str(challenge["challenge_id"]))
            if (
                not isinstance(row, dict)
                or row.get("state") != "issued"
                or row.get("challenge_digest") != _sha256(challenge)
                or row.get("run_id") != challenge["run_id"]
            ):
                raise ValueError("formal source challenge was already consumed")
            row.update(
                {
                    "state": "consumed",
                    "binding_digest": binding["binding_digest"],
                    "verified_at": verified_at,
                }
            )
            self._write_ledger(ledger)
        self._active_challenge = None
        self._baseline = None
        return {
            "challenge": challenge,
            "binding": binding,
            "authority_receipt": receipt,
        }

    def _record(
        self,
        *,
        kind: str,
        method: str,
        path: str,
        subject_ids: Sequence[str],
        details: Mapping[str, object],
        response_sha256: str | None,
    ) -> None:
        with self._state_lock:
            self._record_locked(
                kind=kind,
                method=method,
                path=path,
                subject_ids=subject_ids,
                details=details,
                response_sha256=response_sha256,
            )

    def _record_locked(
        self,
        *,
        kind: str,
        method: str,
        path: str,
        subject_ids: Sequence[str],
        details: Mapping[str, object],
        response_sha256: str | None,
    ) -> None:
        if self._active_challenge is None:
            # Normal application traffic is not formal gate evidence.  It must
            # neither poison the next run's pre-chain nor become attestable.
            return
        observed = self._utc_now()
        challenge = self._active_challenge
        if observed > _require_aware_time(
            challenge["expires_at"], "challenge expires_at"
        ):
            raise RuntimeError("formal live event occurred after challenge expiry")
        event: dict[str, object] = {
            "sequence": len(self._events) + 1,
            "kind": kind,
            "method": method,
            "path": path,
            "subject_ids": list(subject_ids),
            "details": dict(details),
            "response_sha256": response_sha256,
            "observed_at": observed.isoformat(),
            "challenge_id": challenge["challenge_id"],
            "nonce_digest": challenge["nonce_digest"],
            "context": {
                **{key: challenge[key] for key in _CHALLENGE_CONTEXT_FIELDS},
                "challenge_id": challenge["challenge_id"],
                "nonce_digest": challenge["nonce_digest"],
                "install_id": self._install_id,
                "composition_sha256": self._composition_sha256,
            },
            "previous_receipt_sha256": self._chain_sha256,
        }
        event["receipt_sha256"] = _sha256(event)
        event["signature"] = _sign(
            self._private_key,
            {
                "purpose": "tripchord-formal-live-source-event-v3",
                "receipt_sha256": event["receipt_sha256"],
            },
        )
        _validate_event(
            event,
            previous=self._chain_sha256,
            sequence=len(self._events) + 1,
            challenge=challenge,
            install_id=self._install_id,
            composition_sha256=self._composition_sha256,
        )
        self._events.append(event)
        self._chain_sha256 = str(event["receipt_sha256"])
        if kind == "browser_heartbeat":
            self._last_heartbeat = dict(event)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("formal source clock must be timezone-aware")
        return value.astimezone(UTC)

    def _read_ledger(self) -> dict[str, dict[str, object]]:
        raw = _protected_regular_file(
            self._ledger_path,
            "formal source challenge ledger",
            missing_ok=True,
        )
        if raw is None:
            return {}
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("formal source challenge ledger is corrupt") from exc
        ledger_fields = {
            "run_id",
            "state",
            "challenge_digest",
            "issued_at",
            "expires_at",
        }
        consumed_fields = ledger_fields | {"binding_digest", "verified_at"}
        expired_fields = ledger_fields | {"expired_at"}
        if not isinstance(parsed, dict):
            raise RuntimeError("formal source challenge ledger has an invalid shape")
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RuntimeError("formal source challenge ledger has an invalid shape")
            state = value.get("state")
            expected = (
                consumed_fields
                if state == "consumed"
                else expired_fields
                if state == "expired"
                else ledger_fields
            )
            if set(value) != expected or state not in {"issued", "consumed", "expired"}:
                raise RuntimeError("formal source challenge ledger row is invalid")
            _nonempty_string(value["run_id"], "formal source ledger run_id")
            _require_sha256(value["challenge_digest"], "formal source ledger challenge")
            _require_aware_time(value["issued_at"], "formal source ledger issued_at")
            _require_aware_time(value["expires_at"], "formal source ledger expires_at")
            if value["state"] == "consumed":
                _require_sha256(value["binding_digest"], "formal source ledger binding")
                _require_aware_time(value["verified_at"], "formal source ledger verified_at")
            elif value["state"] == "expired":
                _require_aware_time(value["expired_at"], "formal source ledger expired_at")
        return parsed

    @contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        lock_path = self._ledger_path.with_suffix(self._ledger_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            lock_path,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
            ):
                raise RuntimeError("formal source ledger lock is not owner-only")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _write_ledger(self, ledger: Mapping[str, object]) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        current = _protected_regular_file(
            self._ledger_path,
            "formal source challenge ledger",
            missing_ok=True,
        )
        del current
        temporary = self._ledger_path.with_name(
            f".{self._ledger_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_canonical_bytes(ledger))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._ledger_path)
            directory = os.open(
                self._ledger_path.parent,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


def load_formal_live_source_authority(
    *,
    commit_sha: str,
    runtime_identity: Mapping[str, object],
    private_key_path: Path = _DEFAULT_PRIVATE_KEY,
    ledger_path: Path = _DEFAULT_LEDGER,
    now: Callable[[], datetime] | None = None,
) -> FormalLiveSourceAuthority:
    """Load the protected signer and prove it matches the fixed public anchor.

    The process and every local program with this OS uid remain inside the
    signer trust boundary.  Ordinary API principals and Browser credentials do
    not gain this constructor capability.
    """
    raw = _protected_regular_file(
        private_key_path, "formal source authority private key"
    )
    if raw is None:  # pragma: no cover - missing_ok is false above
        raise RuntimeError("formal source authority private key is unavailable")
    try:
        loaded = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("formal source authority private key is invalid") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise RuntimeError("formal source authority private key is not Ed25519")
    public_der = loaded.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if public_der != _PUBLIC_KEY_DER:
        raise RuntimeError(
            "formal source authority private key does not match the fixed anchor"
        )
    return FormalLiveSourceAuthority(
        commit_sha=commit_sha,
        private_key=loaded,
        ledger_path=ledger_path,
        runtime_identity=runtime_identity,
        now=now,
        _startup_capability=_STARTUP_CAPABILITY,
    )
