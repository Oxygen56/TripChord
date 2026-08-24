import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, cast
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord import __version__
from tripchord.agents import live_flexible_from_text_worker
from tripchord.agents.agent_budget import request_agent_budgeted
from tripchord.agents.companion_control_tools import (
    BrowserCompanionBuildReconcileRequest,
    BrowserCompanionBuildReconcileResponse,
    BrowserCompanionRuntimeExecutorAgent,
    BrowserCompanionRuntimeSupervisor,
    CompanionControlIdempotencyConflictError,
    CompanionControlInvalidIdempotencyKeyError,
    CompanionControlToolError,
)
from tripchord.agents.context_budget import BudgetedAgentContextBuilder
from tripchord.agents.demo_factory import build_replay_agent_system
from tripchord.agents.flexible_live_system import (
    FlexibleLiveAgentRun,
    FlexibleLiveAgentSystem,
    FlexiblePackageConstraints,
    FlexiblePairExecution,
    LiveDatePairRunner,
    PairCheckpointReporter,
)
from tripchord.agents.live_advisory import AgenticRunSummary
from tripchord.agents.live_jobs import (
    DURABLE_LIVE_PLANNING_BOUNDARY,
    NON_DURABLE_LIVE_PLANNING_BOUNDARY,
    TERMINAL_LIVE_PLANNING_JOB_STATES,
    LiveJobProgressReporter,
    LiveJobWorkerCommand,
    LivePlanningJobCancellationPendingError,
    LivePlanningJobCapacityError,
    LivePlanningJobIdempotencyConflictError,
    LivePlanningJobInactiveError,
    LivePlanningJobRegistry,
    LivePlanningJobRegistryPostCommitError,
    LivePlanningJobSnapshot,
    LivePlanningJobState,
    LiveSourceTerminalEvent,
)
from tripchord.agents.live_monitor import (
    LiveMonitorCheck,
    LiveMonitorStatus,
    LiveQuoteMonitorRegistry,
)
from tripchord.agents.live_system import (
    LiveCoverageMode,
    LiveDataProvider,
    LiveEventReplanRun,
    LiveFinalizationState,
    LivePackageAgentRun,
    LivePackageAgentSystem,
    LivePackageEvent,
    LiveRunPurpose,
)
from tripchord.agents.memory import (
    MemoryAccessContext,
    MemoryKind,
    MemoryQuery,
    MemoryRecord,
    MemoryScope,
    MemoryStore,
    MemoryVolatility,
    PrivacyBoundary,
    ProviderCapabilitySeed,
    confirmed_preference_constitution,
    seed_provider_capability_records,
)
from tripchord.agents.model_gateway import (
    InMemoryModelTraceSink,
    ModelHTTPPostClient,
    ModelRouter,
    build_model_client,
)
from tripchord.agents.model_http_runtime import ManagedModelHTTPRuntime
from tripchord.agents.models import AgentRole
from tripchord.agents.package_request import (
    HybridPackageRequirementAgent,
    PackageRequestState,
    UnresolvedRequirement,
    project_preferences_to_intent_template,
)
from tripchord.agents.persistent_memory import (
    CorruptionPolicy,
    PersistentMemoryStore,
)
from tripchord.agents.plan_modification import (
    LivePlanModificationStatus,
    parse_live_plan_modification,
)
from tripchord.agents.rag import EvidenceRagRetriever
from tripchord.agents.stay_area import system_stay_area_search_profile
from tripchord.api import (
    LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY,
    AgentMemoryListResponse,
    AgentPlanningRequest,
    AgentPlanningResponse,
    AgentRuntimeProvenance,
    AgentRuntimeStatusResponse,
    ConfirmPreferenceMemoryRequest,
    ConfirmPreferenceMemoryResponse,
    CreatePlanningJobRequest,
    CreateWorkspaceRequest,
    GeocodeRequest,
    LiveAgentEventReplanRequest,
    LiveAgentEventReplanResponse,
    LiveAgentPlanningRequest,
    LiveAgentPlanningResponse,
    LiveFlexibleAgentPlanningRequest,
    LiveFlexibleAgentPlanningResponse,
    LiveFlexibleFromTextPlanningRequest,
    LiveFlexibleFromTextPlanningResponse,
    LiveFlexiblePairRunHandle,
    LiveMonitorResponse,
    LivePlanModificationRequest,
    LivePlanModificationResponse,
    OptimizePlanRequest,
    OptimizePlanResponse,
    ParseTripRequest,
    PlaceSearchRequest,
    RepairPlanRequest,
    ReplanRequest,
    RevokeMemoryResponse,
    RouteRequest,
    SavePlanRequest,
    StartLiveFlexibleFromTextJobResponse,
    StartLiveMonitorRequest,
    StartTripPlanningRequest,
    StartTripPlanningResponse,
    VerifyRequest,
    VerifyResponse,
    WeatherRequest,
    WorkspaceReplanRequest,
    WorkspaceReplanResponse,
    _decision_candidate_projections,
    build_best_available_plan_projection,
    build_final_plan_projection,
    create_user_quote,
    optimize_plan,
    parse_trip_request,
    repair_plan,
    replan_after_event,
    revalidate_offer,
    search_offers,
    verify_plan,
)
from tripchord.auth import Principal, get_principal
from tripchord.config import Settings, get_settings
from tripchord.domain.common import Coordinates
from tripchord.domain.offers import TravelOffer
from tripchord.domain.travel_data import Place, RouteLeg, WeatherWindow
from tripchord.formal_live_source import (
    FormalLiveSourceAuthority,
    formal_source_trust_root,
    load_formal_live_source_authority,
    read_owner_only_text,
)
from tripchord.jobs import (
    JobConflictError,
    JobNotFoundError,
    JobRepository,
    JobSnapshot,
    JobStatus,
    PlanningJobRunner,
)
from tripchord.observability import configure_logging, metrics, observe_request
from tripchord.persistence import Database, WorkspaceRepository
from tripchord.persistence.booking_ledger import BookingLedgerStore
from tripchord.persistence.browser_tasks import DurableBrowserTaskStore
from tripchord.persistence.handoff_store import HandoffStore
from tripchord.persistence.live_monitors import DbLiveMonitorStore, LiveMonitorNotFoundError
from tripchord.persistence.live_planning_jobs import DurableLivePlanningJobStore
from tripchord.persistence.repository import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceSnapshot,
)
from tripchord.planning.adaptive import AdaptiveReplanner
from tripchord.planning.assembler import PlanningProblemAssembler, ReplayPlaceCatalog
from tripchord.planning.flexible_dates import (
    FlexibleDateExplorer,
    FlexibleQueryPlanBuilder,
)
from tripchord.planning.frozen_graph import frozen_v4_window_for_run
from tripchord.planning.package import PackageDecisionState, PackageEventKind
from tripchord.planning.policy import ReplanPolicySelector
from tripchord.planning.problem import PlanningInfeasible
from tripchord.planning.repair import PlanDiff, diff_plans
from tripchord.planning.replanner import LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.stay_plans import system_stay_plan_candidate_set
from tripchord.planning.workflow import WorkflowResult
from tripchord.platform.adapters import (
    default_browser_providers_from_registry,
    default_platforms_from_registry,
)
from tripchord.platform.api import guard_live_start
from tripchord.platform.api import router as provider_api_router
from tripchord.platform.booking import BookingLedger
from tripchord.platform.capability import ProviderVertical
from tripchord.platform.registry import build_default_registry
from tripchord.platform.search_run_builder import build_search_run, derive_scope_from_task_id
from tripchord.platform.terminal import SearchRun
from tripchord.platform.wiring_api import router as wiring_api_router
from tripchord.providers.amap import AmapTravelDataProvider
from tripchord.providers.arena_official import ArenaOfficialLodgingProvider
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult, ProviderError
from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    CONTROL_TOKEN_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    LIVE_V5_BROWSER_PROVIDERS,
    BrowserTaskBridge,
    create_browser_bridge_app,
    formal_worker_source_token,
    is_loopback_client,
)
from tripchord.providers.factory import build_amap_provider, build_provider_registry
from tripchord.providers.icom_transfer import (
    IComTransferProvider,
    fetch_icom_cny_reference_estimate,
)
from tripchord.providers.kaani_official import KaaniOfficialLodgingProvider
from tripchord.providers.user_snapshot import UserQuoteInput
from tripchord.rate_limit import RateLimiter
from tripchord.runtime_provenance import PROVENANCE
from tripchord.security.secrets import redact_secrets

_BROWSER_BRIDGE_MOUNT = "/browser-bridge"
_LIVE_RUN_CACHE_CAPACITY = 64
_LIVE_RUN_CACHE_TTL = timedelta(minutes=30)
# Version 2 invalidates snapshots written before the current exploration /
# publication DAG-and-seal contract.  Those v1 runs cannot be reconstructed
# safely under the current ``LivePackageAgentRun`` validators, so a verified
# v1 envelope is deliberately retired instead of being treated as corruption.
_LIVE_RUN_CACHE_SCHEMA_VERSION: Final = 2
_LIVE_RUN_CACHE_RETIRED_SCHEMA_VERSIONS: Final = frozenset({1})
_LIVE_RUN_CACHE_FILE_MODE: Final = 0o600
_LIVE_RUN_CACHE_DIRECTORY_MODE: Final = 0o700
_MAX_CACHED_NORMALIZATION_RESULTS = 256
_MAX_CACHED_SOURCE_TASK_IDS = 128
_LIVE_MONITOR_FRESHNESS_SAFETY_SECONDS = 30
_PROVIDER_CAPABILITY_SEEDS = (
    *(
        ProviderCapabilitySeed(
            provider=provider.value,
            verticals=(
                ("flight", "lodging") if provider.value in {"ctrip", "qunar"} else ("flight",)
            ),
            read_only=True,
            requires_authenticated_browser_session=True,
            booking_supported=False,
            capability_version="live-v5-browser-contract-2026-08-03",
        )
        for provider in LIVE_V5_BROWSER_PROVIDERS
    ),
    ProviderCapabilitySeed(
        provider="icom-public-transfer",
        verticals=("public-transfer",),
        read_only=True,
        requires_authenticated_browser_session=False,
        booking_supported=False,
        capability_version="icom-public-readonly-v1",
    ),
)


@dataclass(slots=True)
class _LiveRunCacheEntry:
    tenant_partition_sha256: str
    run: LivePackageAgentRun
    created_at: datetime
    expires_at: datetime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveRunCacheError(RuntimeError):
    """Base error for the optional durable live-run cache."""


class LiveRunCacheLoadError(LiveRunCacheError):
    """Raised when a durable cache snapshot cannot be trusted in full."""


class LiveRunCacheWriteError(LiveRunCacheError):
    """Raised when an atomic live-run cache snapshot write fails."""


class LiveRunCache:
    """Tenant-bound, fixed-TTL LRU state for local event replanning.

    When ``state_path`` is configured, each mutation is stored as a checksummed
    atomic JSON snapshot.  This is deliberately a single-process, single-owner
    adapter: it does not coordinate multiple Uvicorn/Gunicorn workers or merge
    concurrent snapshots written by separate cache instances.
    """

    def __init__(
        self,
        *,
        capacity: int = _LIVE_RUN_CACHE_CAPACITY,
        ttl: timedelta = _LIVE_RUN_CACHE_TTL,
        now: Callable[[], datetime] | None = None,
        state_path: str | Path | None = None,
        corruption_policy: CorruptionPolicy = CorruptionPolicy.FAIL_CLOSED,
    ) -> None:
        if capacity < 1:
            raise ValueError("live run cache capacity must be positive")
        if ttl <= timedelta(0):
            raise ValueError("live run cache TTL must be positive")
        self._capacity = capacity
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))
        raw_path = Path(state_path).expanduser() if state_path is not None else None
        if raw_path is not None and raw_path.is_symlink():
            raise LiveRunCacheLoadError("live run cache state path must not be a symlink")
        self._state_path = raw_path.resolve(strict=False) if raw_path is not None else None
        self._corruption_policy = corruption_policy
        self._entries: OrderedDict[str, _LiveRunCacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        if self._state_path is not None:
            self._prepare_parent()
            self._restore(self._utc_now())

    @property
    def state_path(self) -> Path | None:
        return self._state_path

    @property
    def persistence_enabled(self) -> bool:
        return self._state_path is not None

    async def put(
        self,
        tenant_id: str,
        run: LivePackageAgentRun,
    ) -> tuple[str, datetime]:
        partition = self._tenant_partition(tenant_id)
        async with self._lock:
            before = OrderedDict(self._entries)
            now = self._utc_now()
            self._prune(now)
            while len(self._entries) >= self._capacity:
                self._entries.popitem(last=False)
            run_id = self._new_run_id()
            expires_at = now + self._ttl
            self._entries[run_id] = _LiveRunCacheEntry(
                tenant_partition_sha256=partition,
                run=run,
                created_at=now,
                expires_at=expires_at,
            )
            try:
                self._persist_locked()
            except Exception:
                self._entries = before
                raise
            return run_id, expires_at

    async def import_worker_runs(
        self,
        tenant_id: str,
        pair_runs: tuple[tuple[str, LivePackageAgentRun], ...],
    ) -> tuple[LiveFlexiblePairRunHandle, ...]:
        """Atomically import cache results returned by one worker process.

        Worker-local ``LiveRunCache`` ids are meaningless in the parent API.
        The parent validates every serialized run first, then allocates fresh
        tenant-bound ids and commits the entire batch in one cache transaction.
        Any validation/persist failure restores the exact pre-import entries;
        no partial handle set can be published by the job registry.
        """
        if len(pair_runs) > 8:
            raise ValueError("worker live-run cache batch exceeds eight pairs")
        if len(pair_runs) > self._capacity:
            raise ValueError("worker live-run cache batch exceeds cache capacity")
        pair_ids = tuple(pair_id for pair_id, _run in pair_runs)
        if any(not pair_id or len(pair_id) > 200 for pair_id in pair_ids):
            raise ValueError("worker live-run cache date pair id is invalid")
        if len(set(pair_ids)) != len(pair_ids):
            raise ValueError("worker live-run cache date pair ids must be unique")
        partition = self._tenant_partition(tenant_id)
        async with self._lock:
            before = OrderedDict(self._entries)
            now = self._utc_now()
            self._prune(now)
            handles: list[LiveFlexiblePairRunHandle] = []
            try:
                for date_pair_id, run in pair_runs:
                    while len(self._entries) >= self._capacity:
                        self._entries.popitem(last=False)
                    run_id = self._new_run_id()
                    expires_at = now + self._ttl
                    self._entries[run_id] = _LiveRunCacheEntry(
                        tenant_partition_sha256=partition,
                        run=run,
                        created_at=now,
                        expires_at=expires_at,
                    )
                    handles.append(
                        LiveFlexiblePairRunHandle(
                            date_pair_id=date_pair_id,
                            run_id=run_id,
                            expires_at=expires_at,
                        )
                    )
                self._persist_locked()
            except Exception:
                self._entries = before
                raise
            return tuple(handles)

    async def get(
        self,
        run_id: str,
        tenant_id: str,
    ) -> _LiveRunCacheEntry | None:
        partition = self._tenant_partition(tenant_id)
        async with self._lock:
            before = OrderedDict(self._entries)
            now = self._utc_now()
            changed = self._prune(now)
            entry = self._entries.get(run_id)
            if entry is None or not hmac.compare_digest(
                entry.tenant_partition_sha256,
                partition,
            ):
                if changed:
                    try:
                        self._persist_locked()
                    except Exception:
                        self._entries = before
                        raise
                return None
            changed = changed or next(reversed(self._entries)) != run_id
            self._entries.move_to_end(run_id)
            if changed:
                try:
                    self._persist_locked()
                except Exception:
                    self._entries = before
                    raise
            return entry

    async def replace(
        self,
        run_id: str,
        tenant_id: str,
        expected: _LiveRunCacheEntry,
        run: LivePackageAgentRun,
    ) -> datetime | None:
        partition = self._tenant_partition(tenant_id)
        async with self._lock:
            before = OrderedDict(self._entries)
            now = self._utc_now()
            changed = self._prune(now)
            current = self._entries.get(run_id)
            if current is not expected or not hmac.compare_digest(
                current.tenant_partition_sha256,
                partition,
            ):
                if changed:
                    try:
                        self._persist_locked()
                    except Exception:
                        self._entries = before
                        raise
                return None
            previous_run = current.run
            current.run = run
            self._entries.move_to_end(run_id)
            try:
                self._persist_locked()
            except Exception:
                current.run = previous_run
                self._entries = before
                raise
            return current.expires_at

    async def clear(self) -> None:
        async with self._lock:
            before = OrderedDict(self._entries)
            self._entries.clear()
            try:
                self._persist_locked()
            except Exception:
                self._entries = before
                raise

    def _new_run_id(self) -> str:
        while True:
            candidate = f"live-run-{uuid4()}"
            if candidate not in self._entries:
                return candidate

    def _prune(self, now: datetime) -> bool:
        expired = [run_id for run_id, entry in self._entries.items() if entry.expires_at <= now]
        for run_id in expired:
            self._entries.pop(run_id, None)
        return bool(expired)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("live run cache clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)

    @staticmethod
    def _tenant_partition(tenant_id: str) -> str:
        if not tenant_id.strip():
            raise ValueError("live run cache tenant id must not be empty")
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()

    def _prepare_parent(self) -> None:
        state_path = self._required_state_path()
        if state_path.exists() and state_path.is_symlink():
            raise LiveRunCacheLoadError("live run cache state path must not be a symlink")
        parent = state_path.parent
        parent.mkdir(mode=_LIVE_RUN_CACHE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if not parent.is_dir():
            raise LiveRunCacheLoadError("live run cache state parent must be a directory")

    def _restore(self, now: datetime) -> None:
        state_path = self._required_state_path()
        if not state_path.exists():
            return
        try:
            restored, needs_rewrite = self._read_validated_snapshot(now)
        except Exception as exc:
            if self._corruption_policy == CorruptionPolicy.QUARANTINE:
                self._quarantine_corrupt_snapshot()
                return
            if isinstance(exc, LiveRunCacheLoadError):
                raise
            raise LiveRunCacheLoadError("live run cache snapshot validation failed") from exc
        self._entries = restored
        if needs_rewrite:
            self._persist_locked()

    def _read_validated_snapshot(
        self,
        now: datetime,
    ) -> tuple[OrderedDict[str, _LiveRunCacheEntry], bool]:
        state_path = self._required_state_path()
        if state_path.is_symlink():
            raise LiveRunCacheLoadError("live run cache state path must not be a symlink")
        try:
            file_stat = state_path.stat()
            if not stat.S_ISREG(file_stat.st_mode):
                raise LiveRunCacheLoadError("live run cache state path must be a regular file")
            os.chmod(state_path, _LIVE_RUN_CACHE_FILE_MODE)
            document = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LiveRunCacheLoadError("live run cache snapshot is unreadable") from exc
        if not isinstance(document, dict):
            raise LiveRunCacheLoadError("live run cache snapshot root must be an object")
        schema_version = document.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise LiveRunCacheLoadError("invalid live run cache snapshot schema")
        if (
            schema_version != _LIVE_RUN_CACHE_SCHEMA_VERSION
            and schema_version not in _LIVE_RUN_CACHE_RETIRED_SCHEMA_VERSIONS
        ):
            raise LiveRunCacheLoadError("unsupported live run cache snapshot schema")
        raw_entries = document.get("entries")
        digest = document.get("entries_sha256")
        if not isinstance(raw_entries, list) or not isinstance(digest, str):
            raise LiveRunCacheLoadError("live run cache snapshot envelope is incomplete")
        if not hmac.compare_digest(digest, self._entries_digest(raw_entries)):
            raise LiveRunCacheLoadError("live run cache snapshot checksum mismatch")
        if schema_version in _LIVE_RUN_CACHE_RETIRED_SCHEMA_VERSIONS:
            return OrderedDict(), True

        restored: OrderedDict[str, _LiveRunCacheEntry] = OrderedDict()
        needs_rewrite = False
        expected_keys = {
            "run_id",
            "tenant_partition_sha256",
            "created_at",
            "expires_at",
            "run",
        }
        try:
            for raw in raw_entries:
                if not isinstance(raw, dict) or set(raw) != expected_keys:
                    raise LiveRunCacheLoadError(
                        "live run cache snapshot contains an invalid entry envelope"
                    )
                run_id = raw["run_id"]
                partition = raw["tenant_partition_sha256"]
                if not isinstance(run_id, str) or not run_id.startswith("live-run-"):
                    raise LiveRunCacheLoadError("live run cache snapshot contains an invalid id")
                if run_id in restored:
                    raise LiveRunCacheLoadError("live run cache snapshot contains duplicate ids")
                if (
                    not isinstance(partition, str)
                    or len(partition) != 64
                    or any(character not in "0123456789abcdef" for character in partition)
                ):
                    raise LiveRunCacheLoadError(
                        "live run cache snapshot contains an invalid tenant partition"
                    )
                created_at = datetime.fromisoformat(raw["created_at"])
                persisted_expiry = datetime.fromisoformat(raw["expires_at"])
                if created_at.tzinfo is None or persisted_expiry.tzinfo is None:
                    raise LiveRunCacheLoadError(
                        "live run cache snapshot contains a naive timestamp"
                    )
                created_at = created_at.astimezone(UTC)
                persisted_expiry = persisted_expiry.astimezone(UTC)
                if persisted_expiry <= created_at:
                    raise LiveRunCacheLoadError(
                        "live run cache snapshot contains an invalid TTL interval"
                    )
                expires_at = min(persisted_expiry, created_at + self._ttl)
                if expires_at != persisted_expiry:
                    needs_rewrite = True
                if expires_at <= now:
                    needs_rewrite = True
                    continue
                restored[run_id] = _LiveRunCacheEntry(
                    tenant_partition_sha256=partition,
                    run=LivePackageAgentRun.model_validate(raw["run"]),
                    created_at=created_at,
                    expires_at=expires_at,
                )
        except LiveRunCacheLoadError:
            raise
        except Exception as exc:
            raise LiveRunCacheLoadError(
                "live run cache snapshot contains an invalid entry"
            ) from exc

        if len(restored) > self._capacity:
            restored = OrderedDict(tuple(restored.items())[-self._capacity :])
            needs_rewrite = True
        return restored, needs_rewrite

    def _persist_locked(self) -> None:
        if self._state_path is None:
            return
        entries = [
            {
                "run_id": run_id,
                "tenant_partition_sha256": entry.tenant_partition_sha256,
                "created_at": entry.created_at.isoformat(),
                "expires_at": entry.expires_at.isoformat(),
                "run": entry.run.model_dump(mode="json"),
            }
            for run_id, entry in self._entries.items()
        ]
        document = {
            "schema_version": _LIVE_RUN_CACHE_SCHEMA_VERSION,
            "entries_sha256": self._entries_digest(entries),
            "entries": entries,
        }
        payload = self._canonical_json(document) + b"\n"
        state_path = self._required_state_path()
        temporary_path = state_path.with_name(f".{state_path.name}.tmp-{uuid4().hex}")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _LIVE_RUN_CACHE_FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, state_path)
            os.chmod(state_path, _LIVE_RUN_CACHE_FILE_MODE)
            self._fsync_parent()
        except Exception as exc:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)
            raise LiveRunCacheWriteError("atomic live run cache snapshot write failed") from exc

    def _quarantine_corrupt_snapshot(self) -> None:
        state_path = self._required_state_path()
        quarantine_path = state_path.with_name(
            f"{state_path.name}.corrupt-{self._utc_now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex}"
        )
        try:
            os.replace(state_path, quarantine_path)
            os.chmod(quarantine_path, _LIVE_RUN_CACHE_FILE_MODE)
            self._fsync_parent()
        except OSError as exc:
            raise LiveRunCacheLoadError(
                "live run cache snapshot is corrupt and could not be quarantined"
            ) from exc

    def _fsync_parent(self) -> None:
        state_path = self._required_state_path()
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(state_path.parent, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _required_state_path(self) -> Path:
        if self._state_path is None:
            raise RuntimeError("live run cache persistence is disabled")
        return self._state_path

    @classmethod
    def _entries_digest(cls, entries: Sequence[object]) -> str:
        return hashlib.sha256(cls._canonical_json(entries)).hexdigest()

    @staticmethod
    def _canonical_json(value: object) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def _install_browser_bridge(
    target_app: FastAPI,
    configured_settings: Settings,
    *,
    model_router: ModelRouter | None = None,
    context_builder: BudgetedAgentContextBuilder | None = None,
    memory_store: MemoryStore | None = None,
    icom_http_client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    formal_source_private_key_path: Path | None = None,
    formal_source_ledger_path: Path | None = None,
    formal_source_runtime_identity: Mapping[str, object] | None = None,
    source_terminal_reporter: Callable[
        [tuple[dict[str, Any], ...]],
        Awaitable[None],
    ]
    | None = None,
    browser_bridge_override: Any | None = None,
    icom_provider_override: Any | None = None,
    durable_browser_task_store: DurableBrowserTaskStore | None = None,
    durable_browser_tenant_id: str | None = None,
    durable_browser_tenant_partition: str | None = None,
    mount_browser_bridge: bool = True,
    formal_source_owned_by_parent: bool = False,
) -> tuple[BrowserTaskBridge | None, LivePackageAgentSystem | None]:
    formal_activation_failpoint_events: dict[str, asyncio.Event] = {}
    target_app.state.formal_activation_failpoint_events = (
        formal_activation_failpoint_events
    )
    token = configured_settings.browser_bridge_token
    if not configured_settings.browser_bridge_enabled or token is None or len(token) < 32:
        target_app.state.browser_task_bridge = None
        target_app.state.live_package_agent_system = None
        target_app.state.flexible_live_agent_system = None
        target_app.state.icom_transfer_provider = None
        target_app.state.formal_live_source_authority = None
        target_app.state.browser_bridge_token = None
        target_app.state.browser_bridge_control_token = None
        target_app.state.browser_bridge_control_enabled = False
        target_app.state.browser_companion_auto_reload_enabled = False
        target_app.state.browser_companion_runtime_agent = None
        target_app.state.browser_companion_runtime_supervisor = None
        return None, None
    commit_sha = PROVENANCE.commit_sha
    if commit_sha is None:
        raise RuntimeError("formal browser composition requires a git commit identity")
    formal_source_requested = not formal_source_owned_by_parent and (
        formal_source_private_key_path is not None
        or formal_source_ledger_path is not None
        or bool(os.environ.get("TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT"))
    )
    if formal_source_runtime_identity is not None and (
        formal_source_private_key_path is None
        or formal_source_ledger_path is None
    ):
        raise RuntimeError(
            "delegated formal source runtime requires explicit key and ledger paths"
        )
    if formal_source_owned_by_parent != (
        browser_bridge_override is not None
        and icom_provider_override is not None
        and not mount_browser_bridge
    ):
        raise RuntimeError("parent-owned formal source composition is incomplete")
    if (browser_bridge_override is not None or icom_provider_override is not None) and (
        formal_source_requested or mount_browser_bridge
    ):
        raise RuntimeError(
            "remote source overrides require an unmounted, parent-owned formal authority"
        )
    source_authority: FormalLiveSourceAuthority | None = None
    if formal_source_requested:
        authority_kwargs: dict[str, Any] = {}
        if formal_source_private_key_path is not None:
            authority_kwargs["private_key_path"] = formal_source_private_key_path
        if formal_source_ledger_path is not None:
            authority_kwargs["ledger_path"] = formal_source_ledger_path
        source_authority = load_formal_live_source_authority(
            commit_sha=commit_sha,
            # A real worker subprocess is a delegated executor of the live API
            # challenge, not an API restart. Its authenticated runtime envelope
            # carries the direct parent API's full identity; using that identity
            # lets both processes serialize events through the same protected
            # ledger without the worker falsely aborting the active challenge as
            # a cold restart. Ordinary API composition always uses its own.
            runtime_identity=(
                formal_source_runtime_identity
                if formal_source_runtime_identity is not None
                else PROVENANCE.to_dict()
            ),
            now=now,
            **authority_kwargs,
        )
    bridge = browser_bridge_override or BrowserTaskBridge(
        max_pending_tasks=512,
        now=now,
        source_authority=source_authority,
        durable_store=durable_browser_task_store,
        durable_tenant_id=durable_browser_tenant_id,
        durable_tenant_partition=durable_browser_tenant_partition,
    )
    icom_provider = icom_provider_override or IComTransferProvider(
        client=icom_http_client,
        now=now,
        source_authority=source_authority,
    )
    selected_memory_store = memory_store or MemoryStore()
    selected_context_builder = context_builder or BudgetedAgentContextBuilder(
        EvidenceRagRetriever(selected_memory_store)
    )
    # Optional model collaborators must not hold the formal live source path
    # open for many minutes when the configured contract does not require them.
    # Deterministic parsing, normalization, candidate generation, Planner and
    # Verifier remain the authority for this endpoint; required-model deployments
    # still receive the injected router unchanged.
    # A configured local model is part of the real planning route even when
    # advisory mode is allowed.  The deterministic verifier remains the gate;
    # disabling the route merely because the model is not mandatory made the
    # formal run claim model collaboration without actually calling it.
    live_model_router = model_router
    live_system = LivePackageAgentSystem(
        bridge,
        icom_provider=icom_provider,
        model_router=live_model_router,
        model_agents_required=configured_settings.model_agents_required,
        context_builder=selected_context_builder,
        memory_store=selected_memory_store,
        now=now,
        sleep=sleep,
        providers=default_browser_providers_from_registry(),
        source_terminal_reporter=source_terminal_reporter,
        official_lodging_provider=ArenaOfficialLodgingProvider(
            captured_evidence_path=os.environ.get(
                "TRIPCHORD_CAPTURED_ARENA_EVIDENCE_PATH"
            ),
            observation_dir=os.environ.get("TRIPCHORD_ARENA_OBSERVATION_DIR"),
        ),
        kaani_lodging_provider=KaaniOfficialLodgingProvider(),
    )
    flexible_system = FlexibleLiveAgentSystem(
        cast(LiveDatePairRunner, live_system),
        explorer=FlexibleDateExplorer(default_platforms_from_registry()),
        query_planner=FlexibleQueryPlanBuilder(default_platforms_from_registry()),
        minimum_departure_lead_days=7,
        model_router=live_model_router,
        model_agents_required=configured_settings.model_agents_required,
        context_builder=selected_context_builder,
        memory_store=selected_memory_store,
        now=now,
        adaptive_agent_scaling_enabled=(
            configured_settings.adaptive_agent_scaling_enabled and model_router is not None
        ),
    )
    if mount_browser_bridge:
        target_app.mount(
            _BROWSER_BRIDGE_MOUNT,
            create_browser_bridge_app(
                bridge,
                bridge_token=token,
                control_token=configured_settings.browser_bridge_control_token,
                allowed_origin_regex=configured_settings.browser_bridge_allowed_origin_regex,
                source_authority=source_authority,
                icom_provider=icom_provider,
                formal_activation_failpoint_events=formal_activation_failpoint_events,
            ),
        )
    target_app.state.browser_task_bridge = bridge
    target_app.state.live_package_agent_system = live_system
    target_app.state.flexible_live_agent_system = flexible_system
    target_app.state.icom_transfer_provider = icom_provider
    target_app.state.formal_live_source_authority = source_authority
    target_app.state.browser_bridge_token = token
    control_token = configured_settings.browser_bridge_control_token
    auto_reload_enabled = configured_settings.browser_companion_auto_reload_enabled
    runtime_agent = (
        BrowserCompanionRuntimeExecutorAgent(bridge)
        if mount_browser_bridge and (auto_reload_enabled or control_token is not None)
        else None
    )
    target_app.state.browser_bridge_control_token = control_token
    target_app.state.browser_bridge_control_enabled = (
        runtime_agent is not None and control_token is not None
    )
    target_app.state.browser_companion_auto_reload_enabled = auto_reload_enabled
    target_app.state.browser_companion_runtime_agent = runtime_agent
    target_app.state.browser_companion_runtime_supervisor = (
        BrowserCompanionRuntimeSupervisor(bridge, runtime_agent)
        if auto_reload_enabled and runtime_agent is not None
        else None
    )
    if source_authority is not None:
        source_authority.bind(
            target_app=target_app,
            bridge=bridge,
            icom_provider=icom_provider,
            live_system=live_system,
            flexible_system=flexible_system,
        )
    return bridge, live_system


def _build_model_router(
    configured_settings: Settings,
    trace_sink: InMemoryModelTraceSink,
    *,
    http_client: ModelHTTPPostClient,
) -> ModelRouter | None:
    primary_config = configured_settings.model_client_config()
    if primary_config is None:
        return None
    primary = build_model_client(
        primary_config,
        http_client=http_client,
        trace_sink=trace_sink,
    )
    fast_config = configured_settings.model_client_config(fast=True)
    fast = (
        primary
        if fast_config is None or fast_config == primary_config
        else build_model_client(
            fast_config,
            http_client=http_client,
            trace_sink=trace_sink,
        )
    )
    fast_roles = {
        AgentRole.CONTEXT,
        AgentRole.QUERY_STRATEGIST,
        AgentRole.SEARCH_SUPERVISOR,
        AgentRole.CANDIDATE_CURATOR,
        AgentRole.EXPLANATION,
        AgentRole.MEMORY_CURATOR,
    }
    return ModelRouter(
        {role: fast for role in fast_roles},
        high_risk_client=primary,
        fallback_client=primary if fast is not primary else None,
    )


def _build_memory_store(
    configured_settings: Settings,
    runtime_base: Path | None = None,
) -> MemoryStore:
    configured_path = configured_settings.memory_state_path
    if configured_path is None or not configured_path.strip():
        return MemoryStore()
    state_path = Path(configured_path).expanduser()
    if not state_path.is_absolute():
        state_path = (runtime_base or Path.cwd()) / state_path
    return PersistentMemoryStore(
        state_path,
        corruption_policy=CorruptionPolicy(configured_settings.memory_corruption_policy),
        persist_sensitive=configured_settings.memory_persist_sensitive,
    )


def _build_live_run_cache(
    configured_settings: Settings,
    runtime_base: Path | None = None,
) -> LiveRunCache:
    configured_path = configured_settings.live_run_cache_state_path
    if configured_path is None or not configured_path.strip():
        return LiveRunCache()
    state_path = Path(configured_path).expanduser()
    if not state_path.is_absolute():
        state_path = (runtime_base or Path.cwd()) / state_path
    return LiveRunCache(
        state_path=state_path,
        corruption_policy=CorruptionPolicy(configured_settings.live_run_cache_corruption_policy),
    )


logger = logging.getLogger("tripchord.api")

settings = get_settings()
configure_logging(settings.log_level)
model_trace_sink = InMemoryModelTraceSink()
model_http_runtime = ManagedModelHTTPRuntime(
    http2=settings.model_http2_enabled,
    max_connections=settings.model_http_max_connections,
    max_keepalive_connections=settings.model_http_max_keepalive_connections,
    max_in_flight=settings.model_http_max_in_flight,
    timeout_seconds=settings.model_timeout_seconds,
)
model_router = _build_model_router(
    settings,
    model_trace_sink,
    http_client=model_http_runtime.http_client,
)
memory_store = _build_memory_store(settings)
context_builder = BudgetedAgentContextBuilder(EvidenceRagRetriever(memory_store))
providers = build_provider_registry(settings)
amap = build_amap_provider(settings)
database = Database(settings.database_url)
live_planning_job_store = DurableLivePlanningJobStore(database)
live_monitor_store = DbLiveMonitorStore(database)
job_runner = PlanningJobRunner(database)
rate_limiter = RateLimiter(
    limit=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    redis_url=settings.redis_url,
)
planning_assembler = PlanningProblemAssembler(ReplayPlaceCatalog())
replan_policy = ReplanPolicySelector.from_package_data()
live_run_cache = _build_live_run_cache(settings)
package_requirement_agent = HybridPackageRequirementAgent(
    model_router=model_router,
    # When model participation is optional, a complete deterministic parse is
    # already executable.  Calling the context model again added roughly one
    # minute to the live product path without changing any locked fact.  A
    # deployment that explicitly requires model agents still keeps the call.
    skip_model_when_deterministic_ready=not settings.model_agents_required,
)
configured_job_registry_path = settings.live_planning_job_registry_state_path
if configured_job_registry_path is None and os.environ.get(
    "TRIPCHORD_FORMAL_SOURCE_TRUST_ROOT"
):
    configured_job_registry_path = str(
        formal_source_trust_root() / "live-planning-jobs.json"
    )
# C-146 P0-1: a live-job WORKER subprocess must never construct the job registry.
# It only runs the operation; the API process owns the durable registry, and a
# second instance would load (and under old-v3 migration, rewrite) the same state
# file — a concurrent second writer. The registry's worker spawner sets
# ``TRIPCHORD_LIVE_WORKER_SUBPROCESS=1`` so this import-time singleton is skipped.
_LIVE_WORKER_SUBPROCESS = os.environ.get("TRIPCHORD_LIVE_WORKER_SUBPROCESS") == "1"
live_planning_job_registry: LivePlanningJobRegistry | None
if not _LIVE_WORKER_SUBPROCESS:
    live_planning_job_registry = LivePlanningJobRegistry(
        # Durable DB is the state authority; this path is retained only for
        # authenticated subprocess marker files used by crash recovery.
        state_path=Path(configured_job_registry_path)
        if configured_job_registry_path
        else (Path(".runtime") / "live-planning-jobs.json").resolve(strict=False),
        durable_store=live_planning_job_store,
    )
else:
    live_planning_job_registry = None


async def _close_lifespan_resources(
    steps: Sequence[tuple[str, Callable[[], Awaitable[None]]]],
) -> None:
    """Run every shutdown step in order without hiding independent failures.

    A single failure is re-raised as its original exception type for backwards
    compatibility.  Multiple failures are returned as an ordered
    ``ExceptionGroup`` only after every remaining resource has had a chance to
    close.
    """

    failures: list[Exception] = []
    for resource_name, close in steps:
        try:
            await close()
        except Exception as exc:
            exc.add_note(f"TripChord lifespan resource failed to close: {resource_name}")
            failures.append(exc)
    if len(failures) == 1:
        raise failures[0]
    if failures:
        raise ExceptionGroup("multiple TripChord lifespan resources failed to close", failures)


async def _recover_live_monitors(target_app: FastAPI) -> int:
    """Rehydrate persisted ACTIVE live monitors after a process restart.

    The store is attached to whichever registry the app currently exposes
    (tests may swap the state slot with a recording resource), then ACTIVE
    records whose run context is still resolvable resume their loop while
    unrecoverable ones are marked FAILED honestly.
    """
    registry = cast(
        LiveQuoteMonitorRegistry | None,
        getattr(target_app.state, "live_quote_monitor_registry", None),
    )
    if not isinstance(registry, LiveQuoteMonitorRegistry):
        return 0
    registry.attach_store(live_monitor_store)
    cache = cast(
        LiveRunCache,
        getattr(target_app.state, "live_run_cache", live_run_cache),
    )

    async def resolvable(tenant_id: str, status: LiveMonitorStatus) -> bool:
        live_system = cast(
            LivePackageAgentSystem | None,
            getattr(target_app.state, "live_package_agent_system", None),
        )
        if live_system is None:
            return False
        entry = await cache.get(status.run_id, tenant_id)
        return entry is not None

    return await registry.recover(resolvable)


@asynccontextmanager
async def lifespan(target_app: FastAPI) -> AsyncIterator[None]:
    await database.create_schema()
    await job_runner.recover()
    await _recover_live_monitors(target_app)
    # C-146 hard-stop gate (12e35d45 门 2/门 3): actively recover durable live
    # job state on startup. The registry may have been constructed at import
    # time (loop-less), so any quarantined + pending_terminal record deferred
    # its cleanup owner; this restores the unique owner/reaper NOW, and cleans
    # real orphaned worker process groups left behind by a SIGKILLed parent API
    # — before any request can reach a durable job. Zero requests: a cold boot
    # auto-terminates; the second boot is stable with no duplicate terminalize.
    live_job_registry_startup = cast(
        LivePlanningJobRegistry | None,
        getattr(target_app.state, "live_planning_job_registry", None),
    )
    if live_job_registry_startup is not None and hasattr(
        live_job_registry_startup, "restore_after_restart"
    ):
        await live_job_registry_startup.restore_after_restart()
        if live_planning_job_store is not None:
            for tenant_id in await live_planning_job_store.list_tenants():
                await live_job_registry_startup.recover_durable(
                    tenant_id=tenant_id,
                    command_resolver=_resolve_persisted_live_worker_command,
                )
    shared_model_http = cast(
        ManagedModelHTTPRuntime | None,
        getattr(target_app.state, "model_http_runtime", None),
    )
    model_enabled = getattr(target_app.state, "model_router", None) is not None
    model_http_started = False
    durable_recovery_task: asyncio.Task[None] | None = None
    browser_completion_publisher_task: asyncio.Task[None] | None = None
    companion_supervisor = cast(
        BrowserCompanionRuntimeSupervisor | None,
        getattr(target_app.state, "browser_companion_runtime_supervisor", None),
    )

    async def durable_recovery_loop() -> None:
        while True:
            await asyncio.sleep(30)
            if live_job_registry_startup is None:
                continue
            try:
                # Re-scan authenticated worker markers before every recovery
                # claim.  A supervisor may start while an older lease is still
                # valid; claiming after expiry must first prove/stop the old
                # worker, rather than allowing a second executor to overlap it.
                await live_job_registry_startup.restore_after_restart()
                for tenant_id in await live_planning_job_store.list_tenants():
                    await live_job_registry_startup.recover_durable(
                        tenant_id=tenant_id,
                        command_resolver=_resolve_persisted_live_worker_command,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable live planning recovery sweep failed")

    async def browser_completion_publisher_loop() -> None:
        bridge = getattr(target_app.state, "browser_task_bridge", None)
        if bridge is None or not getattr(bridge, "durable_enabled", False):
            return
        while True:
            try:
                await bridge.publish_pending_completions()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("durable browser completion publication failed")
                await asyncio.sleep(5)
    try:
        if shared_model_http is not None and model_enabled:
            await shared_model_http.start()
            model_http_started = True
        if companion_supervisor is not None:
            companion_supervisor.start()
        if live_job_registry_startup is not None and live_planning_job_store is not None:
            durable_recovery_task = asyncio.create_task(
                durable_recovery_loop(), name="tripchord:durable-recovery"
            )
        bridge = getattr(target_app.state, "browser_task_bridge", None)
        if bridge is not None and getattr(bridge, "durable_enabled", False):
            browser_completion_publisher_task = asyncio.create_task(
                browser_completion_publisher_loop(),
                name="tripchord:browser-completion-publisher",
            )
        yield
    finally:
        # Cancel long-running live jobs before closing providers so cancellation
        # can invalidate any queued/claimed browser leases through the normal path.
        live_job_registry = cast(
            LivePlanningJobRegistry | None,
            getattr(target_app.state, "live_planning_job_registry", None),
        )
        # Durable live-run state must survive graceful restarts.  Process exit
        # releases the in-memory locks; restored entries receive fresh locks.
        provider = cast(
            IComTransferProvider | None,
            getattr(target_app.state, "icom_transfer_provider", None),
        )
        monitor_registry = cast(
            LiveQuoteMonitorRegistry | None,
            getattr(target_app.state, "live_quote_monitor_registry", None),
        )
        if durable_recovery_task is not None:
            durable_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await durable_recovery_task
        if browser_completion_publisher_task is not None:
            browser_completion_publisher_task.cancel()
            with suppress(asyncio.CancelledError):
                await browser_completion_publisher_task
        # Every model consumer is stopped before the shared connection pool.
        # Closing earlier can deadlock while an in-flight monitor waits on HTTP.
        shutdown_steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if companion_supervisor is not None:
            shutdown_steps.append(("browser_companion_supervisor", companion_supervisor.close))
        if live_job_registry is not None:
            shutdown_steps.append(
                (
                        "live_planning_jobs",
                        live_job_registry.suspend_for_restart
                        if live_planning_job_store is not None
                        and hasattr(live_job_registry, "suspend_for_restart")
                        else live_job_registry.close,
                )
            )
        if provider is not None:
            shutdown_steps.append(("icom_transfer_provider", provider.aclose))
        if monitor_registry is not None:
            shutdown_steps.append(("live_quote_monitors", monitor_registry.close))
        if shared_model_http is not None and model_http_started:
            shutdown_steps.append(("model_http_runtime", shared_model_http.aclose))
        shutdown_steps.extend(
            (
                ("rate_limiter", rate_limiter.close),
                ("database", database.dispose),
            )
        )
        await _close_lifespan_resources(shutdown_steps)


async def get_session() -> AsyncIterator[AsyncSession]:
    async for session in database.session():
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


def get_job_runner() -> PlanningJobRunner:
    return job_runner


RunnerDep = Annotated[PlanningJobRunner, Depends(get_job_runner)]


def get_live_package_agent_system(request: Request) -> LivePackageAgentSystem:
    host = request.client.host if request.client else None
    if not is_loopback_client(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="live browser planning accepts loopback clients only",
        )
    live_system = cast(
        LivePackageAgentSystem | None,
        getattr(request.app.state, "live_package_agent_system", None),
    )
    if live_system is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "实时浏览器核价未启用；需显式启用 browser bridge 并配置至少 32 字符的本地配对令牌"
            ),
        )
    return live_system


def get_live_run_cache(request: Request) -> LiveRunCache:
    return cast(LiveRunCache, request.app.state.live_run_cache)


def get_flexible_live_agent_system(request: Request) -> FlexibleLiveAgentSystem:
    host = request.client.host if request.client else None
    if not is_loopback_client(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="flexible live browser planning accepts loopback clients only",
        )
    return _flexible_live_agent_system_from_app(request.app)


def _flexible_live_agent_system_from_app(target_app: FastAPI) -> FlexibleLiveAgentSystem:
    flexible_system = cast(
        FlexibleLiveAgentSystem | None,
        getattr(target_app.state, "flexible_live_agent_system", None),
    )
    if flexible_system is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "灵活日期实时核价未启用；需显式启用 browser bridge 并配置至少 32 字符的本地配对令牌"
            ),
        )
    return flexible_system


def get_live_planning_job_registry(request: Request) -> LivePlanningJobRegistry:
    return cast(LivePlanningJobRegistry, request.app.state.live_planning_job_registry)


LiveSystemDep = Annotated[LivePackageAgentSystem, Depends(get_live_package_agent_system)]
FlexibleLiveSystemDep = Annotated[
    FlexibleLiveAgentSystem,
    Depends(get_flexible_live_agent_system),
]
LiveRunCacheDep = Annotated[LiveRunCache, Depends(get_live_run_cache)]
LivePlanningJobRegistryDep = Annotated[
    LivePlanningJobRegistry,
    Depends(get_live_planning_job_registry),
]

app = FastAPI(
    title="TripChord API",
    version=__version__,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.middleware("http")(observe_request)
app.state.live_run_cache = live_run_cache
app.state.package_requirement_agent = package_requirement_agent
app.state.model_router = model_router
app.state.model_http_runtime = model_http_runtime
app.state.model_trace_sink = model_trace_sink
app.state.memory_store = memory_store
app.state.context_builder = context_builder
app.state.live_planning_job_registry = live_planning_job_registry
app.state.database = database
app.state.provider_registry = build_default_registry()
app.state.handoff_store = HandoffStore()
app.state.booking_ledger_store = BookingLedgerStore()
app.state.provider_cooldown_overlay = {}
app.state.provider_adapter_registry = {}
# Inject a quote-source factory in tests/fixtures to exercise the reprice chain
# without a real OTA session; absent it, the endpoint reports live-unavailable.
app.state.reprice_quote_source_factory = None


def _load_booking_ledger_for_run(run_id: str) -> BookingLedger | None:
    """Load the append-only booking ledger for one live run, if any.

    Booking facts are keyed by the same ``run_id`` used by the live-run
    booking/acknowledge endpoints (``plan_version=run_id`` in
    ``platform/wiring_api.py``).  A run without a ledger simply has no
    protected components; event replanning then proceeds ungated.
    """
    store = getattr(app.state, "booking_ledger_store", None)
    if store is None:
        return None
    return cast(BookingLedgerStore, store).load(run_id)


app.include_router(provider_api_router)
app.include_router(wiring_api_router)
# The independent worker imports this module to reuse the production planning
# composition, but the parent API remains the sole Browser queue and formal
# signing authority.  Its authenticated runtime bundle installs the remote
# source facade after import; constructing the normal bridge here would load a
# second copy of the trust root and could abort the parent's active challenge.
initial_bridge_settings = (
    settings.model_copy(update={"browser_bridge_enabled": False})
    if _LIVE_WORKER_SUBPROCESS
    else settings
)
durable_browser_task_store = None
durable_browser_tenant_id = None
durable_browser_tenant_partition = None
if initial_bridge_settings.browser_bridge_enabled and not _LIVE_WORKER_SUBPROCESS:
    bridge_token = initial_bridge_settings.browser_bridge_token
    if bridge_token is None:
        raise RuntimeError("durable browser bridge requires a configured bridge token")
    durable_authority = hashlib.sha256(
        f"tripchord-browser-authority:{bridge_token}".encode()
    ).hexdigest()
    durable_browser_task_store = DurableBrowserTaskStore(
        database,
        authority_partition_sha256=durable_authority,
    )
    durable_browser_tenant_id = hashlib.sha256(
        f"tripchord-browser-tenant:{bridge_token}".encode()
    ).hexdigest()
    durable_browser_tenant_partition = hashlib.sha256(
        f"tripchord-browser-partition:{bridge_token}".encode()
    ).hexdigest()
browser_task_bridge, live_package_agent_system = _install_browser_bridge(
    app,
    initial_bridge_settings,
    model_router=model_router,
    context_builder=context_builder,
    memory_store=memory_store,
    durable_browser_task_store=durable_browser_task_store,
    durable_browser_tenant_id=durable_browser_tenant_id,
    durable_browser_tenant_partition=durable_browser_tenant_partition,
)


async def protect_mounted_browser_bridge(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    bridge_enabled = getattr(request.app.state, "browser_task_bridge", None) is not None
    is_bridge_path = request.url.path == _BROWSER_BRIDGE_MOUNT or request.url.path.startswith(
        f"{_BROWSER_BRIDGE_MOUNT}/"
    )
    if bridge_enabled and is_bridge_path:
        host = request.client.host if request.client else None
        if not is_loopback_client(host):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "browser bridge accepts loopback clients only"},
            )
        expected = cast(str | None, getattr(request.app.state, "browser_bridge_token", None))
        supplied = request.headers.get(BRIDGE_TOKEN_HEADER)
        if expected is None or supplied is None or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "invalid browser bridge token"},
            )
    return await call_next(request)


app.middleware("http")(protect_mounted_browser_bridge)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "tripchord", "version": __version__}


@app.get("/ready")
async def ready() -> dict[str, str]:
    try:
        async with database.sessions() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is not ready") from exc
    return {
        "status": "ready",
        "database": "ok",
        "rate_limit_backend": rate_limiter.backend,
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics_endpoint() -> str:
    return metrics.render()


@app.post("/api/v1/plans/verify", response_model=VerifyResponse)
async def verify_endpoint(request: VerifyRequest) -> VerifyResponse:
    return verify_plan(request)


@app.post("/api/v1/offers/search", response_model=OfferSearchResult)
async def offer_search_endpoint(
    query: OfferSearchQuery,
    principal: PrincipalDep,
) -> OfferSearchResult:
    await rate_limiter.check(principal.tenant_id, "offer-search")
    return await search_offers(query, providers)


@app.post("/api/v1/offers/revalidate", response_model=TravelOffer)
async def offer_revalidate_endpoint(
    offer: TravelOffer,
    principal: PrincipalDep,
) -> TravelOffer:
    await rate_limiter.check(principal.tenant_id, "offer-revalidate")
    return await revalidate_offer(offer, providers)


@app.post("/api/v1/offers/user-snapshot", response_model=TravelOffer)
async def user_quote_endpoint(
    quote: UserQuoteInput,
    principal: PrincipalDep,
) -> TravelOffer:
    await rate_limiter.check(principal.tenant_id, "user-quote")
    return create_user_quote(quote)


@app.post("/api/v1/trips/parse", response_model=RequirementParseResult)
async def parse_trip_endpoint(request: ParseTripRequest) -> RequirementParseResult:
    return parse_trip_request(request)


@app.post("/api/v1/plans/optimize", response_model=OptimizePlanResponse)
async def optimize_plan_endpoint(request: OptimizePlanRequest) -> OptimizePlanResponse:
    try:
        return optimize_plan(request)
    except PlanningInfeasible as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v1/plans/repair", response_model=WorkflowResult)
async def repair_plan_endpoint(request: RepairPlanRequest) -> WorkflowResult:
    return repair_plan(request)


@app.post("/api/v1/plans/replan", response_model=LocalReplanResult)
async def replan_endpoint(request: ReplanRequest) -> LocalReplanResult:
    return replan_after_event(request)


@app.post(
    "/api/v1/agents/plan",
    response_model=AgentPlanningResponse,
    deprecated=True,
    summary="Deprecated replay-only Agent demo",
)
@app.post(
    "/api/v1/agents/replay-plan",
    response_model=AgentPlanningResponse,
    summary="Replay-only Agent demo (never a live-model claim)",
)
async def agent_plan_endpoint(
    request: AgentPlanningRequest,
    principal: PrincipalDep,
) -> AgentPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "agent-plan")
    try:
        problem = planning_assembler.assemble(request.spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run = await build_replay_agent_system(problem).run(problem, request.preferences)
    return AgentPlanningResponse(run=run)


@app.get("/api/v1/agents/runtime", response_model=AgentRuntimeStatusResponse)
async def agent_runtime_status_endpoint(
    principal: PrincipalDep,
) -> AgentRuntimeStatusResponse:
    await rate_limiter.check(principal.tenant_id, "agent-runtime-status")
    primary = settings.model_client_config()
    fast = settings.model_client_config(fast=True)
    worker_bundle = _live_flexible_worker_runtime_bundle()
    worker_spec = worker_bundle.get("spec") if worker_bundle is not None else None
    worker_model_identity = (
        worker_spec.get("model_runtime_identity")
        if isinstance(worker_spec, dict)
        else None
    )
    companion_supervisor = cast(
        BrowserCompanionRuntimeSupervisor | None,
        getattr(app.state, "browser_companion_runtime_supervisor", None),
    )
    return AgentRuntimeStatusResponse(
        model_enabled=model_router is not None,
        model_required=settings.model_agents_required,
        model_provider=primary.provider.value if primary is not None else None,
        primary_model=primary.model if primary is not None else None,
        fast_model=fast.model if fast is not None else None,
        worker_model_runtime=(
            {
                "enabled": True,
                "required": True,
                **worker_model_identity,
                "timeout_seconds": settings.model_timeout_seconds,
                "response_format_mode": settings.model_response_format_mode,
                "thinking_mode": settings.model_thinking_mode,
                "runtime_bundle_spec_sha256": worker_bundle["spec_sha256"],
            }
            if isinstance(worker_model_identity, dict)
            and isinstance(worker_spec, dict)
            and isinstance(worker_bundle, dict)
            and worker_spec.get("model_agents_required") is True
            else None
        ),
        model_trace_count=len(model_trace_sink.records),
        effective_flexible_timeout_seconds=_flexible_total_timeout_seconds(None),
        runtime_provenance=AgentRuntimeProvenance(**PROVENANCE.to_dict()),
        formal_live_source=(
            _formal_source_public_status(
                cast(
                    FormalLiveSourceAuthority,
                    app.state.formal_live_source_authority,
                )
            )
            if getattr(app.state, "formal_live_source_authority", None) is not None
            else None
        ),
        memory_backend=(
            "single-process checksummed atomic JSON snapshot"
            if isinstance(memory_store, PersistentMemoryStore)
            else "process-local scoped MemoryStore"
        ),
        memory_persistence_enabled=isinstance(memory_store, PersistentMemoryStore),
        sensitive_memory_persisted=(
            memory_store.persists_sensitive_records
            if isinstance(memory_store, PersistentMemoryStore)
            else False
        ),
        live_run_cache_backend=(
            "single-process checksummed atomic JSON with fixed-TTL LRU"
            if live_run_cache.persistence_enabled
            else "process-local fixed-TTL LRU"
        ),
        live_run_cache_persistence_enabled=live_run_cache.persistence_enabled,
        live_run_cache_multi_worker_supported=False,
        browser_companion_control_enabled=bool(
            getattr(app.state, "browser_bridge_control_enabled", False)
        ),
        browser_companion_auto_reload_enabled=bool(
            getattr(app.state, "browser_companion_auto_reload_enabled", False)
        ),
        browser_companion_supervisor_running=(
            companion_supervisor.running if companion_supervisor is not None else False
        ),
        browser_companion_supervisor_outcome=(
            companion_supervisor.last_outcome if companion_supervisor is not None else None
        ),
        browser_companion_supervisor_attempt_count=(
            companion_supervisor.attempt_count if companion_supervisor is not None else 0
        ),
        browser_companion_last_reconcile=(
            companion_supervisor.last_reconcile_result if companion_supervisor is not None else None
        ),
    )


_FORMAL_SOURCE_CONTROL_HEADER = "X-TripChord-Formal-Source-Control"
_FORMAL_SOURCE_CONTROL_PATH: Path | None = None
_FORMAL_ACTIVATION_HEARTBEAT_TIMEOUT_SECONDS = 15.0
_FORMAL_ACTIVATION_HEARTBEAT_POLL_SECONDS = 0.05


def _formal_source_control_path() -> Path:
    configured_path = _FORMAL_SOURCE_CONTROL_PATH
    if configured_path is not None:
        return configured_path
    return formal_source_trust_root() / "control-token"


def _formal_source_public_status(
    authority: FormalLiveSourceAuthority,
) -> dict[str, object]:
    """Publish the local control-file location, never its protected content."""
    return {
        **authority.public_status(),
        "control_token_path": str(_formal_source_control_path()),
    }


def _formal_source_control_token() -> str:
    return read_owner_only_text(
        _formal_source_control_path(),
        "formal source control token",
        minimum_length=64,
    )


def _authorize_formal_source_control(
    request: Request,
    credential: str | None,
) -> FormalLiveSourceAuthority:
    host = request.client.host if request.client else None
    if not is_loopback_client(host):
        raise HTTPException(status_code=403, detail="formal source control is loopback-only")
    try:
        expected = _formal_source_control_token()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if credential is None or not hmac.compare_digest(credential, expected):
        raise HTTPException(status_code=403, detail="formal source control is unauthorized")
    authority = cast(
        FormalLiveSourceAuthority | None,
        getattr(request.app.state, "formal_live_source_authority", None),
    )
    if authority is None:
        raise HTTPException(status_code=503, detail="formal source authority is unavailable")
    return authority


@app.post("/api/v1/internal/formal-live-source/challenge")
async def issue_formal_live_source_challenge_endpoint(
    payload: dict[str, Any],
    request: Request,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    credential: Annotated[
        str | None,
        Header(alias=_FORMAL_SOURCE_CONTROL_HEADER),
    ] = None,
) -> dict[str, Any]:
    authority = _authorize_formal_source_control(request, credential)
    try:
        replay = authority.issue_replay(
            payload,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return cast(dict[str, Any], replay)
        job_graph = payload.get("job_graph")
        if not isinstance(job_graph, dict):
            raise ValueError("formal source challenge requires an exact job graph")
        job_id = job_graph.get("terminal_job_id")
        request_sha256 = job_graph.get("request_sha256")
        if not isinstance(job_id, str) or not isinstance(request_sha256, str):
            raise ValueError("formal source challenge job graph identity is invalid")
        if not await registry.is_prepared(
            job_id,
            principal.tenant_id,
            request_sha256=request_sha256,
        ):
            raise ValueError(
                "formal source challenge job is not a matching unactivated prepared job"
            )
        return cast(
            dict[str, Any],
            authority.issue_challenge(
                payload,
                idempotency_key=idempotency_key,
            ),
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/internal/formal-live-source/jobs/{job_id}/activate")
async def activate_formal_live_source_job_endpoint(
    job_id: str,
    payload: dict[str, Any],
    request: Request,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    credential: Annotated[
        str | None,
        Header(alias=_FORMAL_SOURCE_CONTROL_HEADER),
    ] = None,
) -> dict[str, Any]:
    authority = _authorize_formal_source_control(request, credential)
    try:
        if set(payload) != {"execution_capability", "companion_binding"}:
            raise ValueError("formal source activation payload is invalid")
        failpoint = os.environ.get("TRIPCHORD_TEST_FORMAL_ACTIVATION_FAILPOINT")
        failpoint_event: asyncio.Event | None = None
        if failpoint in {
            "exit_after_registry_dispatch_persist",
            "exit_after_registry_dispatch",
        }:
            events = getattr(
                request.app.state,
                "formal_activation_failpoint_events",
                None,
            )
            if not isinstance(events, dict):
                raise RuntimeError("formal activation failpoint coordinator is unavailable")
            failpoint_event = asyncio.Event()
            if events.setdefault(job_id, failpoint_event) is not failpoint_event:
                raise RuntimeError("formal activation failpoint already owns this job")
        capability = payload["execution_capability"]
        companion_binding = payload["companion_binding"]
        activation_lock = getattr(
            request.app.state,
            "formal_source_activation_lock",
            None,
        )
        if not isinstance(activation_lock, asyncio.Lock):
            activation_lock = asyncio.Lock()
            request.app.state.formal_source_activation_lock = activation_lock
        async with activation_lock:
            replay = authority.activation_replay(
                job_id=job_id,
                capability=capability,
                idempotency_key=idempotency_key,
                companion_binding=companion_binding,
            )
            if replay is not None:
                activation = authority.activation_state(
                    job_id=job_id,
                    capability=capability,
                    idempotency_key=idempotency_key,
                )
                operation_id = activation.get("operation_id")
                if isinstance(operation_id, str):
                    await registry.commit_activation(
                        job_id,
                        principal.tenant_id,
                        operation_id=operation_id,
                    )
                snapshot = await registry.get(job_id, principal.tenant_id)
                if snapshot is None or snapshot.id != job_id:
                    raise ValueError("formal source prepared job is unavailable")
                if snapshot.state == LivePlanningJobState.CANCELLED:
                    raise LivePlanningJobInactiveError(
                        "live planning job was cancelled"
                    )
                return cast(dict[str, Any], replay)
            bridge = getattr(request.app.state, "browser_task_bridge", None)
            if bridge is None:
                raise ValueError("formal source activation has no mounted Browser bridge")
            activation = authority.begin_activation(
                job_id=job_id,
                capability=capability,
                idempotency_key=idempotency_key,
                companion_binding=companion_binding,
            )
            if activation.get("phase") != "activation_ready":
                authority.require_active_job(job_id)
            deadline = (
                asyncio.get_running_loop().time()
                + _FORMAL_ACTIVATION_HEARTBEAT_TIMEOUT_SECONDS
            )
            while activation.get("phase") == "awaiting_heartbeat":
                if asyncio.get_running_loop().time() >= deadline:
                    raise ValueError(
                        "formal source activation timed out awaiting a real Companion heartbeat"
                    )
                await asyncio.sleep(_FORMAL_ACTIVATION_HEARTBEAT_POLL_SECONDS)
                activation = authority.activation_state(
                    job_id=job_id,
                    capability=capability,
                    idempotency_key=idempotency_key,
                )

            started_result = activation.get("started_result")
            if activation.get("phase") == "heartbeat_recorded":
                snapshot = await registry.get(job_id, principal.tenant_id)
                if snapshot is None:
                    raise ValueError("formal source prepared job is unavailable")
                if snapshot.state != LivePlanningJobState.QUEUED:
                    raise ValueError("formal source prepared job is not queued")
                started_result = {"job": snapshot.model_dump(mode="json")}
                activation = authority.prepare_activation_result(
                    job_id=job_id,
                    capability=capability,
                    idempotency_key=idempotency_key,
                    result=started_result,
                )
            if activation.get("phase") == "activation_ready":
                started_result = activation.get("started_result")
                if not isinstance(started_result, dict):
                    raise ValueError("formal source queued activation receipt is unavailable")
                operation_id = activation.get("operation_id")
                if not isinstance(operation_id, str):
                    raise ValueError("formal source activation operation is unavailable")
                if not isinstance(capability, dict) or not isinstance(
                    companion_binding, dict
                ):
                    raise ValueError("formal source activation identity is unavailable")
                activation_intent = {
                    "schema_version": "tripchord-live-activation-operation-v1",
                    "operation_id": operation_id,
                    "idempotency_key": idempotency_key,
                    "request_digest": activation.get("request_digest"),
                    "job_id": job_id,
                    "challenge_id": activation.get("challenge_id"),
                    "attempt_digest": activation.get("attempt_digest"),
                    "capability_sha256": activation.get("capability_sha256"),
                    "companion_identity_sha256": companion_binding.get(
                        "identity_sha256"
                    ),
                    "queued_result": started_result,
                }
                operation = await registry.prepare_activation_intent(
                    job_id,
                    principal.tenant_id,
                    intent=activation_intent,
                )
                dispatched_now = operation["phase"] == "intent"
                if operation["phase"] == "intent":
                    authority.require_active_job(job_id)
                    with authority.execution_scope(capability):
                        snapshot = await registry.activate(
                            job_id,
                            principal.tenant_id,
                            operation_id=operation_id,
                            worker_execution_capability=capability,
                        )
                else:
                    snapshot = await registry.get(job_id, principal.tenant_id)
                if snapshot is None or snapshot.id != job_id:
                    raise ValueError("formal source prepared job is unavailable")
                if snapshot.state == LivePlanningJobState.CANCELLED:
                    raise LivePlanningJobInactiveError(
                        "live planning job was cancelled before its activation completed"
                    )
                recovered_operation = await registry.activation_operation(
                    job_id,
                    principal.tenant_id,
                    operation_id=operation_id,
                )
                if recovered_operation["phase"] == "cancelled":
                    raise LivePlanningJobInactiveError(
                        "live planning activation operation was cancelled"
                    )
                if recovered_operation["phase"] not in {"dispatched", "committed"}:
                    raise ValueError("formal source activation operation was not dispatched")
                if json.dumps(
                    recovered_operation["queued_result"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ) != json.dumps(
                    started_result,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ):
                    raise ValueError("formal source activation queued receipt differs")
                if dispatched_now and failpoint_event is not None:
                    failpoint_event.set()
                    # The Companion heartbeat response owns the deferred
                    # process exit as a response background task.  Suspending
                    # here preserves the exact pre-ledger interrupt window and
                    # prevents a false successful activation response.
                    await asyncio.Event().wait()
                trust_root_mode: int | None = None
                if (
                    dispatched_now
                    and failpoint == "ledger_write_failure_after_registry_dispatch"
                ):
                    trust_root = formal_source_trust_root()
                    trust_root_mode = stat.S_IMODE(trust_root.stat().st_mode)
                    trust_root.chmod(0o500)
                try:
                    activation = authority.mark_activation_started(
                        job_id=job_id,
                        capability=capability,
                        idempotency_key=idempotency_key,
                        result=started_result,
                    )
                finally:
                    if trust_root_mode is not None:
                        formal_source_trust_root().chmod(trust_root_mode)
                await registry.commit_activation(
                    job_id,
                    principal.tenant_id,
                    operation_id=operation_id,
                )
            if activation.get("phase") == "completed":
                completed = activation.get("result")
                if not isinstance(completed, dict):
                    raise RuntimeError("formal source activation result is unavailable")
                return cast(dict[str, Any], completed)
            if activation.get("phase") != "started" or not isinstance(
                started_result, dict
            ):
                raise ValueError("formal source activation state is not recoverable")
            result = started_result
            return cast(
                dict[str, Any],
                authority.store_activation_result(
                    job_id=job_id,
                    capability=capability,
                    idempotency_key=idempotency_key,
                    result=result,
                ),
            )
    except (LivePlanningJobInactiveError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/internal/formal-live-source/finalize")
async def finalize_formal_live_source_endpoint(
    payload: dict[str, Any],
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    credential: Annotated[
        str | None,
        Header(alias=_FORMAL_SOURCE_CONTROL_HEADER),
    ] = None,
) -> dict[str, Any]:
    authority = _authorize_formal_source_control(request, credential)
    try:
        if set(payload) != {"context", "execution_capability"}:
            raise ValueError("formal source finalize payload is invalid")
        return cast(
            dict[str, Any],
            authority.finalize(
                payload["context"],
                idempotency_key=idempotency_key,
                execution_capability=payload["execution_capability"],
            ),
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("formal source finalize rejected: %s", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/internal/formal-live-source/abort")
async def abort_formal_live_source_endpoint(
    payload: dict[str, Any],
    request: Request,
    credential: Annotated[
        str | None,
        Header(alias=_FORMAL_SOURCE_CONTROL_HEADER),
    ] = None,
) -> dict[str, Any]:
    authority = _authorize_formal_source_control(request, credential)
    try:
        return cast(dict[str, Any], authority.abort(payload))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/internal/formal-live-source/expire")
async def expire_formal_live_source_endpoint(
    payload: dict[str, Any],
    request: Request,
    credential: Annotated[
        str | None,
        Header(alias=_FORMAL_SOURCE_CONTROL_HEADER),
    ] = None,
) -> dict[str, Any]:
    authority = _authorize_formal_source_control(request, credential)
    try:
        return cast(dict[str, Any], authority.expire(payload))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/api/v1/agents/browser-companion/reconcile-build",
    response_model=BrowserCompanionBuildReconcileResponse,
    summary="Run the bounded local Executor Agent build reconciliation",
)
async def reconcile_browser_companion_build_endpoint(
    payload: BrowserCompanionBuildReconcileRequest,
    request: Request,
    control_credential: Annotated[
        str | None,
        Header(alias=CONTROL_TOKEN_HEADER),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        Header(alias=IDEMPOTENCY_KEY_HEADER),
    ] = None,
) -> BrowserCompanionBuildReconcileResponse:
    host = request.client.host if request.client else None
    if not is_loopback_client(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="browser companion runtime control accepts loopback clients only",
        )
    runtime_agent = cast(
        BrowserCompanionRuntimeExecutorAgent | None,
        getattr(request.app.state, "browser_companion_runtime_agent", None),
    )
    expected_control_token = cast(
        str | None,
        getattr(request.app.state, "browser_bridge_control_token", None),
    )
    if runtime_agent is None or expected_control_token is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="browser companion runtime control is not enabled",
        )
    if control_credential is None or not hmac.compare_digest(
        control_credential,
        expected_control_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="browser companion runtime control is unauthorized",
        )
    if idempotency_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    try:
        return await runtime_agent.reconcile_build(
            payload.reason_code,
            idempotency_key=idempotency_key,
        )
    except CompanionControlInvalidIdempotencyKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key must contain 8 to 128 safe ASCII characters",
        ) from exc
    except CompanionControlIdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key was already used for a different reload reason",
        ) from exc
    except CompanionControlToolError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="browser companion reconciliation did not complete",
        ) from exc


@app.post(
    "/api/v1/agents/memory/preferences/confirm",
    response_model=ConfirmPreferenceMemoryResponse,
)
async def confirm_preference_memory_endpoint(
    request: ConfirmPreferenceMemoryRequest,
    principal: PrincipalDep,
) -> ConfirmPreferenceMemoryResponse:
    await rate_limiter.check(principal.tenant_id, "agent-memory-confirm")
    user_id = _authenticated_memory_user_id(principal)
    access = MemoryAccessContext(
        tenant_id=principal.tenant_id,
        user_id=user_id,
        agent_role=AgentRole.CONTEXT,
    )
    digest = hashlib.sha256(f"{principal.tenant_id}|{user_id}|{request.key}".encode()).hexdigest()[
        :24
    ]
    record_id = f"memory:user-preference:{digest}"
    current = memory_store.get(record_id, access)
    try:
        record = MemoryRecord(
            id=record_id,
            version=current.version + 1 if current is not None else 1,
            kind=MemoryKind.USER_PREFERENCE,
            scope=MemoryScope.USER,
            privacy=PrivacyBoundary.USER_PRIVATE,
            tenant_id=principal.tenant_id,
            user_id=user_id,
            topic="user_preference",
            subject=request.key,
            payload={
                "key": request.key,
                "value": request.value,
                "source_evidence_refs": list(request.source_evidence_refs),
            },
            source="user:explicit_memory_confirmation",
            captured_at=datetime.now(UTC),
            confidence=1,
            tags=("travel", "explicit_preference"),
            allowed_roles=(
                AgentRole.CONTEXT,
                AgentRole.QUERY_STRATEGIST,
                AgentRole.SEARCH_SUPERVISOR,
                AgentRole.CANDIDATE_CURATOR,
                AgentRole.REPAIR_STRATEGIST,
                AgentRole.ORCHESTRATOR,
            ),
            volatility=MemoryVolatility.STABLE,
            rag_eligible=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    memory_store.upsert(record)
    return ConfirmPreferenceMemoryResponse(record=record)


@app.get("/api/v1/agents/memory", response_model=AgentMemoryListResponse)
async def list_agent_memory_endpoint(
    principal: PrincipalDep,
) -> AgentMemoryListResponse:
    await rate_limiter.check(principal.tenant_id, "agent-memory-list")
    user_id = _authenticated_memory_user_id(principal)
    records = memory_store.query(
        MemoryQuery(kinds=(MemoryKind.USER_PREFERENCE,), limit=200),
        MemoryAccessContext(
            tenant_id=principal.tenant_id,
            user_id=user_id,
            agent_role=AgentRole.CONTEXT,
        ),
    )
    return AgentMemoryListResponse(records=records)


@app.delete(
    "/api/v1/agents/memory/{record_id}",
    response_model=RevokeMemoryResponse,
)
async def revoke_agent_memory_endpoint(
    record_id: str,
    principal: PrincipalDep,
) -> RevokeMemoryResponse:
    await rate_limiter.check(principal.tenant_id, "agent-memory-revoke")
    user_id = _authenticated_memory_user_id(principal)
    access = MemoryAccessContext(
        tenant_id=principal.tenant_id,
        user_id=user_id,
        agent_role=AgentRole.CONTEXT,
    )
    record = memory_store.get(record_id, access)
    if record is None or record.kind != MemoryKind.USER_PREFERENCE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory record was not found in the authenticated user boundary",
        )
    if not memory_store.delete(record_id, access):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="memory record was not found in the authenticated user boundary",
        )
    return RevokeMemoryResponse(record_id=record_id, revoked=True)


def _live_timeout_seconds(requested: int | None) -> int:
    configured = settings.browser_bridge_task_timeout_seconds
    if not 30 <= configured <= 300:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="browser bridge task timeout must be between 30 and 300 seconds",
        )
    return configured if requested is None else min(requested, configured)


def _flexible_total_timeout_seconds(requested: int | None) -> int:
    configured = settings.browser_bridge_flexible_timeout_seconds
    if not 60 <= configured <= 3600:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="flexible browser planning timeout must be between 60 and 3600 seconds",
        )
    product_cap_seconds = 600
    return min(configured, product_cap_seconds) if requested is None else min(
        requested,
        configured,
        product_cap_seconds,
    )


def _authenticated_memory_user_id(principal: Principal) -> str:
    if principal.auth_mode == "development-anonymous":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "anonymous development principals cannot create, list, or revoke "
                "durable user memory"
            ),
        )
    return principal.tenant_id


def _memory_access(principal: Principal, trip_id: str) -> MemoryAccessContext:
    # Materialize only deterministic registry facts.  This makes
    # provider_capability a real production RAG source instead of a test-only
    # enum/claim, while remaining tenant-isolated and free of live prices.
    seed_provider_capability_records(
        memory_store,
        tenant_id=principal.tenant_id,
        seeds=_PROVIDER_CAPABILITY_SEEDS,
    )
    return MemoryAccessContext(
        tenant_id=principal.tenant_id,
        # There is no stable user subject in development-anonymous mode.  Using
        # the shared literal "anonymous" would mix long-term preferences across
        # people, so user/trip/session memory is disabled for that principal.
        user_id=(None if principal.auth_mode == "development-anonymous" else principal.tenant_id),
        session_id=trip_id,
        trip_id=trip_id,
        agent_role=AgentRole.CONTEXT,
    )


def _require_final_published_live_run_for_event(run: LivePackageAgentRun) -> None:
    """Reject exploration-only snapshots before any event-side state change.

    An exploration seal proves only that the run is suitable for selecting a
    date/package scope.  Reusing that seal after an event repair would make a
    changed candidate look as if it had already passed the publication tail.
    """

    if (
        run.run_purpose != LiveRunPurpose.FINAL_PUBLICATION
        or run.finalization_state != LiveFinalizationState.FINAL_PUBLISHED
    ):
        raise ValueError(
            "event replanning requires a final-published live run; "
            "exploration-only runs must complete publication refresh first"
        )


def _advance_cached_live_run(
    previous: LivePackageAgentRun,
    replanned: LiveEventReplanRun,
) -> LivePackageAgentRun:
    _require_final_published_live_run_for_event(previous)
    if replanned.global_run is not None:
        # A global event replan is a complete new evidence/Agent run.  Reusing
        # any field from the old snapshot would make later events reason over a
        # mixed generation, so replace the cache atomically with the new run.
        _require_final_published_live_run_for_event(replanned.global_run)
        return replanned.global_run
    normalization_results = (
        *previous.normalization_results,
        *replanned.normalization_results,
    )[-_MAX_CACHED_NORMALIZATION_RESULTS:]
    if replanned.event.affected_provider == LiveDataProvider.ICOM_PUBLIC_TRANSFER:
        source_task_ids = previous.source_task_ids
        public_transfer_task_ids = (
            *previous.public_transfer_task_ids,
            *replanned.source_task_ids,
        )[-_MAX_CACHED_SOURCE_TASK_IDS:]
    else:
        source_task_ids = (
            *previous.source_task_ids,
            *replanned.source_task_ids,
        )[-_MAX_CACHED_SOURCE_TASK_IDS:]
        public_transfer_task_ids = previous.public_transfer_task_ids
    combined_agentic = AgenticRunSummary.combine((previous.agentic, replanned.agentic))
    return previous.model_copy(
        update={
            "decision": replanned.decision,
            "claim_boundary": replanned.claim_boundary,
            "inventory": replanned.inventory,
            "normalization_results": normalization_results,
            "package": replanned.package if replanned.package is not None else previous.package,
            "source_task_ids": source_task_ids,
            "public_transfer_task_ids": public_transfer_task_ids,
            "agentic": combined_agentic,
            # The old natural-language explanation and memory suggestions refer
            # to a superseded candidate.  Clear them instead of serving stale
            # model output after a local component repair.
            "explanation": None,
            "memory_candidates": None,
        }
    )


def _maximum_safe_live_monitor_interval_seconds(
    run: LivePackageAgentRun,
    *,
    now: datetime | None = None,
) -> int | None:
    """Bound a round-robin interval by the oldest component's freshness horizon."""

    package = run.package
    if package is None:
        return None
    candidate = package.final_candidate
    supported_values = {item.value for item in LiveDataProvider}
    components = tuple(
        item
        for item in (
            candidate.flight,
            *candidate.lodgings,
            *candidate.transfers,
        )
        if item.provider in supported_values
    )
    if not components:
        return None
    reference = now or datetime.now(UTC)
    freshness_seconds = int(
        (min(item.expires_at for item in components) - reference).total_seconds()
    )
    usable_seconds = freshness_seconds - _LIVE_MONITOR_FRESHNESS_SAFETY_SECONDS
    if usable_seconds <= 0:
        return 0
    return usable_seconds // len(components)


async def _perform_live_monitor_check(
    monitor: LiveMonitorStatus,
    tenant_id: str,
) -> LiveMonitorCheck:
    live_system = cast(
        LivePackageAgentSystem | None,
        getattr(app.state, "live_package_agent_system", None),
    )
    if live_system is None:
        raise RuntimeError("live browser planning is not enabled")
    # Resolve the same cache instance exposed through the FastAPI dependency.
    # Tests and embedded deployments may intentionally replace app.state while
    # the module-level default remains unchanged; mixing those instances would
    # make a monitor report a spurious expired run.
    cache = cast(
        LiveRunCache,
        getattr(app.state, "live_run_cache", live_run_cache),
    )
    entry = await cache.get(monitor.run_id, tenant_id)
    if entry is None:
        raise RuntimeError("live planning run expired before periodic revalidation")
    async with entry.lock:
        current = await cache.get(monitor.run_id, tenant_id)
        if current is not entry:
            raise RuntimeError("live planning run changed or expired before revalidation")
        _require_final_published_live_run_for_event(entry.run)
        package = entry.run.package
        if package is None or entry.run.decision.state != PackageDecisionState.ACCEPT:
            raise RuntimeError("periodic revalidation requires an accepted package candidate")
        safe_interval = _maximum_safe_live_monitor_interval_seconds(entry.run)
        if safe_interval is None or monitor.interval_seconds > safe_interval:
            raise RuntimeError(
                "periodic revalidation can no longer refresh every component before "
                "the package freshness horizon; run a fresh global search"
            )
        candidate = package.final_candidate
        supported_values = {item.value for item in LiveDataProvider}
        components = tuple(
            item
            for item in (
                candidate.flight,
                *candidate.lodgings,
                *candidate.transfers,
            )
            if item.provider in supported_values
        )
        if not components:
            raise RuntimeError("current package has no requery-capable live component")
        target = components[monitor.check_count % len(components)]
        event = LivePackageEvent(
            id=f"monitor-event-{secrets.token_urlsafe(16)}",
            kind=PackageEventKind.PRICE_CHANGED,
            target_component_id=target.id,
            affected_provider=LiveDataProvider(target.provider),
            occurred_at=datetime.now(UTC),
            source="tripchord-opt-in-periodic-revalidation",
        )
        replanned = await live_system.replan_after_event(
            entry.run,
            event,
            timeout_seconds=monitor.timeout_seconds,
            memory_access=MemoryAccessContext(
                tenant_id=tenant_id,
                user_id=None,
                session_id=entry.run.intent.trip_id,
                trip_id=entry.run.intent.trip_id,
            ),
            booking_ledger=_load_booking_ledger_for_run(monitor.run_id),
        )
        updated = _advance_cached_live_run(entry.run, replanned)
        expires_at = await cache.replace(
            monitor.run_id,
            tenant_id,
            entry,
            updated,
        )
        if expires_at is None:
            raise RuntimeError("live planning run expired during periodic revalidation")
    updated_package = updated.package
    changed = bool(
        updated_package is not None
        and updated_package.final_candidate.component_ids != candidate.component_ids
    )
    return LiveMonitorCheck(
        sequence=monitor.check_count + 1,
        checked_at=datetime.now(UTC),
        target_component_id=target.id,
        event_id=event.id,
        applied_disposition=(
            replanned.applied_disposition.value
            if replanned.applied_disposition is not None
            else None
        ),
        decision_state=replanned.decision.state.value,
        package_changed=changed,
        summary=replanned.decision.summary,
    )


live_quote_monitor_registry = LiveQuoteMonitorRegistry(_perform_live_monitor_check)
app.state.live_quote_monitor_registry = live_quote_monitor_registry


async def _persist_search_run(
    tenant_id: str,
    run: LivePackageAgentRun,
) -> SearchRun | None:
    """Persist one completed live run as a :class:`SearchRun` (v0.3).

    Persistence is best-effort at the storage boundary: the live planning
    response must not be held hostage by an unavailable/migrating database, but
    any actual write failure is surfaced through ``logger.warning`` rather than
    silently swallowed.  When the database is ready the run is persisted
    tenant-scoped and recoverable via :class:`SearchRunRepository`.
    """
    from tripchord.persistence.search_runs import SearchRunRepository

    built = build_search_run(run=run)
    try:
        async with database.sessions() as session:
            repository = SearchRunRepository(session, tenant_id=tenant_id)
            await repository.save(built)
    except Exception as exc:  # pragma: no cover - storage-boundary guard
        logger.warning(
            "search run persistence failed for run_id=%s: %s: %s",
            built.run_id,
            type(exc).__name__,
            redact_secrets(str(exc)),
        )
        return None
    return built


def _source_terminal_events_from_run(
    run: FlexibleLiveAgentRun,
    now: datetime,
) -> tuple[LiveSourceTerminalEvent, ...]:
    """Reduce the flexible run's pair coverage into typed source events.

    Each event records one source task's typed terminal state.  Only events for
    sources that actually reached a terminal outcome are emitted; running or
    skipped sources stay out so the pre-barrier SSE stream never leaks a
    ``quote_found`` that did not really happen.
    """
    events: dict[str, LiveSourceTerminalEvent] = {}
    for execution in run.pair_runs:
        pair_runs = [
            item
            for item in (execution.exploration_run, execution.run)
            if item is not None
        ]
        if len(pair_runs) == 2 and pair_runs[0] is pair_runs[1]:
            pair_runs.pop()
        for live_run in pair_runs:
            usable = set(
                live_run.source_execution_completeness.terminal_source_ids or ()
            )
            for item in live_run.coverage:
                terminal_source_ids = tuple(
                    dict.fromkeys(
                        (
                            *item.terminal_outcome_source_ids,
                            *item.successful_source_ids,
                            *item.failed_source_ids,
                        )
                    )
                )
                for source_id in terminal_source_ids:
                    scope = derive_scope_from_task_id(source_id)
                    if scope is None:
                        continue
                    if item.failed_source_ids and source_id in item.failed_source_ids:
                        terminal_state = _terminal_state_from_reasons(
                            item.failure_reasons
                        )
                    elif source_id in usable and source_id in (
                        item.usable_quote_source_ids or item.successful_source_ids
                    ):
                        terminal_state = "quote_found"
                    elif source_id in usable:
                        terminal_state = "bounded_no_exact_quote"
                    else:
                        continue
                    events[source_id] = LiveSourceTerminalEvent(
                        source_task_id=source_id,
                        provider=scope.provider,
                        vertical=scope.vertical.value,
                        terminal_state=terminal_state,
                        occurred_at=now,
                    )
    return tuple(events.values())


def _terminal_state_from_reasons(reasons: tuple[str, ...]) -> str:
    joined = " ".join(reasons).lower()
    if "login" in joined or "login_required" in joined:
        return "login_required"
    if "captcha" in joined:
        return "captcha_required"
    if "dom_drift" in joined or "dom drift" in joined:
        return "dom_drift"
    if "timed_out" in joined or "timeout" in joined:
        return "timed_out"
    if "cancelled" in joined:
        return "cancelled"
    return "provider_error"


async def _cache_flexible_pair_runs(
    run: FlexibleLiveAgentRun,
    cache: LiveRunCache,
    tenant_id: str,
    *,
    ensure_active: Callable[[], Awaitable[None]] | None = None,
    search_run_recorder: Callable[[LivePackageAgentRun], Awaitable[SearchRun | None]] | None = None,
) -> tuple[LiveFlexiblePairRunHandle, ...]:
    if ensure_active is not None:
        await ensure_active()
    handles: list[LiveFlexiblePairRunHandle] = []
    final_projection = build_final_plan_projection(run)
    selected_pair_id = final_projection.date_pair_id if final_projection is not None else None
    if selected_pair_id is None:
        return ()
    for pair_run in run.pair_runs:
        if pair_run.run is None:
            continue
        # Exploration remains in the durable run/result, but the short-lived
        # re-planning cache receives only the one published final option.
        if selected_pair_id is not None and pair_run.date_pair.id != selected_pair_id:
            continue
        if ensure_active is not None:
            await ensure_active()
        run_id, expires_at = await cache.put(tenant_id, pair_run.run)
        if search_run_recorder is not None:
            await search_run_recorder(pair_run.run)
        handles.append(
            LiveFlexiblePairRunHandle(
                date_pair_id=pair_run.date_pair.id,
                run_id=run_id,
                expires_at=expires_at,
            )
        )
    return tuple(handles)


@app.post(
    "/api/v1/agents/live-flexible-plan",
    response_model=LiveFlexibleAgentPlanningResponse,
)
async def live_flexible_agent_plan_endpoint(
    request: LiveFlexibleAgentPlanningRequest,
    http_request: Request,
    flexible_system: FlexibleLiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveFlexibleAgentPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "live-flexible-agent-plan")
    await guard_live_start(
        http_request,
        (ProviderVertical.FLIGHT, ProviderVertical.LODGING),
    )
    if (
        settings.browser_bridge_require_all_providers
        and request.coverage_mode != LiveCoverageMode.STRICT
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="server policy requires strict full-coverage mode across selected scopes",
        )
    pair_timeout_seconds = _live_timeout_seconds(request.timeout_seconds)
    total_timeout_seconds = _flexible_total_timeout_seconds(request.total_timeout_seconds)
    try:
        async with asyncio.timeout(total_timeout_seconds):
            if isinstance(flexible_system, FlexibleLiveAgentSystem):
                run = await flexible_system.run(
                    request.window,
                    request.calendars,
                    mode=request.coverage_mode,
                    max_pairs=request.max_pairs,
                    constraints=request.constraints,
                    timeout_seconds=pair_timeout_seconds,
                    total_timeout_seconds=total_timeout_seconds,
                    stay_plan_candidate_set=request.stay_plan_candidate_set,
                    publication_refresh_minimum_options=(
                        request.publication_refresh_minimum_options
                    ),
                    memory_access=_memory_access(
                        principal,
                        "flexible:"
                        f"{request.window.origin}:{request.window.destination}:"
                        f"{request.window.earliest_departure.isoformat()}",
                    ),
                )
            else:
                run = await flexible_system.run(
                    request.window,
                    request.calendars,
                    mode=request.coverage_mode,
                    max_pairs=request.max_pairs,
                    constraints=request.constraints,
                    timeout_seconds=pair_timeout_seconds,
                    stay_plan_candidate_set=request.stay_plan_candidate_set,
                )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=("flexible live planning exceeded the configured total request timeout"),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    run = await _attach_flexible_pair_icom_reference_estimates(run)
    handles = await _cache_flexible_pair_runs(run, cache, principal.tenant_id)
    return LiveFlexibleAgentPlanningResponse(
        run=run,
        final_plan=build_final_plan_projection(run),
        cached_pair_runs=handles,
    )


async def _preflight_live_flexible_from_text(
    payload: LiveFlexibleFromTextPlanningRequest,
    http_request: Request,
    principal: Principal,
    *,
    rate_limit_bucket: str,
) -> None:
    host = http_request.client.host if http_request.client else None
    if not is_loopback_client(host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="natural-language live browser planning accepts loopback clients only",
        )
    await rate_limiter.check(principal.tenant_id, rate_limit_bucket)
    if (
        settings.browser_bridge_require_all_providers
        and payload.coverage_mode != LiveCoverageMode.STRICT
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="server policy requires strict full-coverage mode across selected scopes",
        )


def _live_flexible_from_text_request_sha256(
    payload: LiveFlexibleFromTextPlanningRequest,
) -> str:
    """Hash the normalized API payload, not a runner scenario or raw user text."""

    return hashlib.sha256(
        json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _resolve_persisted_live_worker_command(spec: dict[str, Any]) -> LiveJobWorkerCommand:
    allowed_module = str(Path(live_flexible_from_text_worker.__file__).resolve())
    if spec.get("kind") != "worker_command" or spec.get("module_path") != allowed_module:
        raise RuntimeError("persisted live worker command is not allowlisted")
    if spec.get("entry") != "run_live_flexible_from_text":
        raise RuntimeError("persisted live worker entry is not allowlisted")
    persisted = spec.get("args")
    if not isinstance(persisted, dict):
        raise RuntimeError("persisted live worker arguments are invalid")
    payload_raw = persisted.get("payload")
    request_digest = persisted.get("request_digest")
    tenant_id = persisted.get("tenant_id")
    if not isinstance(payload_raw, dict) or not isinstance(request_digest, str):
        raise RuntimeError("persisted live worker request envelope is invalid")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise RuntimeError("persisted live worker tenant is invalid")
    payload = LiveFlexibleFromTextPlanningRequest.model_validate(payload_raw)
    if _live_flexible_from_text_request_sha256(payload) != request_digest:
        raise RuntimeError("persisted live worker request digest does not match payload")
    args: dict[str, Any] = {
        "payload": payload.model_dump(mode="json"),
        "request_digest": request_digest,
        "tenant_id": tenant_id,
    }
    # Runtime credentials are deliberately regenerated from current process
    # settings.  They are never read from the durable command spec.
    runtime_bundle = _live_flexible_worker_runtime_bundle()
    if runtime_bundle is not None:
        args["runtime_bundle"] = runtime_bundle
    return LiveJobWorkerCommand(
        module_path=allowed_module,
        entry="run_live_flexible_from_text",
        args=dict(args),
        result_importer=_build_live_worker_result_importer(
            cache=live_run_cache, tenant_id=tenant_id
        ),
    )


async def _execute_live_flexible_from_text_body(
    payload: LiveFlexibleFromTextPlanningRequest,
    *,
    target_app: FastAPI,
    cache: LiveRunCache,
    principal: Principal,
    model_trace_scope_sha256: str,
    report_progress: LiveJobProgressReporter | None = None,
    report_pair_checkpoint: PairCheckpointReporter | None = None,
    checkpoint_request_sha256: str | None = None,
    recovered_pair_executions: tuple[FlexiblePairExecution, ...] = (),
    pair_execution_reporter: Callable[[FlexiblePairExecution], Awaitable[None]] | None = None,
) -> LiveFlexibleFromTextPlanningResponse:
    async def report(stage: str, progress: int) -> None:
        if report_progress is not None:
            await report_progress(stage, progress)

    await report("interpreting_requirement", 10)
    requirement_agent = cast(
        HybridPackageRequirementAgent,
        target_app.state.package_requirement_agent,
    )
    interpretation = await requirement_agent.parse(payload.requirement)
    # Durable preferences are loaded only for the authenticated user and are
    # merged after current-trip parsing.  The domain constitution gives
    # explicit current text precedence over long-term memory; model-inferred
    # preferences never become durable and cannot silently override either.
    durable_preferences = confirmed_preference_constitution(
        memory_store,
        _memory_access(principal, "preference-context"),
    )
    if durable_preferences.rules:
        interpretation = interpretation.model_copy(
            update={
                "preferences": durable_preferences.merged_for_trip(
                    current=interpretation.preferences
                )
            }
        )
    model_enabled = getattr(target_app.state, "model_router", None) is not None
    if interpretation.state == PackageRequestState.HUMAN_BLOCK:
        await report("blocked_before_live_search", 95)
        return LiveFlexibleFromTextPlanningResponse(
            interpretation=interpretation,
            final_plan=None,
            model_enhancement_enabled=model_enabled,
            model_trace_scope_sha256=model_trace_scope_sha256,
            model_trace_count=0,
            model_trace_success_count=0,
            model_trace_failure_count=0,
            execution_boundary=(
                "需求 Agent 已调用真实模型提案，但确定性对账仍发现"
                "关键字段缺失或冲突，因此在浏览器搜索前阻塞。"
                if model_enabled
                else LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY
            ),
        )

    window = interpretation.window
    intent_template = interpretation.intent_template
    if window is None or intent_template is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ready requirement interpretation did not provide executable constraints",
        )
    # The parser builds the template before durable memory is loaded.  Project
    # the already-resolved effective constitution now, so long-term rules are
    # executable while current-trip explicit rules retain their precedence.
    projected_template, unapplied_keys = project_preferences_to_intent_template(
        intent_template,
        interpretation.preferences,
    )
    if projected_template != intent_template or unapplied_keys:
        diagnostics = list(interpretation.unresolved)
        existing_fields = {item.field for item in diagnostics}
        for key in unapplied_keys:
            field = f"preference_application:{key}"
            if field not in existing_fields:
                diagnostics.append(
                    UnresolvedRequirement(
                        field=field,
                        reason=(
                            "该 typed 偏好已保留，但当前执行链没有对应的意图字段；"
                            "不会影响候选排序或硬约束。"
                        ),
                        critical=False,
                    )
                )
        interpretation = interpretation.model_copy(
            update={
                "intent_template": projected_template,
                "unresolved": tuple(diagnostics),
            }
        )
        intent_template = projected_template
    # C-122 R44 (canonical pair-set authority): the frozen gateway scenario must
    # explore the FROZEN window — the interpreter reads "玩5-8天" as a 4-7-night
    # window, which would seal non-canonical (generic) pair ids and break the
    # canonical ordered trio shared by producer, compact and consumer.  Only when
    # the client EXPLICITLY supplies the system-frozen candidate set AND the
    # interpretation keeps the frozen city identity is the window pinned to the
    # frozen one; every other run keeps its own interpreted window.
    frozen_window = frozen_v4_window_for_run(window, payload.stay_plan_candidate_set)
    if frozen_window is not None:
        window = frozen_window
    flexible_system = _flexible_live_agent_system_from_app(target_app)
    constraints = FlexiblePackageConstraints(
        budget_cents=intent_template.budget_cents,
        require_checked_baggage=intent_template.require_checked_baggage,
        allow_connections=intent_template.allow_connections,
        require_breakfast=intent_template.require_breakfast,
        require_non_basic_lodging=intent_template.require_non_basic_lodging,
        require_non_remote_lodging=intent_template.require_non_remote_lodging,
        breakfast_preference_mode=intent_template.breakfast_preference_mode,
        breakfast_preference_weight=intent_template.breakfast_preference_weight,
        minimum_arrival_to_boat_minutes=(intent_template.minimum_arrival_to_boat_minutes),
        minimum_airport_buffer_minutes=intent_template.minimum_airport_buffer_minutes,
    )
    pair_timeout_seconds = _live_timeout_seconds(payload.timeout_seconds)
    total_timeout_seconds = _flexible_total_timeout_seconds(payload.total_timeout_seconds)
    stay_plan_candidate_set = payload.stay_plan_candidate_set
    stay_area_profile = system_stay_area_search_profile(window.destination)
    if stay_area_profile is None and window.destination_code == "MLE":
        # The user-facing parser correctly preserves "马尔代夫" as the
        # destination phrase.  Execution still needs the audited MALÉ gateway
        # profile so lodging tasks are bound to Maafushi/Hulhumalé rather than
        # a meaningless country-wide hotel search.
        stay_area_profile = system_stay_area_search_profile("马累")
    if stay_area_profile is not None:
        if window.destination != stay_area_profile.gateway_destination:
            window = window.model_copy(
                update={"destination": stay_area_profile.gateway_destination}
            )
        if stay_plan_candidate_set is None:
            # Natural-language users should not need to know or submit an
            # internal frozen-candidate schema.  For an audited gateway profile,
            # choose the system set before any provider result is observed.
            stay_plan_candidate_set = system_stay_plan_candidate_set(
                stay_area_profile.gateway_destination
            )
    await report("searching_live_sources", 25)
    try:
        async with asyncio.timeout(total_timeout_seconds):
            if isinstance(flexible_system, FlexibleLiveAgentSystem):
                # C-122 R44: a frozen scenario carries its committed
                # reference_date; pin the run clock to it when the client
                # explicitly set it so the sealed ordered trio is independently
                # reproducible (same derivation as the producer/consumer checks).
                pinned_reference_date = (
                    payload.requirement.reference_date
                    if "reference_date" in payload.requirement.model_fields_set
                    else None
                )
                run = await flexible_system.run(
                    window,
                    payload.calendars,
                    mode=payload.coverage_mode,
                    max_pairs=payload.max_pairs,
                    constraints=constraints,
                    timeout_seconds=pair_timeout_seconds,
                    total_timeout_seconds=total_timeout_seconds,
                    stay_plan_candidate_set=stay_plan_candidate_set,
                    publication_refresh_minimum_options=(
                        payload.publication_refresh_minimum_options
                    ),
                    memory_access=_memory_access(
                        principal,
                        "flexible:"
                        f"{window.origin}:{window.destination}:"
                        f"{window.earliest_departure.isoformat()}",
                    ),
                    pair_checkpoint_reporter=report_pair_checkpoint,
                    checkpoint_request_sha256=checkpoint_request_sha256,
                    reference_date=pinned_reference_date,
                    recovered_pair_executions=recovered_pair_executions,
                    pair_execution_reporter=pair_execution_reporter,
                )
            else:
                run = await flexible_system.run(
                    window,
                    payload.calendars,
                    mode=payload.coverage_mode,
                    max_pairs=payload.max_pairs,
                    constraints=constraints,
                    timeout_seconds=pair_timeout_seconds,
                    stay_plan_candidate_set=stay_plan_candidate_set,
                )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="natural-language flexible planning exceeded the configured total timeout",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    run = await _attach_flexible_pair_icom_reference_estimates(run)
    await report("caching_pair_runs", 90)
    if report_progress is not None:
        barrier_released_at = datetime.now(UTC)
        events = _source_terminal_events_from_run(run, barrier_released_at)
        if events:
            await report_progress.report_source_terminal_events(events)
        await report_progress.report_barrier_released(barrier_released_at)
    handles = await _cache_flexible_pair_runs(
        run,
        cache,
        principal.tenant_id,
        ensure_active=(report_progress.ensure_active if report_progress is not None else None),
        search_run_recorder=(lambda live_run: _persist_search_run(principal.tenant_id, live_run)),
    )
    model_summaries = [run.query_agentic]
    model_summaries.extend(
        pair.run.agentic
        for pair in run.pair_runs
        if pair.run is not None
    )
    model_trace_count = sum(item.logical_request_count for item in model_summaries)
    model_trace_failure_count = sum(
        stage.logical_request_count
        for summary in model_summaries
        for stage in summary.stages
        if stage.failure is not None
    )
    model_trace_success_count = model_trace_count - model_trace_failure_count
    applied_roles = sorted(
        {
            str(item["role"])
            for pair in run.pair_runs
            for live_run in (pair.exploration_run, pair.run)
            if live_run is not None
            for item in live_run.model_applied_diffs
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        }
    )
    execution_boundary = LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY
    if model_enabled:
        execution_boundary = (
            f"本次实际发起 {model_trace_count} 次模型逻辑调用，"
            f"成功 {model_trace_success_count} 次，"
            f"失败 {model_trace_failure_count} 次；实际改变结果的角色："
            f"{','.join(applied_roles) or '无（模型提案未改变确定性选择）'}。"
            "模型只提交受限提案，候选、金额、权限和硬约束仍由确定性代码核验。"
        )
    if run.stay_area_search_profile is not None:
        assumption = run.stay_area_search_profile.assumption_zh
        interpretation = interpretation.model_copy(
            update={"claim_boundary": f"{interpretation.claim_boundary}{assumption}"}
        )
        execution_boundary = f"{execution_boundary}{assumption}"
    await report("assembling_result", 95)
    return LiveFlexibleFromTextPlanningResponse(
        interpretation=interpretation,
        run=run,
        final_plan=build_final_plan_projection(run),
        best_available_plan=build_best_available_plan_projection(run),
        decision_candidates=tuple(
            candidate
            for pair in run.pair_runs
            for live_run in (pair.run or pair.exploration_run,)
            if live_run is not None
            for candidate in _decision_candidate_projections(live_run)
        ),
        cached_pair_runs=handles,
        model_enhancement_enabled=model_enabled,
        model_trace_scope_sha256=model_trace_scope_sha256,
        model_trace_count=model_trace_count,
        model_trace_success_count=model_trace_success_count,
        model_trace_failure_count=model_trace_failure_count,
        execution_boundary=execution_boundary,
    )


@request_agent_budgeted
async def _execute_live_flexible_from_text(
    payload: LiveFlexibleFromTextPlanningRequest,
    *,
    target_app: FastAPI,
    cache: LiveRunCache,
    principal: Principal,
    report_progress: LiveJobProgressReporter | None = None,
    report_pair_checkpoint: PairCheckpointReporter | None = None,
    expected_request_sha256: str | None = None,
    model_trace_scope_id: str | None = None,
    report_model_trace_summary: Callable[[str, str, int, int, int], Awaitable[None]] | None = None,
    recovered_pair_executions: tuple[FlexiblePairExecution, ...] = (),
    pair_execution_reporter: Callable[[FlexiblePairExecution], Awaitable[None]] | None = None,
) -> LiveFlexibleFromTextPlanningResponse:
    request_sha256 = _live_flexible_from_text_request_sha256(payload)
    if expected_request_sha256 is not None and not hmac.compare_digest(
        expected_request_sha256,
        request_sha256,
    ):
        raise ValueError("live planning request SHA-256 changed after job admission")
    trace_sink = cast(
        InMemoryModelTraceSink,
        target_app.state.model_trace_sink,
    )
    with trace_sink.trace_scope(
        request_sha256,
        scope_id=model_trace_scope_id,
    ) as trace_scope:
        try:
            response = await _execute_live_flexible_from_text_body(
                payload,
                target_app=target_app,
                cache=cache,
                principal=principal,
                model_trace_scope_sha256=request_sha256,
                report_progress=report_progress,
                report_pair_checkpoint=report_pair_checkpoint,
                checkpoint_request_sha256=(
                    request_sha256 if report_pair_checkpoint is not None else None
                ),
                recovered_pair_executions=recovered_pair_executions,
                pair_execution_reporter=pair_execution_reporter,
            )
        finally:
            trace_summary = trace_sink.scope_summary(trace_scope)
            if report_model_trace_summary is not None:
                await report_model_trace_summary(
                    trace_summary.scope_id,
                    trace_summary.scope_request_digest,
                    trace_summary.trace_count,
                    trace_summary.success_count,
                    trace_summary.failure_count,
                )
        return response.model_copy(
            update={
                "model_trace_count": trace_summary.trace_count,
                "model_trace_success_count": trace_summary.success_count,
                "model_trace_failure_count": trace_summary.failure_count,
            }
        )


@app.post(
    "/api/v1/agents/live-flexible-plan-from-text",
    response_model=LiveFlexibleFromTextPlanningResponse,
)
async def live_flexible_agent_plan_from_text_endpoint(
    payload: LiveFlexibleFromTextPlanningRequest,
    http_request: Request,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveFlexibleFromTextPlanningResponse:
    await _preflight_live_flexible_from_text(
        payload,
        http_request,
        principal,
        rate_limit_bucket="live-flexible-agent-plan-from-text",
    )
    return await _execute_live_flexible_from_text(
        payload,
        target_app=http_request.app,
        cache=cache,
        principal=principal,
    )


def _live_flexible_worker_runtime_bundle() -> dict[str, Any] | None:
    """The worker runtime bundle configured for this API process, if any.

    C-146 P0-1 (RETURN 7de8cf3e): a JSON spec in
    ``TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE`` configures what runtime the
    worker subprocess builds for the ready chain. Read at command-build time and
    bind its canonical digest to this API process's immutable code provenance;
    the worker recomputes both before it creates any capability. An absent value
    keeps the reconstructed app's default; malformed/foreign configured values
    fail closed instead of silently dropping the production runtime.
    """
    raw = os.environ.get("TRIPCHORD_LIVE_FLEXIBLE_WORKER_RUNTIME_BUNDLE")
    if raw is None:
        authority = getattr(app.state, "formal_live_source_authority", None)
        if authority is None:
            return None
        parent_origin = os.environ.get(
            "TRIPCHORD_LIVE_WORKER_PARENT_API_ORIGIN"
        )
        if not parent_origin:
            # Keep ordinary runtime/status and cold-restart diagnostics
            # available.  A strict model-required gate sees no worker runtime
            # and fails before live search; the controlled launcher supplies
            # the exact loopback origin for production execution.
            return None
        primary = settings.model_client_config()
        fast = settings.model_client_config(fast=True) or primary
        if primary is None or fast is None or model_router is None:
            raise ValueError("formal live worker model runtime is unavailable")
        companion_token = settings.browser_bridge_token
        if not isinstance(companion_token, str):
            raise ValueError("formal live worker source token is unavailable")
        parsed = {
            "runtime": "browser-bridge",
            # A worker receives only the one-way, domain-separated source
            # credential.  It cannot authenticate to Companion heartbeat,
            # claim, or completion routes with this value.
            "bridge_token": formal_worker_source_token(companion_token),
            "providers": [
                provider.value
                for provider in default_browser_providers_from_registry()
            ],
            "model_agents_required": True,
            "model_runtime_identity": {
                "provider": primary.provider.value,
                "base_url": primary.base_url,
                "primary_model": primary.model,
                "fast_model": fast.model,
            },
            "formal_parent_api_origin": parent_origin,
            "adaptive_agent_scaling_enabled": (
                settings.adaptive_agent_scaling_enabled
            ),
            "now_iso": None,
            "http_host": None,
            "http_port": None,
            "icom_api_origin": None,
            "formal_source_private_key_path": None,
            "formal_source_ledger_path": None,
        }
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "live flexible worker runtime bundle is not valid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("live flexible worker runtime bundle must be an object")
        if parsed.get("formal_parent_api_origin") is not None:
            companion_token = settings.browser_bridge_token
            if (
                not isinstance(companion_token, str)
                or parsed.get("bridge_token")
                != formal_worker_source_token(companion_token)
            ):
                raise ValueError(
                    "formal live worker must use the separated parent-source token"
                )
    from tripchord.agents.live_flexible_worker_runtime import (
        build_authenticated_runtime_bundle,
    )

    return build_authenticated_runtime_bundle(parsed)


def _build_live_worker_result_importer(
    *, cache: LiveRunCache, tenant_id: str
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """Build the parent-side importer for a recovered worker result.

    Worker processes cannot publish handles into the API cache.  Keeping this
    importer as a factory makes the normal and cold-recovery paths identical,
    while the returned closure remains process-local and is never serialized.
    """

    async def import_result(result: dict[str, Any]) -> dict[str, Any]:
        raw_runs = result.pop("_worker_cache_runs", None)
        raw_handles = result.get("cached_pair_runs")
        if not isinstance(raw_runs, list) or not isinstance(raw_handles, list):
            raise RuntimeError("live planning worker cache handoff is missing")
        if len(raw_runs) != len(raw_handles) or len(raw_runs) > 8:
            raise RuntimeError("live planning worker cache handoff count is invalid")
        public_result = {
            key: value
            for key, value in result.items()
            if key not in {"worker_runtime_receipt", "model_execution_receipt"}
        }
        response = LiveFlexibleFromTextPlanningResponse.model_validate(public_result)
        if (response.interpretation.state == PackageRequestState.READY) != (
            response.run is not None
        ):
            raise RuntimeError("live planning worker result readiness is invalid")
        expected_pair_runs = (
            tuple(
                (execution.date_pair.id, execution.run)
                for execution in response.run.pair_runs
                if execution.run is not None
            )
            if response.run is not None
            else ()
        )
        parsed: list[tuple[str, LivePackageAgentRun]] = []
        worker_pair_ids: list[str] = []
        for item in raw_runs:
            if not isinstance(item, dict) or set(item) != {"date_pair_id", "run"}:
                raise RuntimeError("live planning worker cache entry is invalid")
            pair_id = item["date_pair_id"]
            if not isinstance(pair_id, str):
                raise RuntimeError("live planning worker cache pair id is invalid")
            parsed.append((pair_id, LivePackageAgentRun.model_validate(item["run"])))
            worker_pair_ids.append(pair_id)
        handle_pair_ids = [handle.date_pair_id for handle in response.cached_pair_runs]
        expected_by_id = dict(expected_pair_runs)
        if worker_pair_ids != handle_pair_ids or len(set(worker_pair_ids)) != len(worker_pair_ids):
            raise RuntimeError("live planning worker cache handles do not match runs")
        if any(pair_id not in expected_by_id for pair_id in worker_pair_ids):
            raise RuntimeError("live planning worker cache handle references unknown run")
        if any(
            cached_run != expected_by_id[pair_id]
            for pair_id, cached_run in parsed
        ):
            raise RuntimeError("live planning worker cache entries do not match result")
        parent_handles = await cache.import_worker_runs(tenant_id, tuple(parsed))
        result["cached_pair_runs"] = [
            handle.model_dump(mode="json") for handle in parent_handles
        ]
        return result

    return import_result


def _build_live_flexible_from_text_worker_command(
    payload: LiveFlexibleFromTextPlanningRequest,
    *,
    request_digest: str,
    target_app: FastAPI,
    cache: LiveRunCache,
    principal: Principal,
) -> LiveJobWorkerCommand:
    """Wrap the real planning operation for execution in an independent worker.

    C-146 P0-1: the production persistent-task entry must reach an independent
    worker/process — not a coroutine inside the API process. The command's
    ``module_path`` points at the production worker entry, which reconstructs the
    durable request and runs the SAME ``_execute_live_flexible_from_text`` path
    the API uses. Query / cancel / retry / cold-start recovery stay bound to the
    durable job identity owned by the registry. When a runtime bundle is
    configured it is forwarded to the worker so the ready chain builds its OWN
    real system in the worker process.
    """
    args: dict[str, Any] = {
        "payload": payload.model_dump(mode="json"),
        "request_digest": request_digest,
        "tenant_id": principal.tenant_id,
    }
    runtime_bundle = _live_flexible_worker_runtime_bundle()
    if runtime_bundle is not None:
        args["runtime_bundle"] = runtime_bundle

    return LiveJobWorkerCommand(
        module_path=str(Path(live_flexible_from_text_worker.__file__)),
        entry="run_live_flexible_from_text",
        args=args,
        result_importer=_build_live_worker_result_importer(
            cache=cache, tenant_id=principal.tenant_id
        ),
    )


def _build_live_flexible_from_text_operation(
    payload: LiveFlexibleFromTextPlanningRequest,
    *,
    request_digest: str,
    target_app: FastAPI,
    cache: LiveRunCache,
    principal: Principal,
) -> Callable[[LiveJobProgressReporter], Awaitable[dict[str, Any]]]:
    """Run an ordinary user job against the API's connected live sources.

    The signed subprocess source path is reserved for explicitly prepared formal
    evidence runs.  A normal product request has no formal execution capability,
    so it must use the already connected Browser Companion owned by this API
    process instead of entering a worker runtime that can only accept a signed
    formal activation.
    """

    async def operation(report: LiveJobProgressReporter) -> dict[str, Any]:
        response = await _execute_live_flexible_from_text(
            payload,
            target_app=target_app,
            cache=cache,
            principal=principal,
            report_progress=report,
            report_pair_checkpoint=report.report_pair_checkpoint,
            expected_request_sha256=request_digest,
            model_trace_scope_id=report.job_id,
            report_model_trace_summary=report.report_model_trace_summary,
        )
        return response.model_dump(mode="json")

    return operation


def _live_planning_job_boundary(registry: LivePlanningJobRegistry) -> str:
    return (
        DURABLE_LIVE_PLANNING_BOUNDARY
        if registry.durable_store_configured
        else NON_DURABLE_LIVE_PLANNING_BOUNDARY
    )


@app.post(
    "/api/v1/agents/live-flexible-plan-from-text/jobs",
    response_model=StartLiveFlexibleFromTextJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_live_flexible_from_text_job_endpoint(
    payload: LiveFlexibleFromTextPlanningRequest,
    http_request: Request,
    cache: LiveRunCacheDep,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    formal_prepare_credential: Annotated[
        str | None,
        Header(alias="X-TripChord-Formal-Source-Control"),
    ] = None,
) -> StartLiveFlexibleFromTextJobResponse:
    await _preflight_live_flexible_from_text(
        payload,
        http_request,
        principal,
        rate_limit_bucket="live-flexible-agent-plan-from-text-job",
    )
    target_app = http_request.app
    request_digest = _live_flexible_from_text_request_sha256(payload)
    effective_total_timeout_seconds = _flexible_total_timeout_seconds(payload.total_timeout_seconds)
    defer_start = formal_prepare_credential is not None
    if defer_start:
        _authorize_formal_source_control(http_request, formal_prepare_credential)

    try:
        job, replayed = await registry.start_idempotent(
            tenant_id=principal.tenant_id,
            # C-146 P0-1/P0-5: the real operation is a worker command, but the
            # registry builds it LAZILY only after its atomic idempotency-capacity
            # gate accepts this new key. A full collection therefore performs
            # zero worker-command / UUID / runtime construction and the existing
            # identity bytes stay untouched, even under concurrent admissions.
            operation_factory=lambda: (
                _build_live_flexible_from_text_operation(
                    payload,
                    request_digest=request_digest,
                    target_app=target_app,
                    cache=cache,
                    principal=principal,
                )
                if (
                    not defer_start
                    and getattr(
                        target_app.state, "formal_live_source_authority", None
                    )
                    is not None
                )
                else _build_live_flexible_from_text_worker_command(
                    payload,
                    request_digest=request_digest,
                    target_app=target_app,
                    cache=cache,
                    principal=principal,
                )
            ),
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            deadline_seconds=effective_total_timeout_seconds,
            defer_start=defer_start,
        )
    except LivePlanningJobIdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="idempotency key was already used with a different request",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except LivePlanningJobCapacityError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="live planning job capacity is currently exhausted",
            headers={"Retry-After": "30"},
        ) from exc
    except LivePlanningJobCancellationPendingError as exc:
        # P0-2: a same-key retry while the real operation is still stopping must
        # return a stable, machine-decidable, retryable 409 with the original
        # job identity and a status query location — never a bare 500 that hides
        # an in-flight cancellation, never a new job, never a false success. The
        # terminal state is returned only once the operation truly stops.
        if exc.job_id is not None:
            pending = await registry.get(exc.job_id, principal.tenant_id)
            if pending is not None:
                status_url = (
                    f"/api/v1/agents/live-flexible-plan-from-text/jobs/{pending.id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "cancellation_pending",
                        "status_code": status.HTTP_409_CONFLICT,
                        "job_id": pending.id,
                        "state": "cancellation_pending",
                        "cancel_pending": pending.cancel_pending,
                        "stage": pending.stage,
                        "retryable": True,
                        "status_url": status_url,
                    },
                ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "cancellation_pending",
                "status_code": status.HTTP_409_CONFLICT,
                "job_id": exc.job_id,
                "state": "cancellation_pending",
                "cancel_pending": True,
                "retryable": True,
            },
        ) from exc
    except LivePlanningJobRegistryPostCommitError as exc:
        # P0-2: the persistent task entry was already committed to disk and the
        # real task is running, but the response envelope was lost to a
        # post-commit persist exception. Return the recoverable committed job
        # identity so the caller can query/cancel the same task with only the
        # original Idempotency-Key or the returned identity — never a bare 500
        # that leaves a running task unreachable.
        if exc.job_id is not None:
            committed = await registry.get(exc.job_id, principal.tenant_id)
            if committed is not None:
                status_url = f"/api/v1/agents/live-flexible-plan-from-text/jobs/{committed.id}"
                return StartLiveFlexibleFromTextJobResponse(
                    job=committed,
                    replayed=False,
                    status_url=status_url,
                    events_url=f"{status_url}/events",
                    boundary=_live_planning_job_boundary(registry),
                )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="live planning job was committed but its identity could not be recovered",
        ) from exc
    status_url = f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job.id}"
    return StartLiveFlexibleFromTextJobResponse(
        job=job,
        replayed=replayed,
        status_url=status_url,
        events_url=f"{status_url}/events",
        boundary=_live_planning_job_boundary(registry),
    )


def _with_current_final_plan_projection(
    job: LivePlanningJobSnapshot,
) -> LivePlanningJobSnapshot:
    """Upgrade only an already-selected legacy plan for the response view.

    The saved run remains the source of truth and the persisted snapshot is not
    mutated.  Reprojection is accepted only when it selects the exact same
    option, so a display-schema upgrade cannot silently change the itinerary.
    """

    payload = job.result
    if payload is None:
        return job
    final_payload = payload.get("final_plan")
    best_payload = payload.get("best_available_plan")
    legacy_plan = final_payload if isinstance(final_payload, dict) else best_payload
    if not isinstance(legacy_plan, dict):
        return job
    if legacy_plan.get("projection_schema_version") == "final-plan-projection-v2":
        return job
    try:
        response = LiveFlexibleFromTextPlanningResponse.model_validate(payload)
    except ValidationError:
        return job
    if response.run is None:
        return job

    target_field = "final_plan" if isinstance(final_payload, dict) else "best_available_plan"
    rebuilt = (
        build_final_plan_projection(response.run)
        if target_field == "final_plan"
        else build_best_available_plan_projection(response.run)
    )
    if rebuilt is None or rebuilt.option_id != legacy_plan.get("option_id"):
        return job

    upgraded_payload = dict(payload)
    upgraded_payload[target_field] = rebuilt.model_dump(mode="json")
    return job.model_copy(update={"result": upgraded_payload})


@app.get(
    "/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
    response_model=LivePlanningJobSnapshot,
)
async def get_live_flexible_from_text_job_endpoint(
    job_id: str,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
) -> LivePlanningJobSnapshot:
    await rate_limiter.check(principal.tenant_id, "live-flexible-agent-plan-from-text-job-get")
    job = await registry.get(job_id, principal.tenant_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="live job not found")
    return _with_current_final_plan_projection(job)


@app.get("/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}/events")
async def stream_live_flexible_from_text_job_endpoint(
    job_id: str,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    await rate_limiter.check(principal.tenant_id, "live-flexible-agent-plan-from-text-job-events")
    initial = await registry.get(job_id, principal.tenant_id)
    if initial is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="live job not found")

    async def events() -> AsyncIterator[str]:
        revision = 0
        barrier_announced = False
        while True:
            job = await registry.wait_for_change(
                job_id,
                principal.tenant_id,
                after_revision=revision,
                timeout_seconds=15,
            )
            if job is None:
                yield 'event: error\ndata: {"detail":"live job expired"}\n\n'
                return
            if job.revision == revision:
                yield "event: heartbeat\ndata: {}\n\n"
                continue
            if job.barrier_released_at is not None and not barrier_announced:
                # v0.3 barrier gating: before this point the stream carried only
                # progress/terminal events; the barrier release is the first
                # signal that the final result is about to become available.
                barrier_announced = True
                barrier_payload = json.dumps(
                    {"barrier_released_at": job.barrier_released_at.isoformat()},
                    ensure_ascii=False,
                )
                yield f"event: barrier\ndata: {barrier_payload}\n\n"
            # The status stream intentionally excludes the potentially large quote result
            # before the barrier.  Once the barrier has released AND the job reached a
            # terminal state, the result is delivered exactly once as its own event.
            if (
                job.state in TERMINAL_LIVE_PLANNING_JOB_STATES
                and job.barrier_released_at is not None
            ):
                payload = json.dumps(
                    _with_current_final_plan_projection(job).model_dump(mode="json"),
                    ensure_ascii=False,
                )
                yield f"event: result\ndata: {payload}\n\n"
                return
            payload = json.dumps(
                job.model_dump(mode="json", exclude={"result"}),
                ensure_ascii=False,
            )
            yield f"event: job\ndata: {payload}\n\n"
            revision = job.revision
            if job.state in TERMINAL_LIVE_PLANNING_JOB_STATES:
                return

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete(
    "/api/v1/agents/live-flexible-plan-from-text/jobs/{job_id}",
    response_model=LivePlanningJobSnapshot,
)
async def cancel_live_flexible_from_text_job_endpoint(
    job_id: str,
    registry: LivePlanningJobRegistryDep,
    principal: PrincipalDep,
) -> LivePlanningJobSnapshot:
    await rate_limiter.check(principal.tenant_id, "live-flexible-agent-plan-from-text-job-cancel")
    job = await registry.cancel(job_id, principal.tenant_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="live job not found")
    return job


async def _attach_icom_cny_reference_estimate(
    run: LivePackageAgentRun,
) -> LivePackageAgentRun:
    """Attach a display-only ECB estimate without changing package truth."""

    if run.decision.state == PackageDecisionState.ACCEPT and run.package is not None:
        budget = run.package.budget
    elif run.decision_only_candidate is not None:
        budget = run.decision_only_candidate.budget
    else:
        return run
    if budget.is_all_in_total:
        return run
    supplemental = tuple(
        item
        for item in budget.supplemental_published_base_fares
        if item.currency == "USD"
    )
    if len(supplemental) != 1:
        return run
    source = supplemental[0]
    try:
        estimate = await fetch_icom_cny_reference_estimate(
            source_usd_base_fare_cents=source.total_for_party_cents,
            price_contract_ids=source.price_contract_ids,
            transfer_ids=source.transfer_ids,
        )
    except ProviderError as exc:
        logger.warning(
            "iCom CNY reference estimate unavailable: provider=%s code=%s",
            exc.provider,
            exc.code,
        )
        return run
    return LivePackageAgentRun.model_validate(
        {
            **run.model_dump(mode="python"),
            "icom_cny_reference_estimate": estimate,
        }
    )


async def _attach_flexible_pair_icom_reference_estimates(
    run: FlexibleLiveAgentRun,
) -> FlexibleLiveAgentRun:
    """Attach iCom reference estimates to every flexible pair independently."""

    updated_pairs: list[FlexiblePairExecution] = []
    for pair in run.pair_runs:
        updated_run = (
            await _attach_icom_cny_reference_estimate(pair.run)
            if pair.run is not None
            else None
        )
        updated_exploration = (
            await _attach_icom_cny_reference_estimate(pair.exploration_run)
            if pair.exploration_run is not None
            else None
        )
        updated_pairs.append(
            pair.model_copy(
                update={
                    "run": updated_run,
                    "exploration_run": updated_exploration,
                }
            )
        )
    return run.model_copy(update={"pair_runs": tuple(updated_pairs)})


@app.post("/api/v1/agents/live-plan", response_model=LiveAgentPlanningResponse)
async def live_agent_plan_endpoint(
    request: LiveAgentPlanningRequest,
    live_system: LiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveAgentPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "live-agent-plan")
    if (
        settings.browser_bridge_require_all_providers
        and request.coverage_mode != LiveCoverageMode.STRICT
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="server policy requires strict full-coverage mode across selected scopes",
        )
    try:
        if isinstance(live_system, LivePackageAgentSystem):
            run = await live_system.run(
                request.intent,
                request.search_query,
                mode=request.coverage_mode,
                timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
                memory_access=_memory_access(principal, request.intent.trip_id),
            )
        else:
            run = await live_system.run(
                request.intent,
                request.search_query,
                mode=request.coverage_mode,
                timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    run = await _attach_icom_cny_reference_estimate(run)
    run_id, expires_at = await cache.put(principal.tenant_id, run)
    return LiveAgentPlanningResponse(
        run_id=run_id,
        expires_at=expires_at,
        run=run,
        final_plan=build_final_plan_projection(run),
    )


@app.get(
    "/api/v1/agents/live-plans/{run_id}",
    response_model=LiveAgentPlanningResponse,
)
async def get_live_agent_plan_endpoint(
    run_id: str,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveAgentPlanningResponse:
    """Return the tenant-bound current generation after event/monitor repairs."""

    await rate_limiter.check(principal.tenant_id, "live-agent-plan-get")
    entry = await cache.get(run_id, principal.tenant_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live planning run was not found or has expired",
        )
    async with entry.lock:
        current = await cache.get(run_id, principal.tenant_id)
        if current is not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="live planning run was not found or has expired",
            )
        return LiveAgentPlanningResponse(
            run_id=run_id,
            expires_at=entry.expires_at,
            run=entry.run,
            final_plan=build_final_plan_projection(entry.run),
        )


@app.post(
    "/api/v1/agents/live-plans/{run_id}/modify",
    response_model=LivePlanModificationResponse,
)
async def modify_live_agent_plan_endpoint(
    run_id: str,
    request: LivePlanModificationRequest,
    live_system: LiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LivePlanModificationResponse:
    await rate_limiter.check(principal.tenant_id, "live-agent-plan-modify")
    entry = await cache.get(run_id, principal.tenant_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live planning run was not found or has expired",
        )
    async with entry.lock:
        current = await cache.get(run_id, principal.tenant_id)
        if current is not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="live planning run was not found or has expired",
            )
        try:
            _require_final_published_live_run_for_event(entry.run)
            modification_intent = parse_live_plan_modification(
                request.instruction,
                current_departure_date=entry.run.intent.start_date,
            )
            updated, receipt = await live_system.modify_plan(
                entry.run,
                modification_intent,
                timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
                memory_access=_memory_access(principal, entry.run.intent.trip_id),
                booking_ledger=_load_booking_ledger_for_run(run_id),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc

        if receipt.status in {
            LivePlanModificationStatus.MODIFIED,
            LivePlanModificationStatus.GLOBAL_REPLAN,
        }:
            if receipt.status == LivePlanModificationStatus.GLOBAL_REPLAN:
                updated = await _attach_icom_cny_reference_estimate(updated)
            expires_at = await cache.replace(
                run_id,
                principal.tenant_id,
                entry,
                updated,
            )
            if expires_at is None:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="live planning run expired during natural-language modification",
                )
        else:
            updated = entry.run
            expires_at = entry.expires_at

    return LivePlanModificationResponse(
        run_id=run_id,
        expires_at=expires_at,
        modification=receipt,
        run=updated,
        final_plan=build_final_plan_projection(updated),
    )


@app.post(
    "/api/v1/agents/live-plans/{run_id}/events/replan",
    response_model=LiveAgentEventReplanResponse,
)
async def live_agent_event_replan_endpoint(
    run_id: str,
    request: LiveAgentEventReplanRequest,
    live_system: LiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveAgentEventReplanResponse:
    await rate_limiter.check(principal.tenant_id, "live-agent-event-replan")
    entry = await cache.get(run_id, principal.tenant_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live planning run was not found or has expired",
        )
    async with entry.lock:
        current = await cache.get(run_id, principal.tenant_id)
        if current is not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="live planning run was not found or has expired",
            )
        try:
            _require_final_published_live_run_for_event(entry.run)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        try:
            ledger = _load_booking_ledger_for_run(run_id)
            if isinstance(live_system, LivePackageAgentSystem):
                run = await live_system.replan_after_event(
                    entry.run,
                    request.event,
                    timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
                    memory_access=_memory_access(principal, entry.run.intent.trip_id),
                    booking_ledger=ledger,
                )
            else:
                run = await live_system.replan_after_event(
                    entry.run,
                    request.event,
                    timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        updated = _advance_cached_live_run(entry.run, run)
        expires_at = await cache.replace(
            run_id,
            principal.tenant_id,
            entry,
            updated,
        )
        if expires_at is None:
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail="live planning run expired during event replanning",
            )
    return LiveAgentEventReplanResponse(
        run_id=run_id,
        expires_at=expires_at,
        run=run,
        final_plan=build_final_plan_projection(updated),
    )


@app.post(
    "/api/v1/agents/live-plans/{run_id}/monitor",
    response_model=LiveMonitorResponse,
    status_code=status.HTTP_201_CREATED,
)
async def start_live_monitor_endpoint(
    run_id: str,
    request: StartLiveMonitorRequest,
    live_system: LiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveMonitorResponse:
    del live_system
    await rate_limiter.check(principal.tenant_id, "live-monitor-start")
    entry = await cache.get(run_id, principal.tenant_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live planning run was not found or has expired",
        )
    try:
        _require_final_published_live_run_for_event(entry.run)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if entry.run.decision.state != PackageDecisionState.ACCEPT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="periodic revalidation requires a currently accepted live package",
        )
    safe_interval = _maximum_safe_live_monitor_interval_seconds(entry.run)
    if safe_interval is not None and request.interval_seconds > safe_interval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "requested monitor interval cannot refresh every package component before "
                f"quote expiry; current maximum is {safe_interval} seconds"
            ),
        )
    try:
        monitor = await live_quote_monitor_registry.start(
            run_id=run_id,
            tenant_id=principal.tenant_id,
            interval_seconds=request.interval_seconds,
            max_checks=request.max_checks,
            timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
        ) from exc
    return LiveMonitorResponse(monitor=monitor)


@app.get(
    "/api/v1/agents/live-monitors/{monitor_id}",
    response_model=LiveMonitorResponse,
)
async def get_live_monitor_endpoint(
    monitor_id: str,
    principal: PrincipalDep,
) -> LiveMonitorResponse:
    await rate_limiter.check(principal.tenant_id, "live-monitor-get")
    monitor = await live_quote_monitor_registry.get(monitor_id, principal.tenant_id)
    if monitor is None:
        # A monitor whose process is gone is still recoverable from the durable
        # store (v0.9): history and status survive a restart.
        try:
            monitor = await live_monitor_store.get_status(principal.tenant_id, monitor_id)
        except LiveMonitorNotFoundError:
            monitor = None
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live quote monitor was not found",
        )
    return LiveMonitorResponse(monitor=monitor)


@app.post(
    "/api/v1/agents/live-monitors/{monitor_id}/check-now",
    response_model=LiveMonitorResponse,
)
async def check_live_monitor_now_endpoint(
    monitor_id: str,
    live_system: LiveSystemDep,
    principal: PrincipalDep,
) -> LiveMonitorResponse:
    del live_system
    await rate_limiter.check(principal.tenant_id, "live-monitor-check-now")
    monitor = await live_quote_monitor_registry.check_now(
        monitor_id,
        principal.tenant_id,
    )
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live quote monitor was not found",
        )
    return LiveMonitorResponse(monitor=monitor)


@app.delete(
    "/api/v1/agents/live-monitors/{monitor_id}",
    response_model=LiveMonitorResponse,
)
async def stop_live_monitor_endpoint(
    monitor_id: str,
    principal: PrincipalDep,
) -> LiveMonitorResponse:
    await rate_limiter.check(principal.tenant_id, "live-monitor-stop")
    monitor = await live_quote_monitor_registry.stop(monitor_id, principal.tenant_id)
    if monitor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live quote monitor was not found",
        )
    return LiveMonitorResponse(monitor=monitor)


@app.post(
    "/api/v1/workspaces",
    response_model=WorkspaceSnapshot,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_endpoint(
    request: CreateWorkspaceRequest,
    session: SessionDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).create(
            request.spec, request.title, idempotency_key
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v1/workspaces/{workspace_id}", response_model=WorkspaceSnapshot)
async def get_workspace_endpoint(
    workspace_id: str,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc


@app.post("/api/v1/workspaces/{workspace_id}/plans", response_model=WorkspaceSnapshot)
async def save_workspace_plan_endpoint(
    workspace_id: str,
    request: SavePlanRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceSnapshot:
    try:
        return await WorkspaceRepository(session, principal.tenant_id).save_plan(
            workspace_id, request.plan
        )
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get(
    "/api/v1/workspaces/{workspace_id}/plans/{from_version}/diff/{to_version}",
    response_model=PlanDiff,
)
async def compare_workspace_plans_endpoint(
    workspace_id: str,
    from_version: int,
    to_version: int,
    session: SessionDep,
    principal: PrincipalDep,
) -> PlanDiff:
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    plans = {plan.version: plan for plan in workspace.plans}
    if from_version not in plans or to_version not in plans:
        raise HTTPException(status_code=404, detail="plan version not found")
    return diff_plans(plans[from_version], plans[to_version])


@app.post(
    "/api/v1/workspaces/{workspace_id}/events/replan",
    response_model=WorkspaceReplanResponse,
)
async def persisted_replan_endpoint(
    workspace_id: str,
    request: WorkspaceReplanRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> WorkspaceReplanResponse:
    await rate_limiter.check(principal.tenant_id, "replan")
    repository = WorkspaceRepository(session, principal.tenant_id)
    try:
        workspace = await repository.get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if not workspace.plans:
        raise HTTPException(status_code=409, detail="workspace has no plan to replan")
    problem = await JobRepository(session, principal.tenant_id).latest_problem(workspace_id)
    result = AdaptiveReplanner(
        replan_policy,
        max_repair_iterations=request.max_iterations,
    ).replan(
        workspace.spec,
        workspace.plans[-1],
        request.event,
        request.preference,
        problem,
        request.context,
        request.dependencies,
        request.replacements,
    )
    plan_to_store = result.final_plan if result.status == "ready" and result.diff.changed else None
    try:
        updated = await repository.record_replan(
            workspace_id,
            request.event,
            result,
            plan_to_store,
        )
    except WorkspaceConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return WorkspaceReplanResponse(result=result, workspace=updated)


@app.post(
    "/api/v1/workspaces/{workspace_id}/jobs/planning",
    response_model=JobSnapshot,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_planning_job_endpoint(
    workspace_id: str,
    request: CreatePlanningJobRequest,
    session: SessionDep,
    runner: RunnerDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobSnapshot:
    await rate_limiter.check(principal.tenant_id, "planning-job")
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).get(workspace_id)
    except WorkspaceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="workspace not found") from exc
    if request.problem.trip != workspace.spec:
        raise HTTPException(status_code=409, detail="planning problem trip differs from workspace")
    try:
        job = await JobRepository(session, principal.tenant_id).create(
            workspace_id, request.problem, idempotency_key
        )
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner.enqueue(job.id, workspace_id, request.problem, principal.tenant_id)
    return job


@app.post(
    "/api/v1/trips/plan",
    response_model=StartTripPlanningResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_trip_planning_endpoint(
    request: StartTripPlanningRequest,
    session: SessionDep,
    runner: RunnerDep,
    principal: PrincipalDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StartTripPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "trip-plan")
    try:
        problem = planning_assembler.assemble(request.spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        workspace = await WorkspaceRepository(session, principal.tenant_id).create(
            request.spec, request.title, idempotency_key
        )
        job = await JobRepository(session, principal.tenant_id).create(
            workspace.id, problem, idempotency_key
        )
    except (WorkspaceConflictError, JobConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runner.enqueue(job.id, workspace.id, problem, principal.tenant_id)
    return StartTripPlanningResponse(
        workspace=workspace,
        job=job,
        data_mode="replay",
        candidate_count=len(problem.activities),
    )


@app.get(
    "/api/v1/workspaces/{workspace_id}/jobs/{job_id}",
    response_model=JobSnapshot,
)
async def get_planning_job_endpoint(
    workspace_id: str,
    job_id: str,
    runner: RunnerDep,
    principal: PrincipalDep,
) -> JobSnapshot:
    try:
        job = await runner.get(job_id, principal.tenant_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="job not found") from exc
    if job.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/v1/workspaces/{workspace_id}/jobs/{job_id}/events")
async def stream_planning_job_endpoint(
    workspace_id: str,
    job_id: str,
    runner: RunnerDep,
    principal: PrincipalDep,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        last_payload = ""
        while True:
            try:
                job = await runner.get(job_id, principal.tenant_id)
            except JobNotFoundError:
                yield 'event: error\ndata: {"detail":"job not found"}\n\n'
                return
            if job.workspace_id != workspace_id:
                yield 'event: error\ndata: {"detail":"job not found"}\n\n'
                return
            payload = json.dumps(job.model_dump(mode="json"), ensure_ascii=False)
            if payload != last_payload:
                yield f"event: job\ndata: {payload}\n\n"
                last_payload = payload
            if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
                return
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def require_amap() -> AmapTravelDataProvider:
    if amap is None:
        raise HTTPException(
            status_code=503,
            detail="AMap provider is not configured; set AMAP_API_KEY",
        )
    return amap


@app.post("/api/v1/places/geocode", response_model=Coordinates)
async def geocode_endpoint(request: GeocodeRequest) -> Coordinates:
    provider = require_amap()
    return await provider.geocode(request.address, request.city)


@app.post("/api/v1/places/search", response_model=tuple[Place, ...])
async def place_search_endpoint(request: PlaceSearchRequest) -> tuple[Place, ...]:
    provider = require_amap()
    return await provider.search_places(
        request.keywords,
        request.city,
        types=request.types,
        limit=request.limit,
    )


@app.post("/api/v1/routes", response_model=RouteLeg)
async def route_endpoint(request: RouteRequest) -> RouteLeg:
    provider = require_amap()
    return await provider.route(
        request.origin,
        request.destination,
        request.mode,
        city=request.city,
    )


@app.post("/api/v1/weather", response_model=tuple[WeatherWindow, ...])
async def weather_endpoint(request: WeatherRequest) -> tuple[WeatherWindow, ...]:
    provider = require_amap()
    return await provider.weather(
        request.adcode,
        request.coordinates,
    )
