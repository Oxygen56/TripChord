from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from uuid import uuid4

_SCHEMA_VERSION = "tripchord-formal-live-source-v1"
_BINDING_SCHEMA_VERSION = "tripchord-formal-live-source-binding-v1"
_BROWSER_MOUNT = "/browser-bridge"
_BROWSER_PATHS = {
    "browser_heartbeat": f"{_BROWSER_MOUNT}/v1/companions/heartbeat",
    "browser_claim": f"{_BROWSER_MOUNT}/v1/tasks/claim",
    "browser_complete": f"{_BROWSER_MOUNT}/v1/tasks/{{task_id}}/complete",
}
_ICOM_PATHS = frozenset(
    {
        "/api/v1/public/trips/schedules",
        "/api/v1/public/ferry-fares/schedule-base-price",
        "/api/v1/public/policy-sections",
    }
)
_COMPOSITION_TYPES = {
    "bridge": "tripchord.providers.browser_bridge.BrowserTaskBridge",
    "icom_provider": "tripchord.providers.icom_transfer.IComTransferProvider",
    "live_system": "tripchord.agents.live_system.LivePackageAgentSystem",
    "flexible_system": "tripchord.agents.flexible_live_system.FlexibleLiveAgentSystem",
}
_AUTHORITY_KEYS: dict[tuple[str, str], bytes] = {}


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _composition_sha256(install_id: str, composition: object) -> str:
    return _sha256({"install_id": install_id, "composition": composition})


def _qualified_type(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def formal_composition_contract(commit_sha: str) -> dict[str, object]:
    if len(commit_sha) != 40 or any(char not in "0123456789abcdef" for char in commit_sha):
        raise ValueError("formal composition commit_sha must be lowercase 40-hex")
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


def _event_without_proofs(event: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"receipt_sha256", "authority_mac"}
    }


def _derive_authority_key(
    authority_secret: str | bytes,
    *,
    install_id: str,
    composition_sha256: str,
) -> bytes:
    secret = (
        authority_secret.encode("utf-8")
        if isinstance(authority_secret, str)
        else authority_secret
    )
    if len(secret) < 32:
        raise ValueError("formal source authority secret is too short")
    return hmac.new(
        secret,
        _canonical_bytes(
            {
                "purpose": "tripchord-formal-live-source-authority-v1",
                "install_id": install_id,
                "composition_sha256": composition_sha256,
            }
        ),
        hashlib.sha256,
    ).digest()


def _authority_key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def _authority_mac(key: bytes, value: object) -> str:
    return hmac.new(key, _canonical_bytes(value), hashlib.sha256).hexdigest()


def _snapshot_mac_payload(
    *,
    install_id: str,
    composition_sha256: str,
    event_count: int,
    chain_sha256: str,
) -> dict[str, object]:
    return {
        "purpose": "tripchord-formal-live-source-snapshot-v1",
        "install_id": install_id,
        "composition_sha256": composition_sha256,
        "event_count": event_count,
        "chain_sha256": chain_sha256,
    }


def _resolve_authority_key(
    *,
    install_id: str,
    composition_sha256: str,
    key_id: str,
) -> bytes:
    registered = _AUTHORITY_KEYS.get((install_id, key_id))
    if registered is not None:
        return registered
    raise ValueError(
        "formal source binding has no trusted production authority capability"
    )


def _verify_event(
    event: object,
    previous_sha256: str,
    sequence: int,
    *,
    authority_key: bytes,
) -> dict[str, object]:
    if not isinstance(event, dict) or set(event) != {
        "sequence",
        "kind",
        "method",
        "path",
        "subject_ids",
        "response_sha256",
        "observed_at",
        "previous_receipt_sha256",
        "receipt_sha256",
        "authority_mac",
    }:
        raise ValueError("formal source receipt has an invalid shape")
    if event["sequence"] != sequence:
        raise ValueError("formal source receipt sequence is not contiguous")
    if event["previous_receipt_sha256"] != previous_sha256:
        raise ValueError("formal source receipt chain predecessor is invalid")
    if event["receipt_sha256"] != _sha256(_event_without_proofs(event)):
        raise ValueError("formal source receipt digest is invalid")
    expected_mac = _authority_mac(
        authority_key,
        {
            "purpose": "tripchord-formal-live-source-receipt-v1",
            "receipt_sha256": event["receipt_sha256"],
        },
    )
    if not isinstance(event["authority_mac"], str) or not hmac.compare_digest(
        event["authority_mac"], expected_mac
    ):
        raise ValueError("formal source receipt lacks production authority proof")
    kind = event["kind"]
    method = event["method"]
    path = event["path"]
    subjects = event["subject_ids"]
    response_sha256 = event["response_sha256"]
    if not isinstance(subjects, list) or any(
        not isinstance(item, str) or not item for item in subjects
    ):
        raise ValueError("formal source receipt subjects are invalid")
    if kind in _BROWSER_PATHS:
        if method != "POST" or path != _BROWSER_PATHS[kind]:
            raise ValueError("formal source browser receipt is not an exact mounted HTTP call")
        if response_sha256 is not None:
            raise ValueError("formal source browser receipt carries an unexpected response digest")
    elif kind == "icom_public_get":
        if method != "GET" or path not in _ICOM_PATHS:
            raise ValueError("formal source iCom receipt is outside the exact public GET contract")
        if (
            not isinstance(response_sha256, str)
            or len(response_sha256) != 64
            or any(char not in "0123456789abcdef" for char in response_sha256)
        ):
            raise ValueError("formal source iCom receipt has no response digest")
    else:
        raise ValueError("formal source receipt kind is unknown")
    if not isinstance(event["observed_at"], str) or not event["observed_at"]:
        raise ValueError("formal source receipt has no observation time")
    return dict(event)


class FormalLiveSourceAuthority:
    """Process-local authority installed only by the production composition entry.

    It observes successful mounted Browser HTTP handlers and successful public
    iCom GETs at their actual transport boundaries.  A direct bridge method call
    never reaches this recorder, and an unbound hand-built object graph cannot
    emit a valid snapshot.
    """

    def __init__(
        self,
        *,
        commit_sha: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._install_id = str(uuid4())
        self._composition = formal_composition_contract(commit_sha)
        self._composition_sha256 = _composition_sha256(
            self._install_id,
            self._composition,
        )
        # This key is intentionally independent of every caller credential.
        # In particular, the Browser bridge token is presented by the Companion
        # and by the gate runner, so deriving authority from it lets either side
        # mint a complete, self-consistent fake receipt chain.  Only the API
        # process that executed the formal composition entry holds this random
        # capability; other processes can ask that authority to verify a binding
        # but cannot derive or register a replacement key from payload bytes.
        self._authority_key = secrets.token_bytes(32)
        self._authority_key_id = _authority_key_id(self._authority_key)
        _AUTHORITY_KEYS[(self._install_id, self._authority_key_id)] = self._authority_key
        self._now = now or (lambda: datetime.now(UTC))
        self._bound = False
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
        actual_types = {
            "bridge": _qualified_type(bridge),
            "icom_provider": _qualified_type(icom_provider),
            "live_system": _qualified_type(live_system),
            "flexible_system": _qualified_type(flexible_system),
        }
        if actual_types != _COMPOSITION_TYPES:
            raise RuntimeError("formal live source composition uses a foreign concrete type")
        if getattr(live_system, "_bridge", None) is not bridge:
            raise RuntimeError("formal live source composition has a foreign browser bridge")
        if getattr(live_system, "_icom_provider", None) is not icom_provider:
            raise RuntimeError("formal live source composition has a foreign iCom provider")
        if getattr(flexible_system, "_live", None) is not live_system:
            raise RuntimeError("formal live source composition has a foreign pair runner")
        mounted = tuple(
            route
            for route in getattr(getattr(target_app, "router", None), "routes", ())
            if getattr(route, "path", None) == _BROWSER_MOUNT
        )
        mounted_state = (
            getattr(getattr(mounted[0], "app", None), "state", None)
            if len(mounted) == 1
            else None
        )
        if len(mounted) != 1 or getattr(
            mounted_state, "browser_task_bridge", None
        ) is not bridge:
            raise RuntimeError("formal live source composition is not mounted by the app entry")
        self._bound = True

    def record_browser_http(
        self,
        kind: str,
        *,
        subject_ids: Sequence[str],
    ) -> None:
        if kind not in _BROWSER_PATHS:
            raise ValueError("unknown formal browser source event")
        self._record(
            kind=kind,
            method="POST",
            path=_BROWSER_PATHS[kind],
            subject_ids=subject_ids,
            response_sha256=None,
        )

    def record_icom_http(self, path: str, *, response_sha256: str) -> None:
        if path not in _ICOM_PATHS:
            raise ValueError("unknown formal iCom source path")
        self._record(
            kind="icom_public_get",
            method="GET",
            path=path,
            subject_ids=(),
            response_sha256=response_sha256,
        )

    def snapshot(self) -> dict[str, object]:
        if not self._bound:
            raise RuntimeError("formal live source authority is not composition-bound")
        return {
            "schema_version": _SCHEMA_VERSION,
            "install_id": self._install_id,
            "composition": self._composition,
            "composition_sha256": self._composition_sha256,
            "authority_key_id": self._authority_key_id,
            "event_count": len(self._events),
            "chain_sha256": self._chain_sha256,
            "authority_mac": _authority_mac(
                self._authority_key,
                _snapshot_mac_payload(
                    install_id=self._install_id,
                    composition_sha256=self._composition_sha256,
                    event_count=len(self._events),
                    chain_sha256=self._chain_sha256,
                ),
            ),
            "last_heartbeat": self._last_heartbeat,
            "events": list(self._events),
        }

    def validate_snapshot(self, snapshot: object) -> dict[str, object]:
        """Validate against this exact installed production authority."""

        self._require_own_identity(snapshot)
        return validate_formal_source_snapshot(snapshot)

    def build_binding(self, before: object, after: object) -> dict[str, object]:
        """Issue a run binding only for snapshots owned by this installation."""

        self._require_own_identity(before)
        self._require_own_identity(after)
        return build_formal_source_binding(before, after)

    def validate_binding(self, binding: object) -> dict[str, object]:
        """Validate a binding against this exact installed production authority."""

        self._require_own_identity(binding)
        return validate_formal_source_binding(binding)

    def _require_own_identity(self, value: object) -> None:
        if not isinstance(value, dict):
            raise ValueError("formal source authority input is not an object")
        if (
            value.get("install_id") != self._install_id
            or value.get("composition_sha256") != self._composition_sha256
            or value.get("authority_key_id") != self._authority_key_id
        ):
            raise ValueError("formal source authority input is not owned by this installation")

    def _record(
        self,
        *,
        kind: str,
        method: str,
        path: str,
        subject_ids: Sequence[str],
        response_sha256: str | None,
    ) -> None:
        if not self._bound:
            raise RuntimeError("formal live source event was emitted before composition binding")
        observed_at = self._now()
        if observed_at.tzinfo is None:
            raise RuntimeError("formal live source clock must be timezone-aware")
        event: dict[str, object] = {
            "sequence": len(self._events) + 1,
            "kind": kind,
            "method": method,
            "path": path,
            "subject_ids": sorted(set(subject_ids)),
            "response_sha256": response_sha256,
            "observed_at": observed_at.astimezone(UTC).isoformat(),
            "previous_receipt_sha256": self._chain_sha256,
        }
        event["receipt_sha256"] = _sha256(event)
        event["authority_mac"] = _authority_mac(
            self._authority_key,
            {
                "purpose": "tripchord-formal-live-source-receipt-v1",
                "receipt_sha256": event["receipt_sha256"],
            },
        )
        self._events.append(event)
        self._chain_sha256 = str(event["receipt_sha256"])
        if kind == "browser_heartbeat":
            self._last_heartbeat = dict(event)


def validate_formal_source_snapshot(
    snapshot: object,
) -> dict[str, object]:
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "schema_version",
        "install_id",
        "composition",
        "composition_sha256",
        "authority_key_id",
        "event_count",
        "chain_sha256",
        "authority_mac",
        "last_heartbeat",
        "events",
    }:
        raise ValueError("formal source snapshot has an invalid shape")
    if snapshot["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("formal source snapshot schema is invalid")
    install_id = snapshot["install_id"]
    if not isinstance(install_id, str) or len(install_id) != 36:
        raise ValueError("formal source snapshot install identity is invalid")
    composition = snapshot["composition"]
    if not isinstance(composition, dict):
        raise ValueError("formal source snapshot composition is not an object")
    commit_sha = composition.get("commit_sha")
    if not isinstance(commit_sha, str) or composition != formal_composition_contract(commit_sha):
        raise ValueError("formal source snapshot composition contract is invalid")
    composition_sha256 = _composition_sha256(install_id, composition)
    if snapshot["composition_sha256"] != composition_sha256:
        raise ValueError("formal source snapshot composition digest is invalid")
    key_id = snapshot["authority_key_id"]
    if not isinstance(key_id, str) or len(key_id) != 64:
        raise ValueError("formal source snapshot authority key identity is invalid")
    authority_key = _resolve_authority_key(
        install_id=install_id,
        composition_sha256=composition_sha256,
        key_id=key_id,
    )
    events = snapshot["events"]
    if not isinstance(events, list) or snapshot["event_count"] != len(events):
        raise ValueError("formal source snapshot event count is invalid")
    previous = composition_sha256
    verified_events: list[dict[str, object]] = []
    for sequence, event in enumerate(events, start=1):
        verified = _verify_event(
            event,
            previous,
            sequence,
            authority_key=authority_key,
        )
        previous = str(verified["receipt_sha256"])
        verified_events.append(verified)
    if snapshot["chain_sha256"] != previous:
        raise ValueError("formal source snapshot terminal chain digest is invalid")
    expected_snapshot_mac = _authority_mac(
        authority_key,
        _snapshot_mac_payload(
            install_id=install_id,
            composition_sha256=composition_sha256,
            event_count=len(events),
            chain_sha256=previous,
        ),
    )
    if not isinstance(snapshot["authority_mac"], str) or not hmac.compare_digest(
        snapshot["authority_mac"], expected_snapshot_mac
    ):
        raise ValueError("formal source snapshot lacks production authority proof")
    heartbeat = snapshot["last_heartbeat"]
    expected_heartbeat = next(
        (event for event in reversed(verified_events) if event["kind"] == "browser_heartbeat"),
        None,
    )
    if heartbeat != expected_heartbeat:
        raise ValueError("formal source snapshot heartbeat receipt is not chain-bound")
    return dict(snapshot)


def build_formal_source_binding(
    before: object,
    after: object,
) -> dict[str, object]:
    pre = validate_formal_source_snapshot(before)
    post = validate_formal_source_snapshot(after)
    for key in ("install_id", "composition", "composition_sha256", "authority_key_id"):
        if pre[key] != post[key]:
            raise ValueError("formal source authority changed during the live run")
    pre_count = int(pre["event_count"])
    post_count = int(post["event_count"])
    if post_count < pre_count or post["events"][:pre_count] != pre["events"]:
        raise ValueError("formal source receipt history changed during the live run")
    heartbeat = post["last_heartbeat"] or pre["last_heartbeat"]
    if heartbeat is None:
        raise ValueError("formal source binding has no mounted HTTP heartbeat receipt")
    binding = {
        "schema_version": _BINDING_SCHEMA_VERSION,
        "install_id": post["install_id"],
        "composition": post["composition"],
        "composition_sha256": post["composition_sha256"],
        "authority_key_id": post["authority_key_id"],
        "pre_event_count": pre_count,
        "pre_chain_sha256": pre["chain_sha256"],
        "pre_authority_mac": pre["authority_mac"],
        "post_event_count": post_count,
        "post_chain_sha256": post["chain_sha256"],
        "post_authority_mac": post["authority_mac"],
        "companion_heartbeat_receipt": heartbeat,
        "receipts": post["events"][pre_count:],
    }
    validate_formal_source_binding(binding)
    return binding


def validate_formal_source_binding(
    binding: object,
) -> dict[str, object]:
    if not isinstance(binding, dict) or set(binding) != {
        "schema_version",
        "install_id",
        "composition",
        "composition_sha256",
        "authority_key_id",
        "pre_event_count",
        "pre_chain_sha256",
        "pre_authority_mac",
        "post_event_count",
        "post_chain_sha256",
        "post_authority_mac",
        "companion_heartbeat_receipt",
        "receipts",
    }:
        raise ValueError("formal source binding has an invalid shape")
    if binding["schema_version"] != _BINDING_SCHEMA_VERSION:
        raise ValueError("formal source binding schema is invalid")
    install_id = binding["install_id"]
    if (
        not isinstance(install_id, str)
        or len(install_id) != 36
        or install_id.count("-") != 4
    ):
        raise ValueError("formal source binding install identity is invalid")
    composition = binding["composition"]
    if not isinstance(composition, dict):
        raise ValueError("formal source binding composition is not an object")
    commit_sha = composition.get("commit_sha")
    if not isinstance(commit_sha, str) or composition != formal_composition_contract(commit_sha):
        raise ValueError("formal source binding composition contract is invalid")
    composition_sha256 = _composition_sha256(install_id, composition)
    if binding["composition_sha256"] != composition_sha256:
        raise ValueError("formal source binding composition digest is invalid")
    key_id = binding["authority_key_id"]
    if not isinstance(key_id, str) or len(key_id) != 64:
        raise ValueError("formal source binding authority key identity is invalid")
    authority_key = _resolve_authority_key(
        install_id=install_id,
        composition_sha256=composition_sha256,
        key_id=key_id,
    )
    pre_count = binding["pre_event_count"]
    post_count = binding["post_event_count"]
    receipts = binding["receipts"]
    if (
        not isinstance(pre_count, int)
        or isinstance(pre_count, bool)
        or pre_count < 0
        or not isinstance(post_count, int)
        or isinstance(post_count, bool)
        or post_count < pre_count
        or not isinstance(receipts, list)
        or post_count - pre_count != len(receipts)
    ):
        raise ValueError("formal source binding event range is invalid")
    previous = binding["pre_chain_sha256"]
    if not isinstance(previous, str) or len(previous) != 64:
        raise ValueError("formal source binding pre-chain digest is invalid")
    verified: list[dict[str, object]] = []
    for sequence, event in enumerate(receipts, start=pre_count + 1):
        item = _verify_event(
            event,
            previous,
            sequence,
            authority_key=authority_key,
        )
        previous = str(item["receipt_sha256"])
        verified.append(item)
    if binding["post_chain_sha256"] != previous:
        raise ValueError("formal source binding post-chain digest is invalid")
    for prefix, count, chain in (
        ("pre", pre_count, binding["pre_chain_sha256"]),
        ("post", post_count, binding["post_chain_sha256"]),
    ):
        expected_mac = _authority_mac(
            authority_key,
            _snapshot_mac_payload(
                install_id=install_id,
                composition_sha256=composition_sha256,
                event_count=count,
                chain_sha256=str(chain),
            ),
        )
        actual_mac = binding[f"{prefix}_authority_mac"]
        if not isinstance(actual_mac, str) or not hmac.compare_digest(
            actual_mac, expected_mac
        ):
            raise ValueError(
                f"formal source binding {prefix} snapshot lacks production authority proof"
            )
    heartbeat = binding["companion_heartbeat_receipt"]
    if not isinstance(heartbeat, dict):
        raise ValueError("formal source binding heartbeat receipt is missing")
    heartbeat_previous = heartbeat.get("previous_receipt_sha256")
    heartbeat_sequence = heartbeat.get("sequence")
    if not isinstance(heartbeat_previous, str) or not isinstance(heartbeat_sequence, int):
        raise ValueError("formal source binding heartbeat receipt is invalid")
    checked_heartbeat = _verify_event(
        heartbeat,
        heartbeat_previous,
        heartbeat_sequence,
        authority_key=authority_key,
    )
    if checked_heartbeat["kind"] != "browser_heartbeat" or not checked_heartbeat["subject_ids"]:
        raise ValueError("formal source binding heartbeat is not a mounted Companion receipt")
    claimed = {
        subject
        for event in verified
        if event["kind"] == "browser_claim"
        for subject in event["subject_ids"]
    }
    completed = {
        subject
        for event in verified
        if event["kind"] == "browser_complete"
        for subject in event["subject_ids"]
    }
    if not claimed or not completed or not completed.issubset(claimed):
        raise ValueError("formal source binding lacks matched mounted claim/complete receipts")
    icom_paths = {
        str(event["path"])
        for event in verified
        if event["kind"] == "icom_public_get"
    }
    if icom_paths != _ICOM_PATHS:
        raise ValueError("formal source binding lacks the exact three public iCom GET receipts")
    return dict(binding)
