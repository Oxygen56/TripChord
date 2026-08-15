from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.parse import quote as url_quote
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from tripchord.agents.stay_area import (
    StayAreaSearchProfile,
    system_stay_area_search_profile,
)
from tripchord.planning.frozen_graph import (
    frozen_v4_canonical_pair_ids,
    frozen_v4_icom_task_ids,
    frozen_v4_ordered_per_pair_query_task_ids,
)
from tripchord.planning.stay_plans import (
    StayPlanCandidateSet,
    system_stay_plan_candidate_set,
)

_SCHEMA_VERSION = "tripchord-formal-live-source-v3"
_BINDING_SCHEMA_VERSION = "tripchord-formal-live-source-binding-v3"
_CHALLENGE_SCHEMA_VERSION = "tripchord-formal-live-source-challenge-v3"
_RECEIPT_SCHEMA_VERSION = "tripchord-formal-live-source-authority-receipt-v3"
_EXECUTION_CAPABILITY_SCHEMA_VERSION = (
    "tripchord-formal-live-source-execution-capability-v1"
)
_STARTUP_CAPABILITY = object()
_REPO_ROOT = Path(__file__).resolve().parents[4]
_TRUST_ROOT_ENV = "TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT"
_GENERATION_PATTERN = re.compile(r"^generation-([1-9][0-9]*)-([0-9a-f]{12})$")

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
_ICOM_PUBLIC_HOST = "sfs-api.icomtours.com"
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
    "job_graph",
}
_FINALIZE_CONTEXT_FIELDS = _CHALLENGE_CONTEXT_FIELDS | {
    "terminal_job",
    "pair_checkpoint_binding",
}
_EVENT_CONTEXT_FIELDS = (_CHALLENGE_CONTEXT_FIELDS - {"job_graph"}) | {
    "job_graph_sha256"
}
_EXECUTION_CAPABILITY_CONTEXT: ContextVar[dict[str, object] | None] = ContextVar(
    "tripchord_formal_execution_capability",
    default=None,
)


def _frozen_per_pair_tasks() -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for item in frozen_v4_ordered_per_pair_query_task_ids():
        if len(item) != 1:
            raise RuntimeError("frozen query-task authority has an invalid shape")
        pair_id, task_ids = next(iter(item.items()))
        result[pair_id] = tuple(task_ids)
    if tuple(result) != frozen_v4_canonical_pair_ids():
        raise RuntimeError("frozen query-task authority order differs from pair authority")
    return result


def _validate_formal_browser_search_url(
    query: Mapping[str, object],
    *,
    provider: object,
    kind: object,
) -> None:
    """Require the byte-exact internally generated public search URL.

    No signed/authenticated wrapper can widen this contract.  Exact equality
    rejects userinfo, alternate hosts/paths, duplicate or unknown parameters,
    encoded smuggling, fragments, tracking identifiers, and reordered fields.
    """

    provider_name = _nonempty_string(provider, "formal Browser provider")
    vertical = _nonempty_string(kind, "formal Browser kind")
    search_url = query.get("search_url")
    if vertical == "lodging":
        if search_url is not None:
            raise ValueError("formal Browser query URL is outside the signed graph")
        return
    if vertical != "flight" or provider_name not in {"ctrip", "qunar", "tongcheng"}:
        raise ValueError("formal Browser query URL is outside the signed graph")
    origin_code = query.get("origin_code")
    destination_code = query.get("destination_code")
    start_date = _nonempty_string(query.get("start_date"), "formal Browser start_date")
    end_date = _nonempty_string(query.get("end_date"), "formal Browser end_date")
    adults = _exact_int(query.get("adults"), "formal Browser adults", minimum=1)
    if (
        origin_code != "HGH"
        or destination_code != "MLE"
        or query.get("origin") != "杭州"
        or query.get("destination") != "马累"
    ):
        raise ValueError("formal Browser query URL identity is not frozen")
    if provider_name == "ctrip":
        expected_url = (
            "https://flights.ctrip.com/international/search/round-hgh-mle"
            f"?depdate={start_date}_{end_date}&cabin=y_s"
            f"&adult={adults}&child=0&infant=0"
        )
    elif provider_name == "qunar":
        parameters = (
            ("from", "flight_int_search"),
            ("showTotalPr", "0"),
            ("searchType", "RoundTripFlight"),
            ("fromCity", "杭州"),
            ("toCity", "马累"),
            ("adultNum", str(adults)),
            ("childNum", "0"),
            ("fromDate", start_date),
            ("toDate", end_date),
        )
        expected_url = (
            "https://flight.qunar.com/twell/flight/Search.jsp?"
            + urlencode(parameters, quote_via=url_quote, safe="")
        )
    else:
        para = "*".join(
            (
                "HGH",
                "MLE",
                start_date,
                end_date,
                "RT",
                f"{adults}_0_0",
                "Y|S|C|F",
            )
        )
        expected_url = (
            "https://www.ly.com/eliflight/book1.html"
            f"?para={url_quote(para, safe='*')}"
            f"&departureCity={url_quote('杭州', safe='')}"
            f"&arrivalCity={url_quote('马累', safe='')}"
        )
    if search_url != expected_url:
        raise ValueError("formal Browser query URL is not the exact public search URL")


def _resolve_formal_browser_job_member(
    job_graph: Mapping[str, object],
    *,
    provider: object,
    kind: object,
    query: Mapping[str, object],
) -> dict[str, object]:
    """Resolve actual query fields to exactly one canonical signed graph member."""

    graph = _validate_job_graph(job_graph)
    provider_name = _nonempty_string(provider, "formal Browser provider")
    vertical = _nonempty_string(kind, "formal Browser kind")
    if provider_name not in {"ctrip", "qunar", "tongcheng"}:
        raise ValueError("formal Browser provider is outside the frozen graph")
    if vertical not in {"flight", "lodging"}:
        raise ValueError("formal Browser kind is outside the frozen graph")
    adults = _exact_int(query.get("adults"), "formal Browser adults", minimum=1)
    if adults != graph["adults"]:
        raise ValueError("formal Browser query is not an exact signed job member")
    start = _nonempty_string(query.get("start_date"), "formal Browser start_date")
    end = _nonempty_string(query.get("end_date"), "formal Browser end_date")
    options = query.get("options")
    if not isinstance(options, dict):
        raise ValueError("formal Browser query options are invalid")
    _validate_formal_browser_search_url(query, provider=provider_name, kind=vertical)
    segment = options.get("segment")
    pairs = graph["pairs"]
    if not isinstance(pairs, list):  # canonical graph validation guards this
        raise ValueError("formal Browser job graph pairs are invalid")
    resolved: list[dict[str, object]] = []
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        departure = datetime.strptime(str(pair["departure_date"]), "%Y-%m-%d").date()
        returning = datetime.strptime(str(pair["return_date"]), "%Y-%m-%d").date()
        if vertical == "flight":
            if segment is not None or (start, end) != (
                departure.isoformat(),
                returning.isoformat(),
            ):
                continue
            query_kind = "flight"
            direction = "round_trip"
        else:
            segment_contract = {
                "full": ("lodging_full_stay", departure, returning),
                "first": (
                    "lodging_first_night",
                    departure,
                    departure + timedelta(days=1),
                ),
                "middle": (
                    "lodging_middle_stay",
                    departure + timedelta(days=1),
                    returning - timedelta(days=1),
                ),
                "last": (
                    "lodging_last_night",
                    returning - timedelta(days=1),
                    returning,
                ),
                "hulhumale-full": (
                    "lodging_hulhumale_full_stay",
                    departure,
                    returning,
                ),
            }.get(segment)
            if segment_contract is None:
                continue
            query_kind, expected_start, expected_end = segment_contract
            if (start, end) != (expected_start.isoformat(), expected_end.isoformat()):
                continue
            direction = "stay"
        prefix = f"query:{provider_name}:{query_kind}:"
        members = [
            item
            for item in pair["query_task_ids"]
            if isinstance(item, str) and item.startswith(prefix)
        ]
        if len(members) != 1:
            continue
        phase = (
            "publication_refresh"
            if options.get("__tripchord_allow_recent_quote_reuse") is False
            else "checkpoint_exploration"
        )
        if phase == "publication_refresh" and members[0] not in pair[
            "publication_query_task_ids"
        ]:
            continue
        resolved.append(
            {
                "pair_id": pair["date_pair_id"],
                "query_task_id": members[0],
                "query_kind": query_kind,
                "direction": direction,
                "start_date": start,
                "end_date": end,
                "execution_phase": phase,
            }
        )
    if len(resolved) != 1:
        raise ValueError("formal Browser query is not an exact signed job member")
    return resolved[0]


def formal_job_graph_for_frozen_v4(
    *,
    terminal_job_id: str,
    request_sha256: str,
    adults: int,
) -> dict[str, object]:
    """Build the exact pre-execution graph a formal challenge is allowed to bind.

    The terminal job id already exists at this point, but its prepared operation
    has not started.  Every pair, Browser query task, checkpoint identity and
    iCom query is derived from the committed frozen graph rather than supplied
    by the caller as a self-consistent set.
    """

    job_id = _nonempty_string(terminal_job_id, "formal terminal job id")
    if not job_id.startswith("live-job-"):
        raise ValueError("formal terminal job id is invalid")
    request_digest = _require_sha256(request_sha256, "formal job request_sha256")
    party_size = _exact_int(adults, "formal job adults", minimum=1)
    if party_size > 9:
        raise ValueError("formal job adults is invalid")
    per_pair = _frozen_per_pair_tasks()
    icom_source_ids = tuple(
        item
        for item in (
            "public-transfer-icom-continuous-outbound",
            "public-transfer-icom-split-outbound",
            "public-transfer-icom-split-inbound",
            "public-transfer-icom-continuous-inbound",
        )
        if item in frozen_v4_icom_task_ids()
    )
    if set(icom_source_ids) != set(frozen_v4_icom_task_ids()):
        raise RuntimeError("frozen iCom task authority differs from formal order")
    pairs: list[dict[str, object]] = []
    icom_queries: list[dict[str, object]] = []
    publication_icom_queries: list[dict[str, object]] = []
    for sequence, pair_id in enumerate(frozen_v4_canonical_pair_ids(), start=1):
        parts = pair_id.split(":")
        if len(parts) != 4:
            raise RuntimeError("frozen pair authority has an invalid identity")
        departure, returning = parts[1], parts[2]
        departure_date = datetime.strptime(departure, "%Y-%m-%d").date()
        return_date = datetime.strptime(returning, "%Y-%m-%d").date()
        task_ids = list(per_pair[pair_id])
        publication_task_ids = (
            [
                task_id
                for task_id in task_ids
                if task_id.startswith("query:ctrip:flight:")
                or task_id.startswith("query:ctrip:lodging_full_stay:")
            ]
            if sequence in {1, 3}
            else []
        )
        if len(publication_task_ids) != (2 if sequence in {1, 3} else 0):
            raise RuntimeError("frozen publication query authority is invalid")
        checkpoint_identity = {
            "sequence": sequence,
            "date_pair_id": pair_id,
            "departure_date": departure,
            "return_date": returning,
            "request_sha256": request_digest,
            "query_task_ids": task_ids,
        }
        pairs.append(
            {
                **checkpoint_identity,
                "query_task_ids_sha256": _sha256(task_ids),
                "publication_query_task_ids": publication_task_ids,
                "publication_query_task_ids_sha256": _sha256(
                    publication_task_ids
                ),
                "checkpoint_identity_sha256": _sha256(checkpoint_identity),
            }
        )
        query_specs = (
            (
                icom_source_ids[0],
                "outbound",
                departure_date,
                "Airport",
                "Maafushi",
            ),
            (
                icom_source_ids[1],
                "outbound",
                departure_date + timedelta(days=1),
                "Airport",
                "Maafushi",
            ),
            (
                icom_source_ids[2],
                "inbound",
                return_date - timedelta(days=1),
                "Maafushi",
                "Airport",
            ),
            (
                icom_source_ids[3],
                "inbound",
                return_date,
                "Maafushi",
                "Airport",
            ),
        )
        for source_task_id, direction, travel_date, origin, destination in query_specs:
            query_task_id = f"{pair_id}|{source_task_id}"
            identity = {
                "pair_id": pair_id,
                "source_task_id": source_task_id,
                "query_task_id": query_task_id,
                "direction": direction,
                "travel_date": travel_date.isoformat(),
                "departure_date": departure,
                "return_date": returning,
                "origin": origin,
                "destination": destination,
                "adults": party_size,
            }
            icom_queries.append(
                {**identity, "query_identity_sha256": _sha256(identity)}
            )
        publication_specs = (
            (
                ("outbound", departure_date, "Airport", "Maafushi"),
                ("inbound", return_date, "Maafushi", "Airport"),
            )
            if sequence in {1, 3}
            else ()
        )
        for direction, travel_date, origin, destination in publication_specs:
            source_task_id = (
                "publication-public-transfer-icom-"
                f"{origin.lower()}-{destination.lower()}-"
                f"{travel_date.isoformat()}"
            )
            query_task_id = f"{pair_id}|{source_task_id}"
            identity = {
                "pair_id": pair_id,
                "source_task_id": source_task_id,
                "query_task_id": query_task_id,
                "direction": direction,
                "travel_date": travel_date.isoformat(),
                "departure_date": departure,
                "return_date": returning,
                "origin": origin,
                "destination": destination,
                "adults": party_size,
            }
            publication_icom_queries.append(
                {**identity, "query_identity_sha256": _sha256(identity)}
            )
    graph: dict[str, object] = {
        "schema_version": "tripchord-formal-job-graph-v1",
        "terminal_job_id": job_id,
        "request_sha256": request_digest,
        "adults": party_size,
        "pairs": pairs,
        "ordered_pair_ids_sha256": _sha256([item["date_pair_id"] for item in pairs]),
        "query_task_membership_sha256": _sha256(
            [
                {
                    "date_pair_id": item["date_pair_id"],
                    "query_task_ids": item["query_task_ids"],
                }
                for item in pairs
            ]
        ),
        "publication_query_task_membership_sha256": _sha256(
            [
                {
                    "date_pair_id": item["date_pair_id"],
                    "query_task_ids": item["publication_query_task_ids"],
                }
                for item in pairs
            ]
        ),
        "icom_queries": icom_queries,
        "icom_query_membership_sha256": _sha256(icom_queries),
        "publication_icom_queries": publication_icom_queries,
        "publication_icom_query_membership_sha256": _sha256(
            publication_icom_queries
        ),
    }
    graph["job_graph_sha256"] = _sha256(graph)
    return graph


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


def formal_source_trust_root(path: Path | None = None) -> Path:
    configured = os.environ.get(_TRUST_ROOT_ENV)
    if path is None:
        if not configured:
            raise RuntimeError(
                f"{_TRUST_ROOT_ENV} must be explicitly configured"
            )
        path = Path(configured)
    if not path.is_absolute():
        raise RuntimeError("formal source trust root must be an absolute path")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(_REPO_ROOT.resolve())
    except ValueError:
        pass
    else:
        raise RuntimeError("formal source trust root must be outside the repository")
    return resolved


def _protected_directory(path: Path, label: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()
        ):
            raise RuntimeError(f"{label} must be an owner-only mode 0700 directory")
    finally:
        os.close(descriptor)


def _current_generation(root: Path | None = None) -> tuple[Path, str]:
    trust_root = formal_source_trust_root(root)
    _protected_directory(trust_root, "formal source trust root")
    generation = read_owner_only_text(
        trust_root / "current", "formal source current generation", minimum_length=1
    )
    if _GENERATION_PATTERN.fullmatch(generation) is None:
        raise RuntimeError("formal source current generation is invalid")
    generation_root = trust_root / generation
    _protected_directory(generation_root, "formal source generation root")
    return generation_root, generation


def _read_verification_anchor(
    generation_root: Path,
    generation: str,
) -> dict[str, object]:
    raw = _protected_regular_file(
        generation_root / "public-anchor.json",
        "formal source public verification anchor",
    )
    if raw is None:  # pragma: no cover - missing is not allowed
        raise RuntimeError("formal source public verification anchor is unavailable")
    try:
        anchor = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("formal source public verification anchor is corrupt") from exc
    fields = {
        "schema_version",
        "anchor_version",
        "generation",
        "authority_key_id",
        "public_key_der_base64",
        "created_at",
    }
    if not isinstance(anchor, dict) or set(anchor) != fields:
        raise RuntimeError("formal source public verification anchor shape is invalid")
    if (
        anchor["schema_version"] != "tripchord-formal-source-anchor-v2"
        or anchor["generation"] != generation
    ):
        raise RuntimeError("formal source public verification anchor identity is invalid")
    _nonempty_string(anchor["anchor_version"], "formal source anchor version")
    _require_aware_time(anchor["created_at"], "formal source anchor created_at")
    try:
        public_der = base64.b64decode(
            _nonempty_string(
                anchor["public_key_der_base64"], "formal source public key"
            ),
            validate=True,
        )
        public_key = serialization.load_der_public_key(public_der)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("formal source public verification anchor is invalid") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise RuntimeError("formal source public verification anchor is not Ed25519")
    key_id = hashlib.sha256(public_der).hexdigest()
    if anchor["authority_key_id"] != key_id:
        raise RuntimeError("formal source public verification anchor key id is invalid")
    return {**anchor, "public_key_der": public_der, "public_key": public_key}


def _load_verification_anchor(
    root: Path | None = None,
    *,
    anchor_version: object | None = None,
    authority_key_id: object | None = None,
) -> dict[str, object]:
    """Load the exact persistent public anchor named by signed evidence.

    Rotation changes the current *signing* generation but deliberately retains
    prior public anchors for offline verification.  A caller cannot install an
    arbitrary anchor: every candidate must be an owner-only generation under
    the externally configured production trust root and must validate its own
    generation/key identity before it can be selected.
    """

    trust_root = formal_source_trust_root(root)
    if anchor_version is None and authority_key_id is None:
        generation_root, generation = _current_generation(trust_root)
        return _read_verification_anchor(generation_root, generation)
    expected_version = _nonempty_string(
        anchor_version, "formal source anchor version"
    )
    expected_key = _require_sha256(
        authority_key_id, "formal source authority key id"
    )
    _protected_directory(trust_root, "formal source trust root")
    matches: list[dict[str, object]] = []
    for candidate in sorted(trust_root.iterdir(), key=lambda item: item.name):
        if _GENERATION_PATTERN.fullmatch(candidate.name) is None:
            continue
        try:
            _protected_directory(candidate, "formal source generation root")
            anchor = _read_verification_anchor(candidate, candidate.name)
        except RuntimeError:
            continue
        if (
            anchor["anchor_version"] == expected_version
            and anchor["authority_key_id"] == expected_key
        ):
            matches.append(anchor)
    if len(matches) != 1:
        raise RuntimeError("formal source verification anchor is unavailable")
    return matches[0]


def _anchor_version() -> str:
    return str(_load_verification_anchor()["anchor_version"])


def _authority_key_id() -> str:
    return str(_load_verification_anchor()["authority_key_id"])


def _sign(private_key: Ed25519PrivateKey, value: object) -> str:
    return base64.b64encode(private_key.sign(_canonical_bytes(value))).decode("ascii")


def _verify_signature(
    value: object,
    signature: object,
    label: str,
    *,
    anchor_version: object | None = None,
    authority_key_id: object | None = None,
) -> None:
    if not isinstance(signature, str):
        raise ValueError(f"{label} has no authority signature")
    try:
        raw = base64.b64decode(signature, validate=True)
        if isinstance(value, Mapping):
            anchor_version = value.get("anchor_version", anchor_version)
            authority_key_id = value.get("authority_key_id", authority_key_id)
        public_key = _load_verification_anchor(
            anchor_version=anchor_version,
            authority_key_id=authority_key_id,
        )["public_key"]
        if not isinstance(public_key, Ed25519PublicKey):  # pragma: no cover
            raise ValueError("formal source anchor is not Ed25519")
        public_key.verify(raw, _canonical_bytes(value))
    except (RuntimeError, ValueError, InvalidSignature) as exc:
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
        "job_graph_sha256": challenge["job_graph"]["job_graph_sha256"],
        "issued_at": challenge["issued_at"],
        "expires_at": challenge["expires_at"],
    }


def _execution_capability_proof_payload(
    capability: Mapping[str, object],
) -> dict[str, object]:
    return {
        "purpose": "tripchord-formal-live-source-execution-capability-proof-v1",
        **{key: value for key, value in capability.items() if key != "signature"},
    }


def _validate_execution_capability(
    capability: object,
    *,
    challenge: Mapping[str, object],
) -> dict[str, object]:
    fields = {
        "schema_version",
        "anchor_version",
        "authority_key_id",
        "capability_id",
        "challenge_id",
        "nonce_digest",
        "run_id",
        "tested_commit_sha",
        "terminal_job_id",
        "job_graph_sha256",
        "request_sha256",
        "attempt_digest",
        "issued_at",
        "expires_at",
        "signature",
    }
    if not isinstance(capability, dict) or set(capability) != fields:
        raise ValueError("formal execution capability has an invalid shape")
    if capability["schema_version"] != _EXECUTION_CAPABILITY_SCHEMA_VERSION:
        raise ValueError("formal execution capability schema is invalid")
    try:
        UUID(str(capability["capability_id"]))
    except ValueError as exc:
        raise ValueError("formal execution capability identity is invalid") from exc
    graph = _validate_job_graph(challenge["job_graph"])
    expected = {
        "anchor_version": challenge["anchor_version"],
        "authority_key_id": challenge["authority_key_id"],
        "challenge_id": challenge["challenge_id"],
        "nonce_digest": challenge["nonce_digest"],
        "run_id": challenge["run_id"],
        "tested_commit_sha": challenge["tested_commit_sha"],
        "terminal_job_id": graph["terminal_job_id"],
        "job_graph_sha256": graph["job_graph_sha256"],
        "request_sha256": challenge["request_sha256"],
        "issued_at": challenge["issued_at"],
        "expires_at": challenge["expires_at"],
    }
    if any(capability[key] != value for key, value in expected.items()):
        raise ValueError("formal execution capability is bound to a foreign job")
    _require_sha256(
        capability["attempt_digest"],
        "formal execution capability attempt digest",
    )
    _verify_signature(
        _execution_capability_proof_payload(capability),
        capability["signature"],
        "formal execution capability",
    )
    return dict(capability)


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
        "terminal_job_graph_sha256": receipt["job_member_summary"][
            "terminal_job_graph_sha256"
        ],
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


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_owner_write(path: Path, data: bytes, label: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
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
            raise RuntimeError(f"{label} was not created owner-only")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"{label} write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if after.st_ino != info.st_ino or after.st_nlink != 1:
            raise RuntimeError(f"{label} inode changed during creation")
    finally:
        os.close(descriptor)
    _protected_regular_file(path, label)


def _atomic_owner_replace(path: Path, data: bytes, label: str) -> None:
    _protected_regular_file(path, label)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        _exclusive_owner_write(temporary, data, f"{label} temporary")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        _protected_regular_file(path, label)
    finally:
        temporary.unlink(missing_ok=True)


def _write_generation(
    root: Path,
    *,
    generation_number: int,
    now: datetime,
) -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_id = hashlib.sha256(public_der).hexdigest()
    generation = f"generation-{generation_number}-{key_id[:12]}"
    generation_root = root / generation
    os.mkdir(generation_root, 0o700)
    _protected_directory(generation_root, "formal source generation root")
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    anchor = {
        "schema_version": "tripchord-formal-source-anchor-v2",
        "anchor_version": f"tripchord-formal-source-anchor-v2:g{generation_number}",
        "generation": generation,
        "authority_key_id": key_id,
        "public_key_der_base64": base64.b64encode(public_der).decode("ascii"),
        "created_at": now.astimezone(UTC).isoformat(),
    }
    _exclusive_owner_write(
        generation_root / "authority-private.pem",
        private_pem,
        "formal source authority private key",
    )
    _exclusive_owner_write(
        generation_root / "public-anchor.json",
        _canonical_bytes(anchor),
        "formal source public verification anchor",
    )
    _fsync_directory(generation_root)
    return generation, key_id


def provision_formal_source_trust_root(
    root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Create a clean production trust root without any embedded fallback key."""

    target = root.absolute()
    if target.exists():
        raise RuntimeError("formal source trust root already exists")
    parent = target.parent
    if not parent.exists():
        os.mkdir(parent, 0o700)
    _protected_directory(parent, "formal source trust-root parent")
    os.mkdir(target, 0o700)
    _protected_directory(target, "formal source trust root")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    generation, key_id = _write_generation(
        target,
        generation_number=1,
        now=timestamp,
    )
    _exclusive_owner_write(
        target / "control-token",
        (base64.urlsafe_b64encode(os.urandom(64)).decode("ascii") + "\n").encode(),
        "formal source control token",
    )
    _exclusive_owner_write(
        target / "ledger.json",
        b"{}",
        "formal source challenge ledger",
    )
    _exclusive_owner_write(
        target / "current",
        (generation + "\n").encode(),
        "formal source current generation",
    )
    _fsync_directory(target)
    return {
        "trust_root": str(target),
        "generation": generation,
        "authority_key_id": key_id,
    }


def verify_formal_source_trust_root(root: Path) -> dict[str, object]:
    target = root.absolute()
    generation_root, generation = _current_generation(target)
    anchor = _load_verification_anchor(target)
    private_raw = _protected_regular_file(
        generation_root / "authority-private.pem",
        "formal source authority private key",
    )
    if private_raw is None:  # pragma: no cover
        raise RuntimeError("formal source authority private key is unavailable")
    private_key = serialization.load_pem_private_key(private_raw, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise RuntimeError("formal source authority private key is not Ed25519")
    if private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ) != anchor["public_key_der"]:
        raise RuntimeError("formal source private key does not match current public anchor")
    read_owner_only_text(
        target / "control-token", "formal source control token", minimum_length=64
    )
    ledger = _protected_regular_file(
        target / "ledger.json", "formal source challenge ledger"
    )
    if ledger is None:  # pragma: no cover
        raise RuntimeError("formal source challenge ledger is unavailable")
    try:
        parsed = json.loads(ledger)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("formal source challenge ledger is corrupt") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("formal source challenge ledger shape is invalid")
    return {
        "trust_root": str(target),
        "generation": generation,
        "anchor_version": anchor["anchor_version"],
        "authority_key_id": anchor["authority_key_id"],
        "ledger_entry_count": len(parsed),
    }


def rotate_formal_source_trust_root(
    root: Path,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    target = root.absolute()
    current_root, current = _current_generation(target)
    del current_root
    ledger_raw = _protected_regular_file(
        target / "ledger.json", "formal source challenge ledger"
    )
    if ledger_raw is None:  # pragma: no cover
        raise RuntimeError("formal source challenge ledger is unavailable")
    try:
        ledger = json.loads(ledger_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("formal source challenge ledger is corrupt") from exc
    if not isinstance(ledger, dict) or any(
        isinstance(row, dict) and row.get("state") == "issued"
        for row in ledger.values()
    ):
        raise RuntimeError("formal source key rotation requires no active challenge")
    match = _GENERATION_PATTERN.fullmatch(current)
    if match is None:  # pragma: no cover - current generation already validated
        raise RuntimeError("formal source current generation is invalid")
    generation, key_id = _write_generation(
        target,
        generation_number=int(match.group(1)) + 1,
        now=(now or datetime.now(UTC)).astimezone(UTC),
    )
    _atomic_owner_replace(
        target / "current",
        (generation + "\n").encode(),
        "formal source current generation",
    )
    return {
        "trust_root": str(target),
        "previous_generation": current,
        "generation": generation,
        "authority_key_id": key_id,
    }


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


def _validate_companion_binding(value: object) -> dict[str, object]:
    fields = {
        "companion_id",
        "providers",
        "authorized_scope_keys",
        "adapter_version",
        "contract_version",
        "runtime_instance_id",
        "build_identity",
        "identity_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("formal Companion binding has an invalid shape")
    identity: dict[str, object] = {
        "companion_id": _nonempty_string(
            value["companion_id"], "formal Companion id"
        ),
        "providers": _exact_string_list(
            value["providers"], "formal Companion providers", nonempty=True
        ),
        "authorized_scope_keys": _exact_string_list(
            value["authorized_scope_keys"],
            "formal Companion authorized scopes",
            nonempty=True,
        ),
        "adapter_version": _nonempty_string(
            value["adapter_version"], "formal Companion adapter version"
        ),
        "contract_version": _nonempty_string(
            value["contract_version"], "formal Companion contract version"
        ),
        "runtime_instance_id": _nonempty_string(
            value["runtime_instance_id"], "formal Companion runtime instance"
        ),
    }
    providers = identity["providers"]
    scopes = identity["authorized_scope_keys"]
    if (
        providers != sorted(providers)
        or set(providers) != {"ctrip", "qunar", "tongcheng"}
        or scopes != sorted(scopes)
    ):
        raise ValueError("formal Companion provider/scope identity is not canonical")
    build = value["build_identity"]
    build_fields = {
        "protocol_version",
        "manifest_version",
        "build_sha256",
        "content_runtime_version",
    }
    if not isinstance(build, dict) or set(build) != build_fields:
        raise ValueError("formal Companion build identity has an invalid shape")
    checked_build: dict[str, object] = {
        "protocol_version": _nonempty_string(
            build["protocol_version"], "formal Companion protocol version"
        ),
        "manifest_version": _nonempty_string(
            build["manifest_version"], "formal Companion manifest version"
        ),
        "build_sha256": _require_sha256(
            build["build_sha256"], "formal Companion build sha256"
        ),
        "content_runtime_version": _nonempty_string(
            build["content_runtime_version"],
            "formal Companion content runtime version",
        ),
    }
    identity["build_identity"] = checked_build
    digest = _require_sha256(
        value["identity_sha256"], "formal Companion identity sha256"
    )
    if digest != _sha256(identity):
        raise ValueError("formal Companion identity digest differs")
    return {**identity, "identity_sha256": digest}


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


def _validate_job_graph(value: object) -> dict[str, object]:
    fields = {
        "schema_version",
        "terminal_job_id",
        "request_sha256",
        "adults",
        "pairs",
        "ordered_pair_ids_sha256",
        "query_task_membership_sha256",
        "publication_query_task_membership_sha256",
        "icom_queries",
        "icom_query_membership_sha256",
        "publication_icom_queries",
        "publication_icom_query_membership_sha256",
        "job_graph_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("formal job graph has an invalid shape")
    expected = formal_job_graph_for_frozen_v4(
        terminal_job_id=_nonempty_string(
            value["terminal_job_id"], "formal job terminal_job_id"
        ),
        request_sha256=_require_sha256(
            value["request_sha256"], "formal job request_sha256"
        ),
        adults=_exact_int(value["adults"], "formal job adults", minimum=1),
    )
    if value != expected:
        raise ValueError(
            "formal job graph differs from the canonical ordered pair/query/checkpoint graph"
        )
    return dict(value)


def _validate_terminal_job_contract(
    value: object,
    *,
    job_graph: Mapping[str, object],
    pair_checkpoint_binding: object,
) -> dict[str, object]:
    fields = {
        "id",
        "state",
        "request_sha256",
        "checkpoint_sha256",
        "checkpoint_chain_sha256",
        "result_sha256",
        "job_graph_sha256",
        "ordered_pair_ids_sha256",
        "query_task_membership_sha256",
        "publication_query_task_membership_sha256",
        "icom_query_membership_sha256",
        "publication_icom_query_membership_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("formal terminal job has an invalid shape")
    if (
        value["id"] != job_graph["terminal_job_id"]
        or value["state"] != "succeeded"
        or value["request_sha256"] != job_graph["request_sha256"]
    ):
        raise ValueError("formal terminal job replays a foreign job/request/state")
    for field in (
        "job_graph_sha256",
        "ordered_pair_ids_sha256",
        "query_task_membership_sha256",
        "publication_query_task_membership_sha256",
        "icom_query_membership_sha256",
        "publication_icom_query_membership_sha256",
    ):
        _require_sha256(value[field], f"formal terminal job {field}")
        if value[field] != job_graph[field]:
            raise ValueError(
                f"formal terminal job {field} differs from the frozen job graph"
            )
    checkpoints = _exact_string_list(
        value["checkpoint_sha256"],
        "formal terminal checkpoint digests",
        nonempty=True,
    )
    for digest in checkpoints:
        _require_sha256(digest, "formal terminal checkpoint digest")
    if len(checkpoints) != len(job_graph["pairs"]):
        raise ValueError("formal terminal checkpoint count differs from job graph")
    if value["checkpoint_chain_sha256"] != _sha256(checkpoints):
        raise ValueError("formal terminal checkpoint chain digest is invalid")
    _require_sha256(value["result_sha256"], "formal terminal result_sha256")
    if not isinstance(pair_checkpoint_binding, dict):
        raise ValueError("formal pair checkpoint binding is not an exact object")
    bindings = pair_checkpoint_binding.get("bindings")
    if not isinstance(bindings, list) or len(bindings) != len(job_graph["pairs"]):
        raise ValueError("formal pair checkpoint binding count differs from job graph")
    expected_pairs = job_graph["pairs"]
    if not isinstance(expected_pairs, list):  # already guarded by canonical equality
        raise ValueError("formal job pair graph is invalid")
    observed_checkpoint_digests: list[str] = []
    for index, (expected, binding) in enumerate(
        zip(expected_pairs, bindings, strict=True), start=1
    ):
        if not isinstance(expected, dict) or not isinstance(binding, dict):
            raise ValueError("formal pair checkpoint member is not an exact object")
        expected_projection = {
            "sequence": expected["sequence"],
            "date_pair_id": expected["date_pair_id"],
            "departure_date": expected["departure_date"],
            "return_date": expected["return_date"],
            "request_sha256": expected["request_sha256"],
            "query_task_ids": expected["query_task_ids"],
            "query_task_ids_sha256": expected["query_task_ids_sha256"],
        }
        if any(binding.get(key) != item for key, item in expected_projection.items()):
            raise ValueError(
                f"formal pair checkpoint {index} differs from its canonical job member"
            )
        checkpoint_digest = _require_sha256(
            binding.get("checkpoint_sha256"),
            f"formal pair checkpoint {index} digest",
        )
        observed_checkpoint_digests.append(checkpoint_digest)
    if observed_checkpoint_digests != checkpoints:
        raise ValueError("formal terminal checkpoint digests differ from top-level binding")
    return dict(value)


def _job_member_summary(
    job_graph: Mapping[str, object],
    terminal_job: Mapping[str, object],
) -> dict[str, object]:
    pairs = job_graph["pairs"]
    if not isinstance(pairs, list):
        raise ValueError("formal job graph pairs are invalid")
    pair_members = [
        {
            "sequence": item["sequence"],
            "date_pair_id": item["date_pair_id"],
            "query_task_count": len(item["query_task_ids"]),
            "query_task_ids_sha256": item["query_task_ids_sha256"],
            "publication_query_task_count": len(
                item["publication_query_task_ids"]
            ),
            "publication_query_task_ids_sha256": item[
                "publication_query_task_ids_sha256"
            ],
            "checkpoint_identity_sha256": item["checkpoint_identity_sha256"],
            "checkpoint_sha256": terminal_job["checkpoint_sha256"][index],
        }
        for index, item in enumerate(pairs)
        if isinstance(item, dict) and isinstance(item.get("query_task_ids"), list)
    ]
    if len(pair_members) != len(pairs):
        raise ValueError("formal job graph pair members are invalid")
    summary: dict[str, object] = {
        "terminal_job_id": job_graph["terminal_job_id"],
        "ordered_pair_ids_sha256": job_graph["ordered_pair_ids_sha256"],
        "pair_members": pair_members,
        "query_task_membership_sha256": job_graph["query_task_membership_sha256"],
        "publication_query_task_membership_sha256": job_graph[
            "publication_query_task_membership_sha256"
        ],
        "icom_query_count": len(job_graph["icom_queries"]),
        "icom_query_membership_sha256": job_graph["icom_query_membership_sha256"],
        "publication_icom_query_count": len(
            job_graph["publication_icom_queries"]
        ),
        "publication_icom_query_membership_sha256": job_graph[
            "publication_icom_query_membership_sha256"
        ],
        "checkpoint_chain_sha256": terminal_job["checkpoint_chain_sha256"],
        "terminal_result_sha256": terminal_job["result_sha256"],
        "job_graph_sha256": job_graph["job_graph_sha256"],
    }
    summary["terminal_job_graph_sha256"] = _sha256(summary)
    return summary


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
        "job_graph",
        "issued_at",
        "expires_at",
        "signature",
    }
    if not isinstance(challenge, dict) or set(challenge) != fields:
        raise ValueError("formal source challenge has an invalid shape")
    if challenge["schema_version"] != _CHALLENGE_SCHEMA_VERSION:
        raise ValueError("formal source challenge schema is invalid")
    try:
        _load_verification_anchor(
            anchor_version=challenge["anchor_version"],
            authority_key_id=challenge["authority_key_id"],
        )
    except (RuntimeError, ValueError) as exc:
        raise ValueError(
            "formal source challenge uses a foreign verification anchor"
        ) from exc
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
    if (
        challenge["candidate_set_sha256"]
        != system_stay_plan_candidate_set().candidate_set_sha256
    ):
        raise ValueError(
            "formal source challenge candidate set differs from the frozen contract"
        )
    job_graph = _validate_job_graph(challenge["job_graph"])
    if job_graph["request_sha256"] != challenge["request_sha256"]:
        raise ValueError("formal source challenge job graph uses a foreign request")
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
        key: challenge[key] for key in _CHALLENGE_CONTEXT_FIELDS - {"job_graph"}
    }
    expected_event_context.update(
        {
            "job_graph_sha256": challenge["job_graph"]["job_graph_sha256"],
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
        anchor_version=challenge["anchor_version"],
        authority_key_id=challenge["authority_key_id"],
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
        or snapshot["anchor_version"] != challenge["anchor_version"]
        or snapshot["authority_key_id"] != challenge["authority_key_id"]
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


def _validate_business_event_details(
    events: Sequence[Mapping[str, object]],
    *,
    job_graph: Mapping[str, object],
    candidate_set_sha256: str,
    require_complete: bool = True,
) -> None:
    """Cross-check transport receipts with their exact business identities."""
    claimed: dict[str, dict[str, object]] = {}
    completed: set[str] = set()
    heartbeat_identity: dict[str, object] | None = None
    icom_by_query: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    claimed_query_members: list[str] = []
    publication_query_members: list[str] = []

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
        _nonempty_string(query["origin"], f"{label} origin")
        _nonempty_string(query["destination"], f"{label} destination")
        for field in ("start_date", "end_date"):
            try:
                datetime.strptime(
                    _nonempty_string(query[field], f"{label} {field}"),
                    "%Y-%m-%d",
                )
            except ValueError as exc:
                raise ValueError(f"{label} {field} is invalid") from exc
        if str(query["end_date"]) < str(query["start_date"]):
            raise ValueError(f"{label} date range is invalid")
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
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
            ):
                raise ValueError(f"{label} search_url is invalid")
        if not isinstance(query["options"], dict):
            raise ValueError(f"{label} options are invalid")
        options = query["options"]
        base_option_fields = {
            "__tripchord_allow_recent_quote_reuse",
            "gateway_destination",
            "stay_area_search_profile",
            "stay_plan_candidate_set",
        }
        lodging_option_fields = {
            "segment",
            "expected_lodging_place_key",
            "expected_package_area",
        }
        segment = options.get("segment")
        expected_option_fields = (
            base_option_fields | lodging_option_fields
            if segment is not None
            else base_option_fields
        )
        if set(options) != expected_option_fields:
            raise ValueError(f"{label} options have an invalid shape")
        reuse = options["__tripchord_allow_recent_quote_reuse"]
        if type(reuse) is not bool:
            raise ValueError(f"{label} reuse flag is not an exact bool")
        try:
            profile = StayAreaSearchProfile.model_validate(
                options["stay_area_search_profile"]
            )
            candidate_set = StayPlanCandidateSet.model_validate(
                options["stay_plan_candidate_set"]
            )
        except ValueError as exc:
            raise ValueError(f"{label} nested frozen query contract is invalid") from exc
        if (
            _canonical_bytes(profile.model_dump(mode="json"))
            != _canonical_bytes(options["stay_area_search_profile"])
            or _canonical_bytes(candidate_set.model_dump(mode="json"))
            != _canonical_bytes(options["stay_plan_candidate_set"])
        ):
            raise ValueError(f"{label} nested frozen query types are not exact")
        expected_profile = system_stay_area_search_profile("马累")
        expected_candidate_set = system_stay_plan_candidate_set()
        if expected_profile is None or (
            _canonical_bytes(profile.model_dump(mode="json"))
            != _canonical_bytes(expected_profile.model_dump(mode="json"))
            or _canonical_bytes(candidate_set.model_dump(mode="json"))
            != _canonical_bytes(expected_candidate_set.model_dump(mode="json"))
        ):
            raise ValueError(f"{label} nested query differs from the frozen contract")
        if (
            candidate_set.candidate_set_sha256 != candidate_set_sha256
            or options["gateway_destination"] != profile.gateway_destination
            or candidate_set.gateway_destination != profile.gateway_destination
        ):
            raise ValueError(f"{label} nested frozen query identity is foreign")
        if query["origin"] != "杭州" or query["origin_code"] != "HGH":
            raise ValueError(f"{label} origin identity is not frozen")
        if segment is None:
            if (
                query["destination"] != profile.gateway_destination
                or query["destination_code"] != "MLE"
            ):
                raise ValueError(f"{label} flight destination is not frozen")
        else:
            segment_identity = {
                "full": ("maafushi", "destination_island", "Maafushi"),
                "middle": ("maafushi", "destination_island", "Maafushi"),
                "first": ("hulhumale", "airport_island", "Hulhumalé"),
                "last": ("hulhumale", "airport_island", "Hulhumalé"),
                "hulhumale-full": (
                    "hulhumale",
                    "airport_island",
                    "Hulhumalé",
                ),
            }.get(segment)
            if segment_identity is None or (
                options["expected_lodging_place_key"],
                options["expected_package_area"],
                query["destination"],
            ) != segment_identity or query["destination_code"] is not None:
                raise ValueError(f"{label} lodging segment identity is not frozen")
        if (
            query["rooms"] != 1
            or query["currency"] != "CNY"
        ):
            raise ValueError(f"{label} scalar query contract is not frozen")
        return query

    def formal_query_contract(
        value: object,
        *,
        task_id: str,
        provider: object,
        kind: object,
        query: Mapping[str, object],
        label: str,
    ) -> dict[str, object]:
        fields = {
            "terminal_job_id",
            "pair_id",
            "task_id",
            "query_task_id",
            "provider",
            "kind",
            "query_kind",
            "direction",
            "start_date",
            "end_date",
            "query_sha256",
            "query_identity",
            "execution_phase",
        }
        formal = exact_object(value, fields, label)
        if (
            formal["terminal_job_id"] != job_graph["terminal_job_id"]
            or formal["task_id"] != task_id
            or formal["provider"] != provider
            or formal["kind"] != kind
            or formal["start_date"] != query["start_date"]
            or formal["end_date"] != query["end_date"]
            or formal["query_sha256"] != _sha256(query)
        ):
            raise ValueError(f"{label} is cross-task/provider/query")
        if formal["execution_phase"] not in {
            "checkpoint_exploration",
            "publication_refresh",
        }:
            raise ValueError(f"{label} execution phase is invalid")
        reuse = query["options"]["__tripchord_allow_recent_quote_reuse"]
        expected_phase = (
            "checkpoint_exploration" if reuse is True else "publication_refresh"
        )
        if formal["execution_phase"] != expected_phase:
            raise ValueError(f"{label} execution phase differs from query reuse policy")
        identity = {
            key: item for key, item in formal.items() if key != "query_identity"
        }
        if formal["query_identity"] != _sha256(identity):
            raise ValueError(f"{label} query identity digest is invalid")
        expected_member = _resolve_formal_browser_job_member(
            job_graph,
            provider=provider,
            kind=kind,
            query=query,
        )
        for field in (
            "pair_id",
            "query_task_id",
            "query_kind",
            "direction",
            "start_date",
            "end_date",
            "execution_phase",
        ):
            if formal[field] != expected_member[field]:
                raise ValueError(f"{label} differs from the exact signed job member")
        pairs = job_graph["pairs"]
        if not isinstance(pairs, list):
            raise ValueError("formal job graph pair list is invalid")
        membership_field = (
            "query_task_ids"
            if formal["execution_phase"] == "checkpoint_exploration"
            else "publication_query_task_ids"
        )
        owners = [
            pair
            for pair in pairs
            if isinstance(pair, dict)
            and pair["date_pair_id"] == formal["pair_id"]
            and formal["query_task_id"] in pair[membership_field]
        ]
        if len(owners) != 1:
            raise ValueError(f"{label} is outside/cross-pair in the formal job graph")
        return formal

    for event in events:
        kind = event["kind"]
        details = event["details"]
        if not isinstance(details, dict):
            raise ValueError("formal event details are not an object")
        if kind == "browser_heartbeat":
            if set(details) != {
                "request",
                "heartbeat",
                "companion_identity_sha256",
            }:
                raise ValueError("formal heartbeat details have an invalid shape")
            request = exact_object(
                details["request"],
                {
                    "companion_id",
                    "providers",
                    "authorized_scope_keys",
                    "adapter_version",
                    "contract_version",
                    "build_identity",
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
                "build_identity",
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
            identity["identity_sha256"] = details["companion_identity_sha256"]
            checked_identity = _validate_companion_binding(identity)
            if checked_identity["identity_sha256"] != details[
                "companion_identity_sha256"
            ]:
                raise ValueError("formal heartbeat Companion digest differs")
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
                        "formal_query",
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
                checked_query = query_contract(
                    lease["query"], "formal claim lease query"
                )
                _validate_formal_browser_search_url(
                    checked_query,
                    provider=lease["provider"],
                    kind=lease["kind"],
                )
                formal_query = formal_query_contract(
                    lease["formal_query"],
                    task_id=task_id,
                    provider=lease["provider"],
                    kind=lease["kind"],
                    query=checked_query,
                    label="formal claim lease formal_query",
                )
                query_member = str(formal_query["query_task_id"])
                member_list = (
                    publication_query_members
                    if formal_query["execution_phase"] == "publication_refresh"
                    else claimed_query_members
                )
                if query_member in member_list:
                    raise ValueError("formal claim reuses a query member in one phase")
                member_list.append(query_member)
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
            if set(details) != {
                "task_id",
                "completion",
                "snapshot",
                "formal_query",
                "result_sha256",
            }:
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
            checked_snapshot_query = query_contract(
                snapshot["query"], "formal completion snapshot.query"
            )
            _validate_formal_browser_search_url(
                checked_snapshot_query,
                provider=snapshot["provider"],
                kind=snapshot["kind"],
            )
            completion_formal_query = formal_query_contract(
                details["formal_query"],
                task_id=task_id,
                provider=snapshot["provider"],
                kind=snapshot["kind"],
                query=checked_snapshot_query,
                label="formal completion formal_query",
            )
            if completion_formal_query != lease["formal_query"]:
                raise ValueError("formal completion query differs from independently checked claim")
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
                "source_task_id",
                "query_task_id",
                "call_id",
                "call_identity_sha256",
                "query_identity",
                "query_identity_sha256",
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
                {
                    "pair_id",
                    "source_task_id",
                    "query_task_id",
                    "direction",
                    "travel_date",
                    "departure_date",
                    "return_date",
                    "origin",
                    "destination",
                    "adults",
                },
                "formal iCom query identity",
            )
            expected_queries = [
                item
                for item in (
                    *job_graph["icom_queries"],
                    *job_graph["publication_icom_queries"],
                )
                if isinstance(item, dict) and item["query_task_id"] == task_id
            ]
            if len(expected_queries) != 1:
                raise ValueError("formal iCom query task is outside the signed job graph")
            expected_query = expected_queries[0]
            expected_identity = {
                key: expected_query[key]
                for key in (
                    "pair_id",
                    "source_task_id",
                    "query_task_id",
                    "direction",
                    "travel_date",
                    "departure_date",
                    "return_date",
                    "origin",
                    "destination",
                    "adults",
                )
            }
            if (
                query_identity != expected_identity
                or details["source_task_id"] != expected_query["source_task_id"]
                or details["query_identity_sha256"]
                != expected_query["query_identity_sha256"]
            ):
                raise ValueError("formal iCom query identity is cross-pair/date/direction")
            call_identity = {
                "query_task_id": task_id,
                "query_identity_sha256": details["query_identity_sha256"],
                "method": "GET",
                "path": event["path"],
            }
            expected_call_id = f"{task_id}|{event['path']}"
            if (
                details["call_id"] != expected_call_id
                or event["subject_ids"] != [expected_call_id]
                or details["call_identity_sha256"] != _sha256(call_identity)
            ):
                raise ValueError("formal iCom call identity/path is not exact")
            if not isinstance(url, str) or not isinstance(query, dict):
                raise ValueError("formal iCom URL/query identity is invalid")
            parsed = urlsplit(url)
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            if len(pairs) != len(dict(pairs)):
                raise ValueError("formal iCom URL carries duplicate query keys")
            parsed_query = dict(pairs)
            if (
                parsed.scheme != "https"
                or parsed.hostname != _ICOM_PUBLIC_HOST
                or parsed.port is not None
                or parsed.username is not None
                or parsed.password is not None
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
            key = (task_id, str(details["query_identity_sha256"]))
            icom_by_query.setdefault(key, []).append(event)
    if not completed.issubset(claimed):
        raise ValueError("formal task completion precedes its exact claim")
    if require_complete and (not claimed or completed != set(claimed)):
        raise ValueError("formal task claim/complete membership is not exact")
    expected_members = [
        task_id
        for pair in job_graph["pairs"]
        if isinstance(pair, dict)
        for task_id in pair["query_task_ids"]
    ]
    if (
        len(claimed_query_members) != len(set(claimed_query_members))
        or not set(claimed_query_members).issubset(expected_members)
    ):
        raise ValueError("formal Browser claim membership is duplicate or foreign")
    if require_complete and set(claimed_query_members) != set(expected_members):
        raise ValueError(
            "formal Browser claim order/membership differs from the canonical job graph"
        )
    expected_publication_members = [
        task_id
        for pair in job_graph["pairs"]
        if isinstance(pair, dict)
        for task_id in pair["publication_query_task_ids"]
    ]
    if (
        len(publication_query_members) != len(set(publication_query_members))
        or not set(publication_query_members).issubset(expected_publication_members)
    ):
        raise ValueError("formal publication claim membership is duplicate or foreign")
    if require_complete and set(publication_query_members) != set(
        expected_publication_members
    ):
        raise ValueError(
            "formal publication claim membership differs from the signed job graph"
        )
    if require_complete and not icom_by_query:
        raise ValueError("formal run contains no iCom query graph")
    expected_icom_keys = [
        (str(item["query_task_id"]), str(item["query_identity_sha256"]))
        for item in (
            *job_graph["icom_queries"],
            *job_graph["publication_icom_queries"],
        )
        if isinstance(item, dict)
    ]
    observed_icom_keys = list(icom_by_query)
    if observed_icom_keys != expected_icom_keys[: len(observed_icom_keys)] or (
        require_complete and observed_icom_keys != expected_icom_keys
    ):
        raise ValueError("formal iCom query group order/membership differs from job graph")
    for (task_id, _query_digest), query_events in icom_by_query.items():
        paths = [str(item["path"]) for item in query_events]
        sequences = [int(item["sequence"]) for item in query_events]
        if (
            paths != list(_ICOM_PATH_ORDER[: len(paths)])
            or (require_complete and paths != list(_ICOM_PATH_ORDER))
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
    graph = _validate_job_graph(checked_challenge["job_graph"])
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
        or binding["anchor_version"] != checked_challenge["anchor_version"]
        or binding["authority_key_id"] != checked_challenge["authority_key_id"]
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
    _validate_business_event_details(
        checked_events,
        job_graph=graph,
        candidate_set_sha256=str(checked_challenge["candidate_set_sha256"]),
    )
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
        "job_member_summary",
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
        "anchor_version": _anchor_version(),
        "authority_key_id": _authority_key_id(),
        "challenge_id": checked_challenge["challenge_id"],
        "nonce_digest": checked_challenge["nonce_digest"],
        "binding_digest": binding["binding_digest"],
        "run_id": checked_challenge["run_id"],
        "tested_commit_sha": checked_challenge["tested_commit_sha"],
        "runtime_identity": checked_challenge["runtime_identity"],
        "pre_event_count": pre,
        "post_event_count": post,
        "delta_digest": _sha256(receipts),
        "job_member_summary": authority_receipt.get("job_member_summary"),
        "issued_at": checked_challenge["issued_at"],
        "verified_at": authority_receipt.get("verified_at"),
    }
    if _signed_payload(authority_receipt) != expected_receipt:
        raise ValueError("formal source authority receipt fields are not bound")
    job_member_summary = authority_receipt["job_member_summary"]
    if not isinstance(job_member_summary, dict):
        raise ValueError("formal source authority receipt job summary is invalid")
    summary_fields = {
        "terminal_job_id",
        "ordered_pair_ids_sha256",
        "pair_members",
        "query_task_membership_sha256",
        "publication_query_task_membership_sha256",
        "icom_query_count",
        "icom_query_membership_sha256",
        "publication_icom_query_count",
        "publication_icom_query_membership_sha256",
        "checkpoint_chain_sha256",
        "terminal_result_sha256",
        "job_graph_sha256",
        "terminal_job_graph_sha256",
    }
    if set(job_member_summary) != summary_fields:
        raise ValueError("formal source authority receipt job summary shape is invalid")
    for key in (
        "terminal_job_id",
        "ordered_pair_ids_sha256",
        "query_task_membership_sha256",
        "publication_query_task_membership_sha256",
        "icom_query_membership_sha256",
        "publication_icom_query_membership_sha256",
        "job_graph_sha256",
    ):
        if job_member_summary[key] != graph[key]:
            raise ValueError("formal source authority receipt job graph is cross-swapped")
    if job_member_summary["icom_query_count"] != len(graph["icom_queries"]):
        raise ValueError("formal source authority receipt iCom membership is incomplete")
    if job_member_summary["publication_icom_query_count"] != len(
        graph["publication_icom_queries"]
    ):
        raise ValueError(
            "formal source authority receipt publication iCom membership is incomplete"
        )
    pair_members = job_member_summary["pair_members"]
    graph_pairs = graph["pairs"]
    if not isinstance(pair_members, list) or not isinstance(graph_pairs, list):
        raise ValueError("formal source authority receipt pair summary is invalid")
    if len(pair_members) != len(graph_pairs):
        raise ValueError("formal source authority receipt pair summary is incomplete")
    for index, (member, graph_pair) in enumerate(
        zip(pair_members, graph_pairs, strict=True), start=1
    ):
        if not isinstance(member, dict) or not isinstance(graph_pair, dict):
            raise ValueError("formal source authority receipt pair member is invalid")
        expected_member_fields = {
            "sequence",
            "date_pair_id",
            "query_task_count",
            "query_task_ids_sha256",
            "publication_query_task_count",
            "publication_query_task_ids_sha256",
            "checkpoint_identity_sha256",
            "checkpoint_sha256",
        }
        if set(member) != expected_member_fields:
            raise ValueError("formal source authority receipt pair member shape is invalid")
        if (
            member["sequence"] != graph_pair["sequence"]
            or member["date_pair_id"] != graph_pair["date_pair_id"]
            or member["query_task_count"] != len(graph_pair["query_task_ids"])
            or member["query_task_ids_sha256"]
            != graph_pair["query_task_ids_sha256"]
            or member["publication_query_task_count"]
            != len(graph_pair["publication_query_task_ids"])
            or member["publication_query_task_ids_sha256"]
            != graph_pair["publication_query_task_ids_sha256"]
            or member["checkpoint_identity_sha256"]
            != graph_pair["checkpoint_identity_sha256"]
        ):
            raise ValueError(
                f"formal source authority receipt pair {index} is cross-swapped"
            )
        _require_sha256(member["checkpoint_sha256"], "formal receipt checkpoint")
    checkpoint_digests = [item["checkpoint_sha256"] for item in pair_members]
    if job_member_summary["checkpoint_chain_sha256"] != _sha256(checkpoint_digests):
        raise ValueError("formal source authority receipt checkpoint chain is invalid")
    _require_sha256(
        job_member_summary["terminal_result_sha256"],
        "formal receipt terminal result",
    )
    expected_terminal_summary_digest = _sha256(
        {
            key: item
            for key, item in job_member_summary.items()
            if key != "terminal_job_graph_sha256"
        }
    )
    if job_member_summary["terminal_job_graph_sha256"] != expected_terminal_summary_digest:
        raise ValueError("formal source authority receipt terminal graph digest is invalid")
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
        if set(expected_context) >= _FINALIZE_CONTEXT_FIELDS:
            terminal = _validate_terminal_job_contract(
                expected_context["terminal_job"],
                job_graph=graph,
                pair_checkpoint_binding=expected_context["pair_checkpoint_binding"],
            )
            if _job_member_summary(graph, terminal) != job_member_summary:
                raise ValueError(
                    "formal source evidence differs from terminal job/checkpoint binding"
                )
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
        "job_member_summary": authority_receipt["job_member_summary"],
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
        "job_member_summary",
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
    required = _CHALLENGE_CONTEXT_FIELDS - {"runtime_identity", "job_graph"}
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
        "anchor_version": _anchor_version(),
        "authority_key_id": _authority_key_id(),
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
    job_member_summary = summary["job_member_summary"]
    if not isinstance(job_member_summary, dict):
        raise ValueError("formal source summary job membership is invalid")
    if "job_graph" in expected_context:
        graph = _validate_job_graph(expected_context["job_graph"])
        if (
            job_member_summary.get("job_graph_sha256") != graph["job_graph_sha256"]
            or job_member_summary.get("terminal_job_id") != graph["terminal_job_id"]
            or job_member_summary.get("query_task_membership_sha256")
            != graph["query_task_membership_sha256"]
            or job_member_summary.get(
                "publication_query_task_membership_sha256"
            )
            != graph["publication_query_task_membership_sha256"]
            or job_member_summary.get("icom_query_membership_sha256")
            != graph["icom_query_membership_sha256"]
            or job_member_summary.get(
                "publication_icom_query_membership_sha256"
            )
            != graph["publication_icom_query_membership_sha256"]
        ):
            raise ValueError("formal source summary job membership is cross-swapped")
        job_graph_digest = graph["job_graph_sha256"]
    else:
        job_graph_digest = _require_sha256(
            job_member_summary.get("job_graph_sha256"),
            "formal source summary job graph",
        )
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
        "job_graph_sha256": job_graph_digest,
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
        "terminal_job_graph_sha256": job_member_summary[
            "terminal_job_graph_sha256"
        ],
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


def validate_formal_execution_capability(
    capability: object,
    challenge: object,
) -> dict[str, object]:
    """Verify one job-bound capability offline against its signed challenge."""

    return _validate_execution_capability(
        capability,
        challenge=_validate_challenge(challenge),
    )


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
        stable = hashlib.sha256(
            bytes(_load_verification_anchor()["public_key_der"])
            + str(_REPO_ROOT).encode()
        ).digest()
        self._install_id = str(UUID(bytes=stable[:16]))
        self._composition_sha256 = _composition_sha256(
            self._install_id, self._composition
        )
        self._now = now or (lambda: datetime.now(UTC))
        self._state_lock = threading.RLock()
        self._bound = False
        self._active_challenge: dict[str, object] | None = None
        self._execution_capability: dict[str, object] | None = None
        self._baseline: dict[str, object] | None = None
        self._events: list[dict[str, object]] = []
        self._chain_sha256 = self._composition_sha256
        self._last_heartbeat: dict[str, object] | None = None
        self._restore_active_state()

    def _active_state_payload(self) -> dict[str, object]:
        if self._active_challenge is None or self._baseline is None:
            raise RuntimeError("formal source active state is incomplete")
        return {
            "challenge": self._active_challenge,
            "execution_capability": self._execution_capability,
            "baseline": self._baseline,
            "events": list(self._events),
            "chain_sha256": self._chain_sha256,
            "last_heartbeat": self._last_heartbeat,
        }

    def _apply_active_state(self, value: object) -> None:
        fields = {
            "challenge",
            "execution_capability",
            "baseline",
            "events",
            "chain_sha256",
            "last_heartbeat",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise RuntimeError("formal source persisted active state is invalid")
        challenge = _validate_challenge(value["challenge"])
        capability = _validate_execution_capability(
            value["execution_capability"],
            challenge=challenge,
        )
        if challenge["tested_commit_sha"] != self._composition["commit_sha"]:
            raise RuntimeError("formal source active state belongs to another build")
        baseline = _validate_snapshot(value["baseline"], challenge)
        if baseline["event_count"] != 0 or baseline["events"] != []:
            raise RuntimeError("formal source persisted baseline is not pre-event")
        events = value["events"]
        if not isinstance(events, list):
            raise RuntimeError("formal source persisted event chain is invalid")
        replay_snapshot = {
            **baseline,
            "event_count": len(events),
            "events": events,
            "chain_sha256": value["chain_sha256"],
            "last_heartbeat": value["last_heartbeat"],
        }
        replay_snapshot["signature"] = _sign(
            self._private_key, _signed_payload(replay_snapshot)
        )
        checked = _validate_snapshot(replay_snapshot, challenge)
        self._active_challenge = challenge
        self._execution_capability = capability
        self._baseline = baseline
        self._events = list(checked["events"])
        self._chain_sha256 = str(checked["chain_sha256"])
        heartbeat = checked["last_heartbeat"]
        self._last_heartbeat = dict(heartbeat) if isinstance(heartbeat, dict) else None

    def _restore_active_state(self) -> None:
        with self._ledger_lock():
            ledger = self._read_ledger()
            active = [row for row in ledger.values() if row.get("state") == "issued"]
            if len(active) > 1:
                raise RuntimeError("formal source ledger has multiple active challenges")
            if not active:
                return
            row = active[0]
            expires = _require_aware_time(
                row["expires_at"], "formal source ledger expires_at"
            )
            if self._utc_now() > expires:
                row["state"] = "expired"
                row["expired_at"] = self._utc_now().isoformat()
                row["terminal_reason"] = "expired_during_cold_start"
                row.pop("active_state", None)
                self._write_ledger(ledger)
                return
            active_state = row.get("active_state")
            challenge = (
                active_state.get("challenge")
                if isinstance(active_state, dict)
                else None
            )
            if (
                not isinstance(challenge, dict)
                or challenge.get("runtime_identity") != self._runtime_identity
            ):
                # Explicit non-continuation model: a PID/started_at/runtime
                # change atomically closes the old attempt.  It may be audited
                # offline, but it can never record/finalize in the new process;
                # the caller must prepare a fresh job/challenge attempt.
                row["state"] = "aborted"
                row["aborted_at"] = self._utc_now().isoformat()
                row["terminal_reason"] = "runtime_restart_requires_new_attempt"
                row.pop("active_state", None)
                self._write_ledger(ledger)
                return
            self._apply_active_state(active_state)

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
        self,
        context: object,
        *,
        lifetime_seconds: int = 3600,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        with self._state_lock:
            return self._issue_challenge_locked(
                context,
                lifetime_seconds=lifetime_seconds,
                idempotency_key=idempotency_key,
            )

    def issue_replay(
        self,
        context: object,
        *,
        lifetime_seconds: int = 3600,
        idempotency_key: object,
    ) -> dict[str, object] | None:
        """Retrieve an already committed issue response without live process state."""

        key = _nonempty_string(
            idempotency_key,
            "formal source issue idempotency key",
        )
        if len(key) > 200:
            raise ValueError("formal source issue idempotency key is invalid")
        request_digest = _sha256(
            {"context": context, "lifetime_seconds": lifetime_seconds}
        )
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            for row in ledger.values():
                if row.get("issue_idempotency_key") != key:
                    continue
                if row.get("issue_request_digest") != request_digest:
                    raise ValueError(
                        "formal source issue idempotency key was used with a different request"
                    )
                result = row.get("issue_result")
                if not isinstance(result, dict):
                    raise RuntimeError("formal source issue result is unavailable")
                return json.loads(_canonical_bytes(result))
        return None

    def _issue_challenge_locked(
        self,
        context: object,
        *,
        lifetime_seconds: int,
        idempotency_key: str | None,
    ) -> dict[str, object]:
        if not self._bound:
            raise RuntimeError("formal source authority is not composition-bound")
        if (
            not isinstance(lifetime_seconds, int)
            or isinstance(lifetime_seconds, bool)
            or not 1 <= lifetime_seconds <= 7200
        ):
            raise ValueError("formal source challenge lifetime is invalid")
        if not isinstance(context, dict) or set(context) != _CHALLENGE_CONTEXT_FIELDS:
            raise ValueError("formal source challenge context has an invalid shape")
        if idempotency_key is None:
            idempotency_key = f"legacy-{_sha256(context)}"
        elif not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValueError("formal source issue idempotency key is invalid")
        runtime = _runtime_identity(context["runtime_identity"])
        if (
            runtime != self._runtime_identity
            or context["tested_commit_sha"] != self._composition["commit_sha"]
        ):
            raise ValueError("formal source challenge runtime/commit is not this API process")
        for key in ("request_sha256", "candidate_set_sha256", "scenario_sha256"):
            _require_sha256(context[key], f"formal source challenge {key}")
        if (
            context["candidate_set_sha256"]
            != system_stay_plan_candidate_set().candidate_set_sha256
        ):
            raise ValueError(
                "formal source challenge candidate set differs from the frozen contract"
            )
        job_graph = _validate_job_graph(context["job_graph"])
        if job_graph["request_sha256"] != context["request_sha256"]:
            raise ValueError("formal source challenge job graph uses a foreign request")
        if not isinstance(context["run_id"], str) or not context["run_id"]:
            raise ValueError("formal source challenge run_id is invalid")
        with self._ledger_lock():
            ledger = self._read_ledger()
            request_digest = _sha256(
                {"context": context, "lifetime_seconds": lifetime_seconds}
            )
            for row in ledger.values():
                if row.get("issue_idempotency_key") != idempotency_key:
                    continue
                if row.get("issue_request_digest") != request_digest:
                    raise ValueError(
                        "formal source issue idempotency key was used with a different request"
                    )
                result = row.get("issue_result")
                if not isinstance(result, dict):
                    raise RuntimeError("formal source issue result is unavailable")
                return json.loads(_canonical_bytes(result))
            if self._active_challenge is not None:
                raise ValueError("formal source authority already has an active challenge")
            now = self._utc_now()
            for row in ledger.values():
                if row.get("state") == "issued" and now > _require_aware_time(
                    row["expires_at"], "formal source ledger expires_at"
                ):
                    row["state"] = "expired"
                    row["expired_at"] = now.isoformat()
                    row["terminal_reason"] = "expired_before_new_challenge"
                    row.pop("active_state", None)
            if any(row.get("state") == "issued" for row in ledger.values()):
                raise ValueError("formal source authority already has an active challenge")
            if any(
                row.get("run_id") == context["run_id"]
                and row.get("state") in {"issued", "consumed"}
                for row in ledger.values()
            ):
                raise ValueError("formal source run_id already has a challenge")
            issued = now
            challenge: dict[str, object] = {
                "schema_version": _CHALLENGE_SCHEMA_VERSION,
                "anchor_version": _anchor_version(),
                "authority_key_id": _authority_key_id(),
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
            capability: dict[str, object] = {
                "schema_version": _EXECUTION_CAPABILITY_SCHEMA_VERSION,
                "anchor_version": challenge["anchor_version"],
                "authority_key_id": challenge["authority_key_id"],
                "capability_id": str(uuid4()),
                "challenge_id": challenge["challenge_id"],
                "nonce_digest": challenge["nonce_digest"],
                "run_id": challenge["run_id"],
                "tested_commit_sha": challenge["tested_commit_sha"],
                "terminal_job_id": job_graph["terminal_job_id"],
                "job_graph_sha256": job_graph["job_graph_sha256"],
                "request_sha256": challenge["request_sha256"],
                "attempt_digest": hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest(),
                "issued_at": challenge["issued_at"],
                "expires_at": challenge["expires_at"],
            }
            capability["signature"] = _sign(
                self._private_key,
                _execution_capability_proof_payload(capability),
            )
            self._active_challenge = challenge
            self._execution_capability = capability
            self._events = []
            self._chain_sha256 = self._composition_sha256
            self._last_heartbeat = None
            self._baseline = self._snapshot_locked()
            result = {
                "challenge": dict(challenge),
                "before": dict(self._baseline),
                "execution_capability": dict(capability),
            }
            ledger[str(challenge["challenge_id"])] = {
                "run_id": context["run_id"],
                "state": "issued",
                "challenge_digest": _sha256(challenge),
                "issued_at": challenge["issued_at"],
                "expires_at": challenge["expires_at"],
                "issue_idempotency_key": idempotency_key,
                "issue_request_digest": request_digest,
                "issue_result": result,
                "active_state": self._active_state_payload(),
            }
            try:
                self._write_ledger(ledger)
            except Exception:
                self._active_challenge = None
                self._execution_capability = None
                self._baseline = None
                self._events = []
                self._chain_sha256 = self._composition_sha256
                self._last_heartbeat = None
                raise
        return result

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
        subject_ids: Sequence[str],
        response_sha256: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if path not in _ICOM_PATHS:
            raise ValueError("unknown formal iCom path")
        self._record(
            kind="icom_public_get",
            method="GET",
            path=path,
            subject_ids=subject_ids,
            details=details or {},
            response_sha256=response_sha256,
        )

    def formal_icom_call(
        self,
        *,
        source_task_id: object,
        query_identity: object,
        path: object,
    ) -> dict[str, object]:
        """Resolve one runtime iCom call against the signed frozen job graph."""

        with self._state_lock:
            if self._active_challenge is None:
                raise ValueError("formal iCom call has no active challenge")
            self._require_execution_capability()
            graph = _validate_job_graph(self._active_challenge["job_graph"])
            source_id = _nonempty_string(source_task_id, "formal iCom source task")
            endpoint = _nonempty_string(path, "formal iCom endpoint")
            if endpoint not in _ICOM_PATHS:
                raise ValueError("formal iCom endpoint is not canonical")
            query = query_identity
            if not isinstance(query, dict) or set(query) != {
                "travel_date",
                "origin",
                "destination",
                "adults",
            }:
                raise ValueError("formal iCom runtime query has an invalid shape")
            _exact_int(query["adults"], "formal iCom runtime adults", minimum=1)
            matches = [
                item
                for item in (
                    *graph["icom_queries"],
                    *graph["publication_icom_queries"],
                )
                if isinstance(item, dict)
                and item["source_task_id"] == source_id
                and item["travel_date"] == query["travel_date"]
                and item["origin"] == query["origin"]
                and item["destination"] == query["destination"]
                and item["adults"] == query["adults"]
            ]
            if len(matches) != 1:
                raise ValueError("formal iCom runtime query is outside the frozen job graph")
            expected = matches[0]
            call_identity = {
                "query_task_id": expected["query_task_id"],
                "query_identity_sha256": expected["query_identity_sha256"],
                "method": "GET",
                "path": endpoint,
            }
            return {
                **expected,
                "call_id": f"{expected['query_task_id']}|{endpoint}",
                "call_identity_sha256": _sha256(call_identity),
            }

    def formal_browser_query(
        self,
        *,
        task_id: object,
        provider: object,
        kind: object,
        query: object,
    ) -> dict[str, object]:
        """Resolve one Browser lease/snapshot independently to a job member."""

        with self._state_lock:
            if self._active_challenge is None:
                raise ValueError("formal Browser query has no active challenge")
            self._require_execution_capability()
            graph = _validate_job_graph(self._active_challenge["job_graph"])
            runtime_task_id = _nonempty_string(task_id, "formal Browser task_id")
            provider_name = _nonempty_string(provider, "formal Browser provider")
            vertical = _nonempty_string(kind, "formal Browser kind")
            if provider_name not in {"ctrip", "qunar", "tongcheng"}:
                raise ValueError("formal Browser provider is outside the frozen graph")
            if vertical not in {"flight", "lodging"}:
                raise ValueError("formal Browser kind is outside the frozen graph")
            if not isinstance(query, dict):
                raise ValueError("formal Browser query is not an exact object")
            adults = _exact_int(
                query.get("adults"), "formal Browser adults", minimum=1
            )
            if adults != graph["adults"]:
                raise ValueError(
                    "formal Browser query is not an exact signed job member"
                )
            start_date = _nonempty_string(
                query.get("start_date"), "formal Browser start_date"
            )
            end_date = _nonempty_string(
                query.get("end_date"), "formal Browser end_date"
            )
            options = query.get("options")
            if not isinstance(options, dict):
                raise ValueError("formal Browser query options are invalid")
            _validate_formal_browser_search_url(
                query,
                provider=provider_name,
                kind=vertical,
            )
            pair_matches = [
                item
                for item in graph["pairs"]
                if isinstance(item, dict)
                and (
                    (vertical == "flight"
                    and item["departure_date"] == start_date
                    and item["return_date"] == end_date)
                    or (
                        vertical == "lodging"
                        and start_date >= item["departure_date"]
                        and end_date <= item["return_date"]
                    )
                )
            ]
            if len(pair_matches) != 1:
                raise ValueError("formal Browser query does not identify one frozen pair")
            pair = pair_matches[0]
            segment = options.get("segment")
            if vertical == "flight":
                if segment is not None:
                    raise ValueError("formal Browser flight carries a lodging segment")
                query_kind = "flight"
                direction = "round_trip"
            else:
                segment_kinds = {
                    "full": "lodging_full_stay",
                    "first": "lodging_first_night",
                    "middle": "lodging_middle_stay",
                    "last": "lodging_last_night",
                    "hulhumale-full": "lodging_hulhumale_full_stay",
                }
                if segment not in segment_kinds:
                    raise ValueError("formal Browser lodging segment is not canonical")
                query_kind = segment_kinds[str(segment)]
                direction = "stay"
                departure = datetime.strptime(
                    str(pair["departure_date"]), "%Y-%m-%d"
                ).date()
                returning = datetime.strptime(
                    str(pair["return_date"]), "%Y-%m-%d"
                ).date()
                exact_dates = {
                    "full": (departure, returning),
                    "first": (departure, departure + timedelta(days=1)),
                    "middle": (
                        departure + timedelta(days=1),
                        returning - timedelta(days=1),
                    ),
                    "last": (returning - timedelta(days=1), returning),
                    "hulhumale-full": (departure, returning),
                }
                expected_start, expected_end = exact_dates[str(segment)]
                if (start_date, end_date) != (
                    expected_start.isoformat(),
                    expected_end.isoformat(),
                ):
                    raise ValueError(
                        "formal Browser query is not an exact signed job member"
                    )
            prefix = f"query:{provider_name}:{query_kind}:"
            member_ids = [
                item
                for item in pair["query_task_ids"]
                if isinstance(item, str) and item.startswith(prefix)
            ]
            if len(member_ids) != 1:
                raise ValueError("formal Browser query does not map to one job member")
            identity: dict[str, object] = {
                "terminal_job_id": graph["terminal_job_id"],
                "pair_id": pair["date_pair_id"],
                "task_id": runtime_task_id,
                "query_task_id": member_ids[0],
                "provider": provider_name,
                "kind": vertical,
                "query_kind": query_kind,
                "direction": direction,
                "start_date": start_date,
                "end_date": end_date,
                "query_sha256": _sha256(query),
                "execution_phase": (
                    "publication_refresh"
                    if options.get("__tripchord_allow_recent_quote_reuse") is False
                    else "checkpoint_exploration"
                ),
            }
            identity["query_identity"] = _sha256(identity)
            return identity

    def snapshot(self) -> dict[str, object]:
        with self._state_lock:
            return self._snapshot_locked()

    def is_active(self) -> bool:
        """Whether this process currently has one recoverable formal flow."""

        with self._state_lock:
            return self._active_challenge is not None

    def execution_active(self) -> bool:
        """Whether this async flow carries the signed active job capability."""

        with self._state_lock:
            try:
                self._require_execution_capability()
            except ValueError:
                return False
            return True

    def current_execution_capability(self) -> dict[str, object] | None:
        """Return the verified capability only inside its bound operation flow."""

        with self._state_lock:
            try:
                return dict(self._require_execution_capability())
            except ValueError:
                return None

    @contextmanager
    def execution_scope(self, capability: object) -> Iterator[None]:
        """Bind one verified operation capability to the current async flow."""

        with self._state_lock:
            checked = self._require_execution_capability(capability)
        token: Token[dict[str, object] | None] = (
            _EXECUTION_CAPABILITY_CONTEXT.set(checked)
        )
        try:
            yield
        finally:
            _EXECUTION_CAPABILITY_CONTEXT.reset(token)

    def _require_execution_capability(
        self,
        capability: object | None = None,
    ) -> dict[str, object]:
        if self._active_challenge is None or self._execution_capability is None:
            raise ValueError("formal execution capability has no active challenge")
        supplied = (
            capability
            if capability is not None
            else _EXECUTION_CAPABILITY_CONTEXT.get()
        )
        checked = _validate_execution_capability(
            supplied,
            challenge=self._active_challenge,
        )
        if checked != self._execution_capability:
            raise ValueError("formal execution capability does not match the active job")
        return checked

    def _snapshot_locked(self) -> dict[str, object]:
        if not self._bound or self._active_challenge is None:
            raise RuntimeError("formal source snapshot requires an active signed challenge")
        challenge = self._active_challenge
        snapshot: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "anchor_version": _anchor_version(),
            "authority_key_id": _authority_key_id(),
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
            "anchor_version": _anchor_version(),
            "authority_key_id": _authority_key_id(),
            "install_id": self._install_id,
            "composition": self._composition,
            "composition_sha256": self._composition_sha256,
            "runtime_identity": self._runtime_identity,
            "challenge_active": self._active_challenge is not None,
        }

    def require_active_job(self, job_id: object) -> None:
        """Control-plane guard used immediately before a prepared job starts."""

        with self._state_lock:
            if self._active_challenge is None:
                raise ValueError("formal source activation has no active challenge")
            graph = _validate_job_graph(self._active_challenge["job_graph"])
            if job_id != graph["terminal_job_id"]:
                raise ValueError("formal source activation targets a foreign job")

    @staticmethod
    def _activation_request_digest(
        job_id: object,
        capability: Mapping[str, object],
        companion_binding: Mapping[str, object],
    ) -> str:
        return _sha256(
            {
                "job_id": job_id,
                "execution_capability": capability,
                "companion_binding": companion_binding,
                "phase_version": "tripchord-formal-activation-v2",
            }
        )

    def _activation_row_locked(
        self,
        ledger: Mapping[str, dict[str, object]],
        *,
        job_id: object,
        capability: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        for candidate in ledger.values():
            issued = candidate.get("issue_result")
            if not isinstance(issued, dict):
                continue
            try:
                challenge = _validate_challenge(issued.get("challenge"))
                checked = _validate_execution_capability(
                    capability,
                    challenge=challenge,
                )
            except (RuntimeError, ValueError):
                continue
            if job_id != checked["terminal_job_id"]:
                raise ValueError("formal source activation targets a foreign job")
            return candidate, checked
        raise ValueError("formal source activation capability is unknown")

    @staticmethod
    def _activation_state_copy(record: Mapping[str, object]) -> dict[str, object]:
        return json.loads(_canonical_bytes(record))

    def begin_activation(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
        companion_binding: object,
    ) -> dict[str, object]:
        """Persist the first phase before a Companion or job side effect.

        The durable phase record is the recovery authority after a response is
        lost.  It deliberately contains the complete exact request identity;
        the signed capability remains in the issued result and is never
        reconstructed from client fields.
        """

        key = _nonempty_string(
            idempotency_key,
            "formal source activation idempotency key",
        )
        if len(key) > 200:
            raise ValueError("formal source activation idempotency key is invalid")
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row, checked = self._activation_row_locked(
                ledger,
                job_id=job_id,
                capability=capability,
            )
            checked_companion = _validate_companion_binding(companion_binding)
            request_digest = self._activation_request_digest(
                job_id,
                checked,
                checked_companion,
            )
            existing = row.get("activation_record")
            if existing is not None:
                if not isinstance(existing, dict):
                    raise RuntimeError("formal source activation result is invalid")
                if (
                    existing.get("idempotency_key") != key
                    or existing.get("request_digest") != request_digest
                ):
                    raise ValueError(
                        "formal source activation idempotency key was used with a different request"
                    )
                return self._activation_state_copy(existing)
            if row.get("state") != "issued":
                raise ValueError("formal source activation challenge is not active")
            self._require_execution_capability(capability)
            prepared_at = self._utc_now().isoformat()
            record: dict[str, object] = {
                "phase_version": "tripchord-formal-activation-v2",
                "idempotency_key": key,
                "request_digest": request_digest,
                "job_id": job_id,
                "companion_binding": checked_companion,
                "phase": "awaiting_heartbeat",
                "prepared_at": prepared_at,
                "heartbeat_request_digest": None,
                "heartbeat_result": None,
                "started_result": None,
                "result": None,
            }
            row["activation_record"] = record
            self._write_ledger(ledger)
            return self._activation_state_copy(record)

    def activation_state(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
    ) -> dict[str, object]:
        key = _nonempty_string(
            idempotency_key,
            "formal source activation idempotency key",
        )
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row, checked = self._activation_row_locked(
                ledger,
                job_id=job_id,
                capability=capability,
            )
            record = row.get("activation_record")
            if not isinstance(record, dict):
                raise ValueError("formal source activation state is unavailable")
            companion_binding = _validate_companion_binding(
                record.get("companion_binding")
            )
            request_digest = self._activation_request_digest(
                job_id,
                checked,
                companion_binding,
            )
            if (
                not isinstance(record, dict)
                or record.get("idempotency_key") != key
                or record.get("request_digest") != request_digest
            ):
                raise ValueError("formal source activation state is unavailable")
            return self._activation_state_copy(record)

    def pending_activation_request(self) -> dict[str, object] | None:
        """Return the one signed request an ordinary heartbeat may acknowledge."""

        with self._state_lock, self._ledger_lock():
            if self._active_challenge is None or self._execution_capability is None:
                return None
            ledger = self._read_ledger()
            row = ledger.get(str(self._active_challenge["challenge_id"]))
            record = row.get("activation_record") if isinstance(row, dict) else None
            if not isinstance(record, dict) or record.get("phase") != "awaiting_heartbeat":
                return None
            return {
                "job_id": record["job_id"],
                "challenge_id": self._active_challenge["challenge_id"],
                "execution_capability": dict(self._execution_capability),
                "companion_binding": self._activation_state_copy(
                    _validate_companion_binding(record.get("companion_binding"))
                ),
            }

    def record_activation_heartbeat(
        self,
        *,
        acknowledgment: object,
        request_details: object,
        heartbeat: object,
    ) -> dict[str, object]:
        """Atomically bind a real mounted heartbeat request to activation."""

        if not isinstance(acknowledgment, dict) or set(acknowledgment) != {
            "job_id",
            "challenge_id",
            "execution_capability",
            "companion_binding",
        }:
            raise ValueError("formal activation heartbeat acknowledgment is invalid")
        if not isinstance(request_details, dict) or not isinstance(heartbeat, dict):
            raise ValueError("formal activation heartbeat payload is invalid")
        with self._state_lock, self._ledger_lock():
            if self._active_challenge is None:
                raise ValueError("formal activation heartbeat has no active challenge")
            if acknowledgment["challenge_id"] != self._active_challenge["challenge_id"]:
                raise ValueError("formal activation heartbeat targets a foreign challenge")
            checked = self._require_execution_capability(
                acknowledgment["execution_capability"]
            )
            if acknowledgment["job_id"] != checked["terminal_job_id"]:
                raise ValueError("formal activation heartbeat targets a foreign job")
            acknowledged_companion = _validate_companion_binding(
                acknowledgment["companion_binding"]
            )
            ledger = self._read_ledger()
            row = ledger.get(str(checked["challenge_id"]))
            if not isinstance(row, dict) or row.get("state") != "issued":
                raise ValueError("formal activation heartbeat challenge is not active")
            record = row.get("activation_record")
            if not isinstance(record, dict) or record.get("job_id") != checked[
                "terminal_job_id"
            ]:
                raise ValueError("formal activation heartbeat was not prepared")
            expected_companion = _validate_companion_binding(
                record.get("companion_binding")
            )
            if acknowledged_companion != expected_companion:
                raise ValueError("formal activation heartbeat targets a foreign Companion")
            request_identity = _validate_companion_binding(
                {
                    **request_details,
                    "identity_sha256": expected_companion["identity_sha256"],
                }
            )
            heartbeat_identity = _validate_companion_binding(
                {
                    key: heartbeat.get(key)
                    for key in (
                        "companion_id",
                        "providers",
                        "authorized_scope_keys",
                        "adapter_version",
                        "contract_version",
                        "runtime_instance_id",
                        "build_identity",
                    )
                }
                | {"identity_sha256": expected_companion["identity_sha256"]}
            )
            if request_identity != expected_companion or heartbeat_identity != expected_companion:
                raise ValueError("formal activation heartbeat uses a foreign Companion")
            heartbeat_digest = _sha256(
                {
                    "acknowledgment": acknowledgment,
                    "request": request_details,
                    "heartbeat": heartbeat,
                }
            )
            phase = record.get("phase")
            if phase in {
                "heartbeat_recorded",
                "activation_ready",
                "started",
                "completed",
            }:
                if record.get("heartbeat_request_digest") != heartbeat_digest:
                    raise ValueError(
                        "formal activation heartbeat differs from the recorded request"
                    )
                stored = record.get("heartbeat_result")
                if not isinstance(stored, dict):
                    raise RuntimeError("formal activation heartbeat result is unavailable")
                return json.loads(_canonical_bytes(stored))
            if phase != "awaiting_heartbeat":
                raise ValueError("formal activation heartbeat phase is invalid")
            before = self._active_state_payload()
            try:
                self._record_locked(
                    kind="browser_heartbeat",
                    method="POST",
                    path=_BROWSER_PATHS["browser_heartbeat"],
                    subject_ids=(str(heartbeat.get("companion_id")),),
                    details={
                        "request": request_details,
                        "heartbeat": heartbeat,
                        "companion_identity_sha256": expected_companion[
                            "identity_sha256"
                        ],
                    },
                    response_sha256=None,
                )
                result = {
                    "receipt_sha256": self._events[-1]["receipt_sha256"],
                    "companion_id": heartbeat["companion_id"],
                }
                record.update(
                    {
                        "phase": "heartbeat_recorded",
                        "heartbeat_request_digest": heartbeat_digest,
                        "heartbeat_result": result,
                    }
                )
                row["active_state"] = self._active_state_payload()
                self._write_ledger(ledger)
            except Exception:
                self._apply_active_state(before)
                raise
            return json.loads(_canonical_bytes(result))

    def validate_activation_heartbeat_request(
        self,
        *,
        acknowledgment: object,
        request_details: object,
    ) -> None:
        """Reject a foreign formal heartbeat before it mutates bridge state."""

        pending = self.pending_activation_request()
        if not isinstance(acknowledgment, dict) or acknowledgment != pending:
            raise ValueError(
                "formal activation heartbeat acknowledgment is not the pending job"
            )
        if not isinstance(request_details, dict):
            raise ValueError("formal activation heartbeat payload is invalid")
        expected = _validate_companion_binding(acknowledgment["companion_binding"])
        request_identity = _validate_companion_binding(
            {
                **request_details,
                "identity_sha256": expected["identity_sha256"],
            }
        )
        if request_identity != expected:
            raise ValueError("formal activation heartbeat uses a foreign Companion")

    def mark_activation_started(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
        result: object,
    ) -> dict[str, object]:
        if not isinstance(result, dict):
            raise ValueError("formal source activation started result is invalid")
        key = _nonempty_string(idempotency_key, "formal source activation idempotency key")
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row, checked = self._activation_row_locked(
                ledger, job_id=job_id, capability=capability
            )
            record = row.get("activation_record")
            companion_binding = _validate_companion_binding(
                record.get("companion_binding") if isinstance(record, dict) else None
            )
            request_digest = self._activation_request_digest(
                job_id,
                checked,
                companion_binding,
            )
            if (
                not isinstance(record, dict)
                or record.get("idempotency_key") != key
                or record.get("request_digest") != request_digest
            ):
                raise ValueError("formal source activation state is unavailable")
            if record.get("phase") in {"started", "completed"}:
                stored = record.get("started_result")
                durable_result = record.get("result")
                if (
                    not isinstance(stored, dict)
                    or not isinstance(durable_result, dict)
                    or _canonical_bytes(stored) != _canonical_bytes(result)
                    or _canonical_bytes(durable_result) != _canonical_bytes(result)
                ):
                    raise ValueError("formal source activation started result differs")
                return self._activation_state_copy(record)
            if record.get("phase") != "activation_ready":
                raise ValueError("formal source activation has no durable queued receipt")
            if not isinstance(record.get("started_result"), dict) or _canonical_bytes(
                record["started_result"]
            ) != _canonical_bytes(result):
                raise ValueError("formal source activation queued receipt differs")
            record.update({"phase": "started", "result": result})
            self._write_ledger(ledger)
            return self._activation_state_copy(record)

    def prepare_activation_result(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
        result: object,
    ) -> dict[str, object]:
        """Persist the immutable QUEUED response before starting the job."""

        if not isinstance(result, dict):
            raise ValueError("formal source activation queued result is invalid")
        key = _nonempty_string(idempotency_key, "formal source activation idempotency key")
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row, checked = self._activation_row_locked(
                ledger, job_id=job_id, capability=capability
            )
            record = row.get("activation_record")
            companion_binding = _validate_companion_binding(
                record.get("companion_binding") if isinstance(record, dict) else None
            )
            request_digest = self._activation_request_digest(
                job_id, checked, companion_binding
            )
            if (
                not isinstance(record, dict)
                or record.get("idempotency_key") != key
                or record.get("request_digest") != request_digest
            ):
                raise ValueError("formal source activation state is unavailable")
            if record.get("phase") in {"activation_ready", "started", "completed"}:
                stored_started = record.get("started_result")
                if not isinstance(stored_started, dict) or _canonical_bytes(
                    stored_started
                ) != _canonical_bytes(result):
                    raise ValueError("formal source activation queued receipt differs")
                return self._activation_state_copy(record)
            if record.get("phase") != "heartbeat_recorded":
                raise ValueError("formal source activation has no real heartbeat")
            record["phase"] = "activation_ready"
            record["started_result"] = result
            self._write_ledger(ledger)
            return self._activation_state_copy(record)

    def activation_replay(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
        companion_binding: object,
    ) -> dict[str, object] | None:
        """Return the durable activation response for one exact retry identity."""

        key = _nonempty_string(
            idempotency_key,
            "formal source activation idempotency key",
        )
        if len(key) > 200:
            raise ValueError("formal source activation idempotency key is invalid")
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row, checked = self._activation_row_locked(
                ledger, job_id=job_id, capability=capability
            )
            checked_companion = _validate_companion_binding(companion_binding)
            request_digest = self._activation_request_digest(
                job_id,
                checked,
                checked_companion,
            )
            record = row.get("activation_record")
            if record is None:
                if row.get("state") != "issued":
                    raise ValueError("formal source activation challenge is not active")
                self._require_execution_capability(capability)
                return None
            if not isinstance(record, dict):
                raise RuntimeError("formal source activation result is invalid")
            if (
                record.get("idempotency_key") != key
                or record.get("request_digest") != request_digest
            ):
                raise ValueError(
                    "formal source activation idempotency key was used with a different request"
                )
            result = record.get("result")
            if result is None:
                return None
            if not isinstance(result, dict):
                raise RuntimeError("formal source activation result is unavailable")
            return json.loads(_canonical_bytes(result))

    def store_activation_result(
        self,
        *,
        job_id: object,
        capability: object,
        idempotency_key: object,
        result: object,
    ) -> dict[str, object]:
        """Atomically persist the complete response before it is returned."""

        key = _nonempty_string(
            idempotency_key,
            "formal source activation idempotency key",
        )
        if len(key) > 200 or not isinstance(result, dict):
            raise ValueError("formal source activation result is invalid")
        with self._state_lock, self._ledger_lock():
            checked = self._require_execution_capability(capability)
            if job_id != checked["terminal_job_id"]:
                raise ValueError("formal source activation targets a foreign job")
            ledger = self._read_ledger()
            row = ledger.get(str(checked["challenge_id"]))
            if not isinstance(row, dict) or row.get("state") != "issued":
                raise ValueError("formal source activation challenge is not active")
            existing = row.get("activation_record")
            if not isinstance(existing, dict):
                raise ValueError("formal source activation has no persisted begin record")
            companion_binding = _validate_companion_binding(
                existing.get("companion_binding")
            )
            request_digest = self._activation_request_digest(
                job_id,
                checked,
                companion_binding,
            )
            if (
                existing.get("idempotency_key") != key
                or existing.get("request_digest") != request_digest
            ):
                raise ValueError(
                    "formal source activation idempotency key was used with a different request"
                )
            if existing.get("phase") not in {"started", "completed"}:
                raise ValueError("formal source activation has no verified heartbeat/start")
            started_result = existing.get("started_result")
            if not isinstance(started_result, dict) or _canonical_bytes(
                started_result
            ) != _canonical_bytes(result):
                raise ValueError(
                    "formal source activation result differs from its queued receipt"
                )
            stored = existing.get("result")
            if isinstance(stored, dict):
                return json.loads(_canonical_bytes(stored))
            existing["phase"] = "completed"
            existing["result"] = result
            self._write_ledger(ledger)
            return json.loads(_canonical_bytes(result))

    def abort(self, context: object) -> dict[str, object]:
        return self._terminal_transition(context, target="aborted", require_expired=False)

    def expire(self, context: object) -> dict[str, object]:
        return self._terminal_transition(context, target="expired", require_expired=True)

    def _terminal_transition(
        self,
        context: object,
        *,
        target: str,
        require_expired: bool,
    ) -> dict[str, object]:
        fields = {"challenge_id", "run_id", "reason_code"}
        if not isinstance(context, dict) or set(context) != fields:
            raise ValueError("formal source terminal transition context is invalid")
        reason = _nonempty_string(
            context["reason_code"], "formal source terminal reason"
        )
        if len(reason) > 80 or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in reason
        ):
            raise ValueError("formal source terminal reason is invalid")
        with self._state_lock, self._ledger_lock():
            ledger = self._read_ledger()
            row = ledger.get(str(context["challenge_id"]))
            if (
                not isinstance(row, dict)
                or row.get("state") != "issued"
                or row.get("run_id") != context["run_id"]
            ):
                raise ValueError("formal source challenge is not active for transition")
            active_state = row.get("active_state")
            if not isinstance(active_state, dict):
                raise RuntimeError("formal source active state is unavailable")
            challenge = _validate_challenge(active_state.get("challenge"))
            is_expired = self._utc_now() > _require_aware_time(
                challenge["expires_at"], "challenge expires_at"
            )
            if require_expired and not is_expired:
                raise ValueError("formal source challenge is not yet expired")
            transitioned_at = self._utc_now().isoformat()
            row["state"] = target
            row[f"{target}_at"] = transitioned_at
            row["terminal_reason"] = reason
            row.pop("active_state", None)
            self._write_ledger(ledger)
            if (
                self._active_challenge is not None
                and self._active_challenge.get("challenge_id")
                == context["challenge_id"]
            ):
                self._clear_active_state()
            return {
                "challenge_id": context["challenge_id"],
                "run_id": context["run_id"],
                "state": target,
                "transitioned_at": transitioned_at,
            }

    def finalize(
        self,
        context: object,
        *,
        idempotency_key: str | None = None,
        execution_capability: object | None = None,
    ) -> dict[str, object]:
        with self._state_lock:
            return self._finalize_locked(
                context,
                idempotency_key=idempotency_key,
                execution_capability=execution_capability,
            )

    def _finalize_locked(
        self,
        context: object,
        *,
        idempotency_key: str | None,
        execution_capability: object | None,
    ) -> dict[str, object]:
        with self._ledger_lock():
            ledger = self._read_ledger()
            if idempotency_key is not None:
                if not idempotency_key.strip() or len(idempotency_key) > 200:
                    raise ValueError("formal source finalize idempotency key is invalid")
                for row in ledger.values():
                    if row.get("finalize_idempotency_key") != idempotency_key:
                        continue
                    result = row.get("finalize_result")
                    if not isinstance(result, dict):
                        raise RuntimeError("formal source finalize result is unavailable")
                    challenge = _validate_challenge(result.get("challenge"))
                    checked = _validate_execution_capability(
                        execution_capability,
                        challenge=challenge,
                    )
                    request_digest = _sha256(
                        {
                            "context": context,
                            "execution_capability": checked,
                        }
                    )
                    if row.get("finalize_request_digest") != request_digest:
                        raise ValueError(
                            "formal source finalize idempotency key was used "
                            "with a different request"
                        )
                    return json.loads(_canonical_bytes(result))
            active_rows = [
                row for row in ledger.values() if row.get("state") == "issued"
            ]
            if len(active_rows) != 1:
                raise ValueError("formal source finalize has no unique active challenge")
            self._apply_active_state(active_rows[0]["active_state"])
            checked_capability = self._require_execution_capability(
                execution_capability
            )
            return self._finalize_under_ledger_lock(
                context,
                ledger,
                idempotency_key=idempotency_key,
                execution_capability=checked_capability,
            )

    def _finalize_under_ledger_lock(
        self,
        context: object,
        ledger: dict[str, dict[str, object]],
        *,
        idempotency_key: str | None,
        execution_capability: Mapping[str, object],
    ) -> dict[str, object]:
        if self._active_challenge is None or self._baseline is None:
            raise ValueError("formal source finalize has no active challenge")
        challenge = _validate_challenge(self._active_challenge)
        if not isinstance(context, dict) or set(context) != _FINALIZE_CONTEXT_FIELDS:
            raise ValueError("formal source finalize context has an invalid shape")
        if any(context[key] != challenge[key] for key in _CHALLENGE_CONTEXT_FIELDS):
            raise ValueError("formal source finalize context differs from challenge")
        job_graph = _validate_job_graph(challenge["job_graph"])
        terminal_job = _validate_terminal_job_contract(
            context["terminal_job"],
            job_graph=job_graph,
            pair_checkpoint_binding=context["pair_checkpoint_binding"],
        )
        job_member_summary = _job_member_summary(job_graph, terminal_job)
        if self._utc_now() > _require_aware_time(
            challenge["expires_at"], "challenge expires_at"
        ):
            row = ledger.get(str(challenge["challenge_id"]))
            if isinstance(row, dict) and row.get("state") == "issued":
                row["state"] = "expired"
                row["expired_at"] = self._utc_now().isoformat()
                row["terminal_reason"] = "expired_before_finalize"
                row.pop("active_state", None)
                self._write_ledger(ledger)
            self._clear_active_state()
            raise ValueError("formal source challenge expired before consumption")
        after = self._snapshot_locked()
        pre = _validate_snapshot(self._baseline, challenge)
        post = _validate_snapshot(after, challenge)
        receipts = post["events"][int(pre["event_count"]) :]
        binding: dict[str, object] = {
            "schema_version": _BINDING_SCHEMA_VERSION,
            "anchor_version": _anchor_version(),
            "authority_key_id": _authority_key_id(),
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
            "anchor_version": _anchor_version(),
            "authority_key_id": _authority_key_id(),
            "challenge_id": challenge["challenge_id"],
            "nonce_digest": challenge["nonce_digest"],
            "binding_digest": binding["binding_digest"],
            "run_id": challenge["run_id"],
            "tested_commit_sha": challenge["tested_commit_sha"],
            "runtime_identity": challenge["runtime_identity"],
            "pre_event_count": pre["event_count"],
            "post_event_count": post["event_count"],
            "delta_digest": _sha256(receipts),
            "job_member_summary": job_member_summary,
            "issued_at": challenge["issued_at"],
            "verified_at": verified_at,
        }
        receipt["signature"] = _sign(
            self._private_key,
            _receipt_proof_payload(receipt),
        )
        binding["authority_receipt"] = receipt
        validate_formal_source_evidence(binding, receipt, challenge)
        row = ledger.get(str(challenge["challenge_id"]))
        if (
            not isinstance(row, dict)
            or row.get("state") != "issued"
            or row.get("challenge_digest") != _sha256(challenge)
            or row.get("run_id") != challenge["run_id"]
        ):
            raise ValueError("formal source challenge was already consumed")
        result = {
            "challenge": challenge,
            "binding": binding,
            "authority_receipt": receipt,
        }
        row.update(
            {
                "state": "consumed",
                "binding_digest": binding["binding_digest"],
                "verified_at": verified_at,
                "terminal_job_graph_sha256": job_member_summary[
                    "terminal_job_graph_sha256"
                ],
            }
        )
        if idempotency_key is not None:
            row.update(
                {
                    "finalize_idempotency_key": idempotency_key,
                    "finalize_request_digest": _sha256(
                        {
                            "context": context,
                            "execution_capability": execution_capability,
                        }
                    ),
                    "finalize_result": result,
                }
            )
        row.pop("active_state", None)
        self._write_ledger(ledger)
        self._clear_active_state()
        return result

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
            if self._active_challenge is None:
                return
            self._require_execution_capability()
            with self._ledger_lock():
                ledger = self._read_ledger()
                challenge_id = str(self._active_challenge["challenge_id"])
                row = ledger.get(challenge_id)
                if not isinstance(row, dict) or row.get("state") != "issued":
                    raise RuntimeError("formal source active challenge is not ledger-active")
                self._apply_active_state(row["active_state"])
                challenge = self._active_challenge
                if challenge is None:  # pragma: no cover - apply enforces this
                    raise RuntimeError("formal source active state disappeared")
                if self._utc_now() > _require_aware_time(
                    challenge["expires_at"], "challenge expires_at"
                ):
                    row["state"] = "expired"
                    row["expired_at"] = self._utc_now().isoformat()
                    row["terminal_reason"] = "expired_before_event_record"
                    row.pop("active_state", None)
                    self._write_ledger(ledger)
                    self._clear_active_state()
                    raise RuntimeError("formal live event occurred after challenge expiry")
                before = self._active_state_payload()
                try:
                    self._record_locked(
                        kind=kind,
                        method=method,
                        path=path,
                        subject_ids=subject_ids,
                        details=details,
                        response_sha256=response_sha256,
                    )
                    row["active_state"] = self._active_state_payload()
                    self._write_ledger(ledger)
                except Exception:
                    self._apply_active_state(before)
                    raise

    def _clear_active_state(self) -> None:
        self._active_challenge = None
        self._execution_capability = None
        self._baseline = None
        self._events = []
        self._chain_sha256 = self._composition_sha256
        self._last_heartbeat = None

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
                **{
                    key: challenge[key]
                    for key in _CHALLENGE_CONTEXT_FIELDS - {"job_graph"}
                },
                "job_graph_sha256": challenge["job_graph"]["job_graph_sha256"],
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
        _validate_business_event_details(
            (*self._events, event),
            job_graph=_validate_job_graph(challenge["job_graph"]),
            candidate_set_sha256=str(challenge["candidate_set_sha256"]),
            require_complete=False,
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
        issue_idempotency_fields = {
            "issue_idempotency_key",
            "issue_request_digest",
            "issue_result",
        }
        activation_fields = {"activation_record"}
        issued_fields = ledger_fields | {"active_state"}
        consumed_fields = ledger_fields | {
            "binding_digest",
            "verified_at",
            "terminal_job_graph_sha256",
        }
        finalize_idempotency_fields = {
            "finalize_idempotency_key",
            "finalize_request_digest",
            "finalize_result",
        }
        expired_fields = ledger_fields | {"expired_at", "terminal_reason"}
        aborted_fields = ledger_fields | {"aborted_at", "terminal_reason"}
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
                else aborted_fields
                if state == "aborted"
                else issued_fields
            )
            present_idempotency_fields = set(value) & issue_idempotency_fields
            if present_idempotency_fields not in (set(), issue_idempotency_fields):
                raise RuntimeError("formal source challenge ledger row is invalid")
            expected |= present_idempotency_fields
            present_activation_fields = set(value) & activation_fields
            expected |= present_activation_fields
            present_finalize_fields = set(value) & finalize_idempotency_fields
            if present_finalize_fields not in (set(), finalize_idempotency_fields):
                raise RuntimeError("formal source challenge ledger row is invalid")
            if present_finalize_fields and state != "consumed":
                raise RuntimeError("formal source challenge ledger row is invalid")
            expected |= present_finalize_fields
            if set(value) != expected or state not in {
                "issued",
                "consumed",
                "expired",
                "aborted",
            }:
                raise RuntimeError("formal source challenge ledger row is invalid")
            _nonempty_string(value["run_id"], "formal source ledger run_id")
            _require_sha256(value["challenge_digest"], "formal source ledger challenge")
            _require_aware_time(value["issued_at"], "formal source ledger issued_at")
            _require_aware_time(value["expires_at"], "formal source ledger expires_at")
            if present_idempotency_fields:
                _nonempty_string(
                    value["issue_idempotency_key"],
                    "formal source ledger issue idempotency key",
                )
                _require_sha256(
                    value["issue_request_digest"],
                    "formal source ledger issue request",
                )
                if not isinstance(value["issue_result"], dict):
                    raise RuntimeError("formal source ledger issue result is invalid")
            if present_finalize_fields:
                _nonempty_string(
                    value["finalize_idempotency_key"],
                    "formal source ledger finalize idempotency key",
                )
                _require_sha256(
                    value["finalize_request_digest"],
                    "formal source ledger finalize request",
                )
                if not isinstance(value["finalize_result"], dict):
                    raise RuntimeError("formal source ledger finalize result is invalid")
            if present_activation_fields:
                activation = value["activation_record"]
                phased_activation_fields = {
                    "phase_version",
                    "idempotency_key",
                    "request_digest",
                    "result",
                    "job_id",
                    "companion_binding",
                    "phase",
                    "prepared_at",
                    "heartbeat_request_digest",
                    "heartbeat_result",
                    "started_result",
                }
                if (
                    not isinstance(activation, dict)
                    or set(activation) != phased_activation_fields
                ):
                    raise RuntimeError("formal source ledger activation result is invalid")
                _nonempty_string(
                    activation["idempotency_key"],
                    "formal source ledger activation idempotency key",
                )
                if activation["phase_version"] != "tripchord-formal-activation-v2":
                    raise RuntimeError(
                        "formal source ledger activation phase version is invalid"
                    )
                _require_sha256(
                    activation["request_digest"],
                    "formal source ledger activation request",
                )
                _nonempty_string(
                    activation["job_id"],
                    "formal source ledger activation job",
                )
                _validate_companion_binding(activation["companion_binding"])
                phase = activation["phase"]
                if phase not in {
                    "awaiting_heartbeat",
                    "heartbeat_recorded",
                    "activation_ready",
                    "started",
                    "completed",
                }:
                    raise RuntimeError("formal source ledger activation phase is invalid")
                _require_aware_time(
                    activation["prepared_at"],
                    "formal source ledger activation prepared_at",
                )
                heartbeat_digest = activation["heartbeat_request_digest"]
                heartbeat_result = activation["heartbeat_result"]
                started_result = activation["started_result"]
                result = activation["result"]
                if phase == "awaiting_heartbeat":
                    if any(
                        item is not None
                        for item in (
                            heartbeat_digest,
                            heartbeat_result,
                            started_result,
                            result,
                        )
                    ):
                        raise RuntimeError(
                            "formal source ledger activation phase is inconsistent"
                        )
                else:
                    _require_sha256(
                        heartbeat_digest,
                        "formal source ledger activation heartbeat request",
                    )
                    if not isinstance(heartbeat_result, dict):
                        raise RuntimeError(
                            "formal source ledger activation heartbeat is invalid"
                        )
                if phase in {"activation_ready", "started", "completed"} and not isinstance(
                    started_result, dict
                ):
                    raise RuntimeError(
                        "formal source ledger activation started result is invalid"
                    )
                if phase in {"started", "completed"} and not isinstance(result, dict):
                    raise RuntimeError(
                        "formal source ledger activation result is invalid"
                    )
                if phase in {"started", "completed"} and _canonical_bytes(result) != (
                    _canonical_bytes(started_result)
                ):
                    raise RuntimeError(
                        "formal source ledger activation result differs from started result"
                    )
                if phase not in {"started", "completed"} and result is not None:
                    raise RuntimeError(
                        "formal source ledger activation result is premature"
                    )
            if value["state"] == "consumed":
                _require_sha256(value["binding_digest"], "formal source ledger binding")
                _require_aware_time(value["verified_at"], "formal source ledger verified_at")
                _require_sha256(
                    value["terminal_job_graph_sha256"],
                    "formal source ledger terminal job graph",
                )
            elif value["state"] == "expired":
                _require_aware_time(value["expired_at"], "formal source ledger expired_at")
                _nonempty_string(
                    value["terminal_reason"], "formal source ledger terminal reason"
                )
            elif value["state"] == "aborted":
                _require_aware_time(value["aborted_at"], "formal source ledger aborted_at")
                _nonempty_string(
                    value["terminal_reason"], "formal source ledger terminal reason"
                )
            elif not isinstance(value["active_state"], dict):
                raise RuntimeError("formal source ledger active state is invalid")
        return parsed

    @contextmanager
    def _ledger_lock(self) -> Iterator[None]:
        lock_path = self._ledger_path.with_suffix(self._ledger_path.suffix + ".lock")
        _protected_directory(lock_path.parent, "formal source ledger parent")
        common_flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(
                lock_path,
                common_flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            _fsync_directory(lock_path.parent)
        except FileExistsError:
            descriptor = os.open(lock_path, common_flags)
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
        _protected_directory(
            self._ledger_path.parent, "formal source ledger parent"
        )
        current = _protected_regular_file(
            self._ledger_path,
            "formal source challenge ledger",
            missing_ok=True,
        )
        del current
        temporary = self._ledger_path.with_name(
            f".{self._ledger_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        try:
            expected = _canonical_bytes(ledger)
            _exclusive_owner_write(
                temporary,
                expected,
                "formal source challenge ledger temporary",
            )
            os.replace(temporary, self._ledger_path)
            replaced = _protected_regular_file(
                self._ledger_path, "formal source challenge ledger"
            )
            if replaced != expected:
                raise RuntimeError("formal source challenge ledger replace is inconsistent")
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
    private_key_path: Path | None = None,
    ledger_path: Path | None = None,
    trust_root: Path | None = None,
    now: Callable[[], datetime] | None = None,
) -> FormalLiveSourceAuthority:
    """Load the protected signer and prove it matches the fixed public anchor.

    The process and every local program with this OS uid remain inside the
    signer trust boundary.  Ordinary API principals and Browser credentials do
    not gain this constructor capability.
    """
    generation_root, _generation = _current_generation(trust_root)
    resolved_private_key = private_key_path or generation_root / "authority-private.pem"
    resolved_ledger = ledger_path or formal_source_trust_root(trust_root) / "ledger.json"
    raw = _protected_regular_file(
        resolved_private_key, "formal source authority private key"
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
    anchor = _load_verification_anchor(trust_root)
    if public_der != anchor["public_key_der"]:
        raise RuntimeError(
            "formal source authority private key does not match the fixed anchor"
        )
    return FormalLiveSourceAuthority(
        commit_sha=commit_sha,
        private_key=loaded,
        ledger_path=resolved_ledger,
        runtime_identity=runtime_identity,
        now=now,
        _startup_capability=_STARTUP_CAPABILITY,
    )
