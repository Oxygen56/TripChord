import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tripchord import __version__
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
    LiveDatePairRunner,
    PairCheckpointReporter,
)
from tripchord.agents.live_advisory import AgenticRunSummary
from tripchord.agents.live_jobs import (
    TERMINAL_LIVE_PLANNING_JOB_STATES,
    LiveJobProgressReporter,
    LivePlanningJobCapacityError,
    LivePlanningJobIdempotencyConflictError,
    LivePlanningJobRegistry,
    LivePlanningJobSnapshot,
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
)
from tripchord.agents.persistent_memory import (
    CorruptionPolicy,
    PersistentMemoryStore,
)
from tripchord.agents.rag import EvidenceRagRetriever
from tripchord.agents.stay_area import system_stay_area_search_profile
from tripchord.api import (
    LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY,
    AgentMemoryListResponse,
    AgentPlanningRequest,
    AgentPlanningResponse,
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
from tripchord.persistence.repository import (
    WorkspaceConflictError,
    WorkspaceNotFoundError,
    WorkspaceSnapshot,
)
from tripchord.planning.adaptive import AdaptiveReplanner
from tripchord.planning.assembler import PlanningProblemAssembler, ReplayPlaceCatalog
from tripchord.planning.flexible_dates import (
    LIVE_V5_PLATFORMS,
    FlexibleDateExplorer,
    FlexibleQueryPlanBuilder,
)
from tripchord.planning.package import PackageDecisionState, PackageEventKind
from tripchord.planning.policy import ReplanPolicySelector
from tripchord.planning.problem import PlanningInfeasible
from tripchord.planning.repair import PlanDiff, diff_plans
from tripchord.planning.replanner import LocalReplanResult
from tripchord.planning.requirements import RequirementParseResult
from tripchord.planning.stay_plans import system_stay_plan_candidate_set
from tripchord.planning.workflow import WorkflowResult
from tripchord.providers.amap import AmapTravelDataProvider
from tripchord.providers.base import OfferSearchQuery, OfferSearchResult
from tripchord.providers.browser_bridge import (
    BRIDGE_TOKEN_HEADER,
    CONTROL_TOKEN_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    LIVE_V5_BROWSER_PROVIDERS,
    BrowserTaskBridge,
    create_browser_bridge_app,
    is_loopback_client,
)
from tripchord.providers.factory import build_amap_provider, build_provider_registry
from tripchord.providers.icom_transfer import IComTransferProvider
from tripchord.providers.user_snapshot import UserQuoteInput
from tripchord.rate_limit import RateLimiter

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
                ("flight", "lodging")
                if provider.value in {"ctrip", "qunar"}
                else ("flight",)
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
            candidate = f"live-run-{secrets.token_urlsafe(18)}"
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
            f"{state_path.name}.corrupt-"
            f"{self._utc_now().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex}"
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
) -> tuple[BrowserTaskBridge | None, LivePackageAgentSystem | None]:
    token = configured_settings.browser_bridge_token
    if not configured_settings.browser_bridge_enabled or token is None or len(token) < 32:
        target_app.state.browser_task_bridge = None
        target_app.state.live_package_agent_system = None
        target_app.state.flexible_live_agent_system = None
        target_app.state.icom_transfer_provider = None
        target_app.state.browser_bridge_token = None
        target_app.state.browser_bridge_control_token = None
        target_app.state.browser_bridge_control_enabled = False
        target_app.state.browser_companion_auto_reload_enabled = False
        target_app.state.browser_companion_runtime_agent = None
        target_app.state.browser_companion_runtime_supervisor = None
        return None, None
    bridge = BrowserTaskBridge()
    icom_provider = IComTransferProvider()
    selected_memory_store = memory_store or MemoryStore()
    selected_context_builder = context_builder or BudgetedAgentContextBuilder(
        EvidenceRagRetriever(selected_memory_store)
    )
    live_system = LivePackageAgentSystem(
        bridge,
        icom_provider=icom_provider,
        model_router=model_router,
        model_agents_required=configured_settings.model_agents_required,
        context_builder=selected_context_builder,
        memory_store=selected_memory_store,
        providers=LIVE_V5_BROWSER_PROVIDERS,
    )
    flexible_system = FlexibleLiveAgentSystem(
        cast(LiveDatePairRunner, live_system),
        explorer=FlexibleDateExplorer(LIVE_V5_PLATFORMS),
        query_planner=FlexibleQueryPlanBuilder(LIVE_V5_PLATFORMS),
        minimum_departure_lead_days=7,
        model_router=model_router,
        model_agents_required=configured_settings.model_agents_required,
        context_builder=selected_context_builder,
        memory_store=selected_memory_store,
        adaptive_agent_scaling_enabled=(
            configured_settings.adaptive_agent_scaling_enabled
            and model_router is not None
        ),
    )
    target_app.mount(
        _BROWSER_BRIDGE_MOUNT,
        create_browser_bridge_app(
            bridge,
            bridge_token=token,
            control_token=configured_settings.browser_bridge_control_token,
            allowed_origin_regex=configured_settings.browser_bridge_allowed_origin_regex,
        ),
    )
    target_app.state.browser_task_bridge = bridge
    target_app.state.live_package_agent_system = live_system
    target_app.state.flexible_live_agent_system = flexible_system
    target_app.state.icom_transfer_provider = icom_provider
    target_app.state.browser_bridge_token = token
    control_token = configured_settings.browser_bridge_control_token
    auto_reload_enabled = configured_settings.browser_companion_auto_reload_enabled
    runtime_agent = (
        BrowserCompanionRuntimeExecutorAgent(bridge)
        if auto_reload_enabled or control_token is not None
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
        corruption_policy=CorruptionPolicy(
            configured_settings.live_run_cache_corruption_policy
        ),
    )


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
job_runner = PlanningJobRunner(database)
rate_limiter = RateLimiter(
    limit=settings.rate_limit_requests,
    window_seconds=settings.rate_limit_window_seconds,
    redis_url=settings.redis_url,
)
planning_assembler = PlanningProblemAssembler(
    ReplayPlaceCatalog()
)
replan_policy = ReplanPolicySelector.from_package_data()
live_run_cache = _build_live_run_cache(settings)
package_requirement_agent = HybridPackageRequirementAgent(model_router=model_router)
live_planning_job_registry = LivePlanningJobRegistry()


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


@asynccontextmanager
async def lifespan(target_app: FastAPI) -> AsyncIterator[None]:
    await database.create_schema()
    await job_runner.recover()
    shared_model_http = cast(
        ManagedModelHTTPRuntime | None,
        getattr(target_app.state, "model_http_runtime", None),
    )
    model_enabled = getattr(target_app.state, "model_router", None) is not None
    model_http_started = False
    companion_supervisor = cast(
        BrowserCompanionRuntimeSupervisor | None,
        getattr(target_app.state, "browser_companion_runtime_supervisor", None),
    )
    try:
        if shared_model_http is not None and model_enabled:
            await shared_model_http.start()
            model_http_started = True
        if companion_supervisor is not None:
            companion_supervisor.start()
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
        # Every model consumer is stopped before the shared connection pool.
        # Closing earlier can deadlock while an in-flight monitor waits on HTTP.
        shutdown_steps: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        if companion_supervisor is not None:
            shutdown_steps.append(("browser_companion_supervisor", companion_supervisor.close))
        if live_job_registry is not None:
            shutdown_steps.append(("live_planning_jobs", live_job_registry.close))
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
browser_task_bridge, live_package_agent_system = _install_browser_bridge(
    app,
    settings,
    model_router=model_router,
    context_builder=context_builder,
    memory_store=memory_store,
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
        model_trace_count=len(model_trace_sink.records),
        effective_flexible_timeout_seconds=_flexible_total_timeout_seconds(None),
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
            companion_supervisor.last_outcome
            if companion_supervisor is not None
            else None
        ),
        browser_companion_supervisor_attempt_count=(
            companion_supervisor.attempt_count
            if companion_supervisor is not None
            else 0
        ),
        browser_companion_last_reconcile=(
            companion_supervisor.last_reconcile_result
            if companion_supervisor is not None
            else None
        ),
    )


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
    return configured if requested is None else min(requested, configured)


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
        user_id=(
            None
            if principal.auth_mode == "development-anonymous"
            else principal.tenant_id
        ),
        session_id=trip_id,
        trip_id=trip_id,
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
    combined_agentic = AgenticRunSummary.combine(
        (previous.agentic, replanned.agentic)
    )
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


async def _cache_flexible_pair_runs(
    run: FlexibleLiveAgentRun,
    cache: LiveRunCache,
    tenant_id: str,
    *,
    ensure_active: Callable[[], Awaitable[None]] | None = None,
) -> tuple[LiveFlexiblePairRunHandle, ...]:
    handles: list[LiveFlexiblePairRunHandle] = []
    for pair_run in run.pair_runs:
        if pair_run.run is None:
            continue
        if ensure_active is not None:
            await ensure_active()
        run_id, expires_at = await cache.put(tenant_id, pair_run.run)
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
    flexible_system: FlexibleLiveSystemDep,
    cache: LiveRunCacheDep,
    principal: PrincipalDep,
) -> LiveFlexibleAgentPlanningResponse:
    await rate_limiter.check(principal.tenant_id, "live-flexible-agent-plan")
    if (
        settings.browser_bridge_require_all_providers
        and request.coverage_mode != LiveCoverageMode.STRICT
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="server policy requires strict three-platform coverage",
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
    handles = await _cache_flexible_pair_runs(run, cache, principal.tenant_id)
    return LiveFlexibleAgentPlanningResponse(
        run=run,
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
            detail="server policy requires strict three-platform coverage",
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
    model_enabled = getattr(target_app.state, "model_router", None) is not None
    if interpretation.state == PackageRequestState.HUMAN_BLOCK:
        await report("blocked_before_live_search", 95)
        return LiveFlexibleFromTextPlanningResponse(
            interpretation=interpretation,
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
    flexible_system = _flexible_live_agent_system_from_app(target_app)
    constraints = FlexiblePackageConstraints(
        budget_cents=intent_template.budget_cents,
        require_checked_baggage=intent_template.require_checked_baggage,
        allow_connections=intent_template.allow_connections,
        require_breakfast=intent_template.require_breakfast,
        breakfast_preference_mode=intent_template.breakfast_preference_mode,
        breakfast_preference_weight=intent_template.breakfast_preference_weight,
        minimum_arrival_to_boat_minutes=(intent_template.minimum_arrival_to_boat_minutes),
        minimum_airport_buffer_minutes=intent_template.minimum_airport_buffer_minutes,
    )
    pair_timeout_seconds = _live_timeout_seconds(payload.timeout_seconds)
    total_timeout_seconds = _flexible_total_timeout_seconds(payload.total_timeout_seconds)
    stay_plan_candidate_set = payload.stay_plan_candidate_set
    if stay_plan_candidate_set is None:
        stay_area_profile = system_stay_area_search_profile(window.destination)
        if stay_area_profile is not None:
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
                run = await flexible_system.run(
                    window,
                    payload.calendars,
                    mode=payload.coverage_mode,
                    max_pairs=payload.max_pairs,
                    constraints=constraints,
                    timeout_seconds=pair_timeout_seconds,
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
    await report("caching_pair_runs", 90)
    handles = await _cache_flexible_pair_runs(
        run,
        cache,
        principal.tenant_id,
        ensure_active=(report_progress.ensure_active if report_progress is not None else None),
    )
    execution_boundary = LIVE_FLEXIBLE_FROM_TEXT_EXECUTION_BOUNDARY
    if model_enabled:
        execution_boundary = (
            "需求理解已由真实模型 Agent 提案并经确定性事实锁对账；"
            "实时整包中的证据仲裁、候选策展、风险批判、Repair 策略"
            "和主控建议同样使用受限模型 Agent；报价、金额、权限和"
            "硬约束始终由确定性代码控制。"
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
        cached_pair_runs=handles,
        model_enhancement_enabled=model_enabled,
        model_trace_scope_sha256=model_trace_scope_sha256,
        model_trace_count=0,
        model_trace_success_count=0,
        model_trace_failure_count=0,
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
    report_model_trace_summary: Callable[[str, str, int, int, int], Awaitable[None]]
    | None = None,
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
        str | None,
        Header(alias="Idempotency-Key", max_length=200),
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
    effective_total_timeout_seconds = _flexible_total_timeout_seconds(
        payload.total_timeout_seconds
    )

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
    try:
        job, replayed = await registry.start_idempotent(
            tenant_id=principal.tenant_id,
            operation=operation,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            deadline_seconds=effective_total_timeout_seconds,
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
    status_url = f"/api/v1/agents/live-flexible-plan-from-text/jobs/{job.id}"
    return StartLiveFlexibleFromTextJobResponse(
        job=job,
        replayed=replayed,
        status_url=status_url,
        events_url=f"{status_url}/events",
    )


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
    return job


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
            # The status stream intentionally excludes the potentially large quote result.
            # Clients fetch the result once from the tenant-scoped GET endpoint.
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
            detail="server policy requires strict three-platform coverage",
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
    run_id, expires_at = await cache.put(principal.tenant_id, run)
    return LiveAgentPlanningResponse(
        run_id=run_id,
        expires_at=expires_at,
        run=run,
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
            if isinstance(live_system, LivePackageAgentSystem):
                run = await live_system.replan_after_event(
                    entry.run,
                    request.event,
                    timeout_seconds=_live_timeout_seconds(request.timeout_seconds),
                    memory_access=_memory_access(principal, entry.run.intent.trip_id),
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
