from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock

from pydantic import Field, JsonValue, field_validator, model_validator

from tripchord.agents.models import (
    AgentRole,
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
)
from tripchord.domain.common import DomainModel

_MAX_MEMORY_PAYLOAD_BYTES = 8_192
_MAX_MEMORY_CONTAINER_ITEMS = 64
_MAX_MEMORY_STRING_CHARS = 2_048
_MAX_MEMORY_NESTING_DEPTH = 6
_PROMPT_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "ignore the system",
    "system prompt",
    "developer message",
    "you are chatgpt",
    "<script",
    "javascript:",
    "忽略上述",
    "忽略之前",
    "忽略以上",
    "系统提示词",
    "开发者消息",
)

# Durable preferences describe stable user intent, never a particular live
# offer.  Semantic price preferences remain allowed (for example
# ``lodging_price`` or ``price_sensitivity``); factual quote/inventory fields
# are rejected recursively before they can enter stable memory.
_DYNAMIC_PREFERENCE_KEYS = frozenset(
    {
        "price",
        "price_cents",
        "amount",
        "amount_cents",
        "total",
        "total_cents",
        "total_for_party_cents",
        "quote",
        "quote_id",
        "fare",
        "fare_id",
        "inventory",
        "availability",
        "available",
        "seat_count",
        "seats",
        "remaining_seats",
        "flight_number",
        "flight_no",
        "schedule_id",
        "departure_time",
        "arrival_time",
        "specific_flight",
    }
)
_SEMANTIC_PRICE_KEYS = frozenset({"lodging_price", "price_sensitivity", "budget_sensitivity"})
_SUPPORTED_PREFERENCE_KEYS = frozenset(
    {
        "airport_lodging_fallback",
        "checked_baggage",
        "compare_budget_options",
        "flight_connections",
        "hotel_breakfast",
        "hotel_star_rating",
        "lodging_location",
        "lodging_price",
        "lodging_quality",
        "lodging_zone_comparison",
        "price_sensitivity",
        "budget_sensitivity",
        "elder_trip_comfort",
    }
)
_PREFERENCE_EXPECTED_VALUES: dict[str, frozenset[JsonValue]] = {
    "airport_lodging_fallback": frozenset({True, False}),
    "checked_baggage": frozenset({True, False}),
    "compare_budget_options": frozenset({True, False}),
    "flight_connections": frozenset({True, False}),
    "hotel_breakfast": frozenset({True, False}),
    "hotel_star_rating": frozenset({"3_plus", "4_plus", "5_plus"}),
    "lodging_location": frozenset({"convenient_not_remote", "airport_nearby", "central"}),
    "lodging_price": frozenset({"low", "reasonable_not_high", "price_first", "balanced"}),
    "lodging_quality": frozenset({"not_basic", "standard", "premium"}),
    "lodging_zone_comparison": frozenset({True, False}),
    "price_sensitivity": frozenset({"low", "balanced", "high", "price_first"}),
    "budget_sensitivity": frozenset({"low", "balanced", "high", "price_first"}),
}


def _normalize_elder_trip_comfort(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError("elder_trip_comfort expected 必须是结构化条件偏好")
    allowed = {
        "condition",
        "avoid_transfers",
        "avoid_departures_before",
        "max_comfort_premium_cny_cents",
    }
    if set(value) != allowed:
        raise ValueError("elder_trip_comfort expected 字段不完整或超出允许范围")
    if value.get("condition") != "traveling_with_elders":
        raise ValueError("elder_trip_comfort 只能用于 traveling_with_elders 条件")
    if not isinstance(value.get("avoid_transfers"), bool):
        raise ValueError("elder_trip_comfort avoid_transfers 必须是布尔值")
    departure_before = value.get("avoid_departures_before")
    if not isinstance(departure_before, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d",
        departure_before,
    ):
        raise ValueError("elder_trip_comfort avoid_departures_before 必须是 HH:MM")
    premium = value.get("max_comfort_premium_cny_cents")
    if isinstance(premium, bool) or not isinstance(premium, int) or not 0 <= premium <= 1_000_000:
        raise ValueError("elder_trip_comfort 可接受溢价必须是 0–10000 元的意愿阈值")
    return {
        "condition": "traveling_with_elders",
        "avoid_transfers": value["avoid_transfers"],
        "avoid_departures_before": departure_before,
        "max_comfort_premium_cny_cents": premium,
    }


def _is_dynamic_preference_key(key: str) -> bool:
    normalized = key.strip().casefold()
    if normalized in _SEMANTIC_PRICE_KEYS:
        return False
    if normalized in _DYNAMIC_PREFERENCE_KEYS:
        return True
    # Keep semantic preference names such as lodging_price, but reject fields
    # that embed an exact live-fact marker (flight_price, quoted_amount, etc.).
    return any(
        marker in normalized
        for marker in (
            "total_cents",
            "quote_",
            "inventory",
            "availability",
            "seat_count",
            "flight_number",
            "schedule_id",
        )
    )


def _reject_dynamic_preference_payload(value: JsonValue, *, path: str = "value") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_dynamic_preference_key(str(key)):
                raise ValueError(
                    f"长期偏好不能包含实时价格、余位、库存或具体班次字段: {path}.{key}"
                )
            _reject_dynamic_preference_payload(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_dynamic_preference_payload(item, path=f"{path}[{index}]")


def normalize_confirmed_preference_value(key: str, value: JsonValue) -> dict[str, JsonValue]:
    """Normalize the public memory envelope and reject live facts.

    The legacy API accepted a scalar or a partial ``{mode, weight}`` object;
    those forms remain readable while all newly stored records get the same
    explicit mode/expected/weight shape.
    """

    normalized_key = key.strip()
    if not normalized_key:
        raise ValueError("preference key cannot be empty")
    if normalized_key not in _SUPPORTED_PREFERENCE_KEYS:
        raise ValueError(f"不支持的长期偏好 key: {normalized_key}")
    if _is_dynamic_preference_key(normalized_key):
        raise ValueError("长期偏好不能把实时价格、余位、库存或具体班次作为 key")
    _reject_dynamic_preference_payload(value)
    if isinstance(value, dict) and "mode" in value:
        mode = PreferenceMode(str(value["mode"]))
        expected = value.get("expected")
        weight_value = value.get("weight")
        if weight_value is not None and not isinstance(weight_value, (int, float, str)):
            raise ValueError("preference weight must be numeric")
        weight = float(weight_value) if weight_value is not None else {
            PreferenceMode.REQUIRED: 1.0,
            PreferenceMode.FORBIDDEN: 1.0,
            PreferenceMode.INDIFFERENT: 0.0,
        }.get(mode, 0.5)
    elif isinstance(value, bool):
        mode, expected, weight = PreferenceMode.REQUIRED, value, 1.0
    elif isinstance(value, str) and value in {item.value for item in PreferenceMode}:
        mode = PreferenceMode(value)
        expected = {PreferenceMode.REQUIRED: True, PreferenceMode.FORBIDDEN: False}.get(mode)
        weight = {PreferenceMode.REQUIRED: 1.0, PreferenceMode.FORBIDDEN: 1.0,
                  PreferenceMode.INDIFFERENT: 0.0}.get(mode, 0.5)
    else:
        mode, expected, weight = PreferenceMode.WEIGHTED, value, 0.5
    if not 0 <= weight <= 1:
        raise ValueError("preference weight must be between 0 and 1")
    if mode == PreferenceMode.REQUIRED and expected is None:
        expected = True
    if mode == PreferenceMode.FORBIDDEN and expected is None:
        expected = False
    if mode == PreferenceMode.INDIFFERENT:
        expected = None
        weight = 0.0
    if normalized_key == "elder_trip_comfort":
        expected = _normalize_elder_trip_comfort(expected)
    else:
        allowed_expected = _PREFERENCE_EXPECTED_VALUES[normalized_key]
        if expected is not None and isinstance(expected, (dict, list)):
            raise ValueError("长期偏好 expected 只能是受控布尔值或枚举值")
        if expected is not None and expected not in allowed_expected:
            raise ValueError(f"长期偏好 {normalized_key} 的 expected 不在允许枚举内")
    return {"mode": mode.value, "expected": expected, "weight": weight}


def confirmed_preference_constitution(
    store: MemoryStore,
    access: MemoryAccessContext,
    *,
    now: datetime | None = None,
) -> PreferenceConstitution:
    """Load only fresh, user-scoped confirmed preferences into domain rules."""

    rules: list[PreferenceRule] = []
    records = store.query(
        MemoryQuery(kinds=(MemoryKind.USER_PREFERENCE,), fresh_only=True, rag_only=True, limit=200),
        access,
        now=now,
    )
    for record in records:
        if record.scope != MemoryScope.USER or record.user_id != access.user_id:
            continue
        key = record.payload.get("key")
        raw_value = record.payload.get("value")
        if not isinstance(key, str):
            continue
        try:
            normalized = normalize_confirmed_preference_value(key, raw_value)
            normalized_weight = normalized["weight"]
            if not isinstance(normalized_weight, (int, float)):
                continue
            rules.append(
                PreferenceRule(
                    key=key.strip(),
                    mode=PreferenceMode(str(normalized["mode"])),
                    expected=normalized["expected"],
                    weight=float(normalized_weight),
                    source=PreferenceSource.EXPLICIT_LONG_TERM,
                    scope="user",
                    reason="用户已显式确认的长期偏好",
                    created_at=record.captured_at,
                )
            )
        except (TypeError, ValueError):
            # Corrupt/legacy records are not allowed to influence planning.
            continue
    return PreferenceConstitution(rules=tuple(rules)).model_copy(
        update={"rules": PreferenceConstitution(rules=tuple(rules)).effective_rules()}
    )


def _validate_memory_json(value: JsonValue, *, depth: int = 0) -> None:
    if depth > _MAX_MEMORY_NESTING_DEPTH:
        raise ValueError("memory payload nesting is too deep")
    if isinstance(value, str):
        if len(value) > _MAX_MEMORY_STRING_CHARS:
            raise ValueError("memory payload string is too long")
        return
    if isinstance(value, list):
        if len(value) > _MAX_MEMORY_CONTAINER_ITEMS:
            raise ValueError("memory payload list has too many items")
        for item in value:
            _validate_memory_json(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > _MAX_MEMORY_CONTAINER_ITEMS:
            raise ValueError("memory payload object has too many fields")
        for key, item in value.items():
            if len(key) > 120:
                raise ValueError("memory payload key is too long")
            _validate_memory_json(item, depth=depth + 1)


def _contains_prompt_injection(value: str) -> bool:
    normalized = " ".join(value.casefold().split())
    return any(marker in normalized for marker in _PROMPT_INJECTION_MARKERS)


class MemoryKind(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    USER_PREFERENCE = "user_preference"
    EVIDENCE = "evidence"
    PROVIDER_CAPABILITY = "provider_capability"


class MemoryScope(StrEnum):
    SESSION = "session"
    TRIP = "trip"
    USER = "user"
    TENANT = "tenant"


class PrivacyBoundary(StrEnum):
    USER_PRIVATE = "user_private"
    TENANT_SHARED = "tenant_shared"


class MemoryVolatility(StrEnum):
    STABLE = "stable"
    EVENT_DRIVEN = "event_driven"
    REALTIME = "realtime"


class MemoryRecord(DomainModel):
    id: str = Field(min_length=1, max_length=240)
    version: int = Field(default=1, ge=1)
    kind: MemoryKind
    scope: MemoryScope
    privacy: PrivacyBoundary = PrivacyBoundary.USER_PRIVATE
    tenant_id: str = Field(min_length=1, max_length=160)
    user_id: str | None = Field(default=None, max_length=160)
    session_id: str | None = Field(default=None, max_length=240)
    trip_id: str | None = Field(default=None, max_length=240)
    topic: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=240)
    payload: dict[str, JsonValue]
    source: str = Field(min_length=1, max_length=240)
    captured_at: datetime
    expires_at: datetime | None = None
    confidence: float = Field(default=1, ge=0, le=1)
    tags: tuple[str, ...] = Field(default=(), max_length=32)
    allowed_roles: tuple[AgentRole, ...] = ()
    volatility: MemoryVolatility = MemoryVolatility.STABLE
    rag_eligible: bool = True
    sensitive: bool = False
    tainted: bool = False
    taint_reasons: tuple[str, ...] = Field(default=(), max_length=8)
    token_cost: int = Field(default=0, ge=0)

    @field_validator("captured_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("memory timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> MemoryRecord:
        _validate_memory_json(self.payload)
        serialized_payload = json.dumps(
            self.payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(serialized_payload.encode("utf-8")) > _MAX_MEMORY_PAYLOAD_BYTES:
            raise ValueError("memory payload exceeds the 8192-byte storage limit")
        if any(len(tag) > 80 for tag in self.tags):
            raise ValueError("memory tag is too long")
        if any(not reason.strip() or len(reason) > 160 for reason in self.taint_reasons):
            raise ValueError("memory taint reason is invalid")
        suspicious = _contains_prompt_injection(f"{self.subject} {serialized_payload}")
        if suspicious and not self.tainted:
            raise ValueError("potential prompt-injection memory must be marked tainted")
        if self.tainted and not self.taint_reasons:
            raise ValueError("tainted memory requires an explicit taint reason")
        if not self.tainted and self.taint_reasons:
            raise ValueError("untainted memory cannot carry taint reasons")
        if self.tainted and self.rag_eligible:
            raise ValueError("tainted memory cannot be RAG eligible")
        if self.expires_at is not None and self.expires_at <= self.captured_at:
            raise ValueError("memory expires_at must be after captured_at")
        if self.scope == MemoryScope.SESSION and not self.session_id:
            raise ValueError("session memory requires session_id")
        if self.scope == MemoryScope.TRIP and not self.trip_id:
            raise ValueError("trip memory requires trip_id")
        if (
            self.scope in {MemoryScope.USER, MemoryScope.TRIP, MemoryScope.SESSION}
            and not self.user_id
        ):
            raise ValueError("user-scoped memory requires user_id")
        if self.privacy == PrivacyBoundary.USER_PRIVATE and not self.user_id:
            raise ValueError("private memory requires user_id")
        if self.kind == MemoryKind.WORKING and self.scope != MemoryScope.SESSION:
            raise ValueError("working memory must use session scope")
        if self.kind == MemoryKind.WORKING and self.expires_at is None:
            raise ValueError("working memory requires an explicit TTL")
        if self.volatility == MemoryVolatility.REALTIME:
            if self.expires_at is None:
                raise ValueError("realtime evidence requires an explicit TTL")
            if self.rag_eligible:
                raise ValueError("realtime facts cannot enter durable RAG knowledge")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = (now or datetime.now(UTC)).astimezone(UTC)
        captured = self.captured_at.astimezone(UTC)
        expires = self.expires_at.astimezone(UTC) if self.expires_at is not None else None
        return captured <= reference and (expires is None or reference < expires)

    @property
    def approximate_tokens(self) -> int:
        if self.token_cost:
            return self.token_cost
        serialized_length = len(str(self.payload)) + len(self.topic) + len(self.subject)
        return max(1, serialized_length // 4)


class MemoryAccessContext(DomainModel):
    tenant_id: str = Field(min_length=1)
    user_id: str | None = None
    session_id: str | None = None
    trip_id: str | None = None
    agent_role: AgentRole | None = None
    include_sensitive: bool = False


class MemoryQuery(DomainModel):
    kinds: tuple[MemoryKind, ...] = ()
    topics: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    text_terms: tuple[str, ...] = ()
    fresh_only: bool = True
    rag_only: bool = False
    limit: int = Field(default=20, ge=1, le=200)


class MemoryStore:
    """Thread-safe in-process store with explicit scope and privacy checks."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._lock = RLock()

    def upsert(self, record: MemoryRecord) -> None:
        with self._lock:
            current = self._records.get(record.id)
            if current is not None:
                if current.tenant_id != record.tenant_id or current.user_id != record.user_id:
                    raise PermissionError("memory identity boundary cannot change across versions")
                if record.version <= current.version:
                    raise ValueError("memory version must increase")
            self._records[record.id] = record

    def get(
        self,
        record_id: str,
        access: MemoryAccessContext,
        *,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        with self._lock:
            record = self._records.get(record_id)
            if record is None or not self._visible(record, access):
                return None
            if not record.is_fresh(now):
                return None
            return record

    def query(
        self,
        query: MemoryQuery,
        access: MemoryAccessContext,
        *,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        reference = now or datetime.now(UTC)
        with self._lock:
            records = tuple(self._records.values())
        selected = [
            record
            for record in records
            if self._visible(record, access)
            and (not query.kinds or record.kind in query.kinds)
            and (not query.topics or record.topic in query.topics)
            and (not query.tags or bool(set(query.tags) & set(record.tags)))
            and (not query.fresh_only or record.is_fresh(reference))
            and (not query.rag_only or record.rag_eligible)
            and self._matches_terms(record, query.text_terms)
        ]
        selected.sort(
            key=lambda record: (
                -self._relevance(record, query, reference),
                record.topic,
                record.subject,
                -record.version,
            )
        )
        return tuple(selected[: query.limit])

    def delete(self, record_id: str, access: MemoryAccessContext) -> bool:
        """Revoke one user-owned record without exposing whether others exist."""

        with self._lock:
            record = self._records.get(record_id)
            if (
                record is None
                or access.user_id is None
                or record.user_id != access.user_id
                or not self._visible(record, access)
            ):
                return False
            self._records.pop(record_id, None)
            return True

    def purge_expired(self, *, now: datetime | None = None) -> int:
        reference = now or datetime.now(UTC)
        with self._lock:
            expired = [
                record_id
                for record_id, record in self._records.items()
                if not record.is_fresh(reference)
            ]
            for record_id in expired:
                self._records.pop(record_id, None)
        return len(expired)

    def _visible(self, record: MemoryRecord, access: MemoryAccessContext) -> bool:
        if record.tenant_id != access.tenant_id:
            return False
        if record.privacy == PrivacyBoundary.USER_PRIVATE and record.user_id != access.user_id:
            return False
        if record.scope == MemoryScope.SESSION and record.session_id != access.session_id:
            return False
        if record.scope == MemoryScope.TRIP and record.trip_id != access.trip_id:
            return False
        if record.scope == MemoryScope.USER and record.user_id != access.user_id:
            return False
        if record.allowed_roles and access.agent_role not in record.allowed_roles:
            return False
        return not record.sensitive or access.include_sensitive

    @staticmethod
    def _matches_terms(record: MemoryRecord, terms: tuple[str, ...]) -> bool:
        if not terms:
            return True
        haystack = " ".join(
            (record.topic, record.subject, *record.tags, str(record.payload))
        ).casefold()
        return any(term.casefold() in haystack for term in terms if term.strip())

    @staticmethod
    def _relevance(record: MemoryRecord, query: MemoryQuery, now: datetime) -> float:
        topic_match = 1.0 if record.topic in query.topics else 0.0
        tag_match = len(set(record.tags) & set(query.tags)) / max(1, len(query.tags))
        age_seconds = max(0.0, (now - record.captured_at).total_seconds())
        recency = 1 / (1 + age_seconds / 86_400)
        return 3 * topic_match + 2 * tag_match + record.confidence + recency


class ProviderCapabilitySeed(DomainModel):
    """Audited, deterministic provider facts that may enter stable RAG."""

    provider: str = Field(min_length=1, max_length=80)
    verticals: tuple[str, ...] = Field(min_length=1, max_length=8)
    read_only: bool = True
    requires_authenticated_browser_session: bool
    booking_supported: bool = False
    capability_version: str = Field(min_length=1, max_length=80)


def seed_provider_capability_records(
    store: MemoryStore,
    *,
    tenant_id: str,
    seeds: tuple[ProviderCapabilitySeed, ...],
    now: datetime | None = None,
) -> tuple[MemoryRecord, ...]:
    """Idempotently materialize the runtime capability registry into RAG.

    This is intentionally a seed path, not learned memory: only facts from the
    deterministic provider registry are accepted, and a changed capability
    payload creates a new record version.
    """

    captured_at = now or datetime.now(UTC)
    access = MemoryAccessContext(
        tenant_id=tenant_id,
        agent_role=AgentRole.QUERY_STRATEGIST,
    )
    seeded: list[MemoryRecord] = []
    for seed in seeds:
        digest = hashlib.sha256(f"{tenant_id}|{seed.provider}".encode()).hexdigest()[:24]
        record_id = f"memory:provider-capability:{digest}"
        payload: dict[str, JsonValue] = {
            "provider": seed.provider,
            "verticals": list(seed.verticals),
            "read_only": seed.read_only,
            "requires_authenticated_browser_session": (
                seed.requires_authenticated_browser_session
            ),
            "booking_supported": seed.booking_supported,
            "capability_version": seed.capability_version,
        }
        current = store.get(record_id, access, now=captured_at)
        if current is not None and current.payload == payload:
            seeded.append(current)
            continue
        record = MemoryRecord(
            id=record_id,
            version=current.version + 1 if current is not None else 1,
            kind=MemoryKind.PROVIDER_CAPABILITY,
            scope=MemoryScope.TENANT,
            privacy=PrivacyBoundary.TENANT_SHARED,
            tenant_id=tenant_id,
            topic="provider_capability",
            subject=seed.provider,
            payload=payload,
            source="tripchord:runtime-capability-registry",
            captured_at=captured_at,
            confidence=1,
            tags=("travel", "provider", *seed.verticals),
            allowed_roles=(
                AgentRole.QUERY_STRATEGIST,
                AgentRole.SEARCH_SUPERVISOR,
                AgentRole.CANDIDATE_CURATOR,
                AgentRole.REPAIR_STRATEGIST,
                AgentRole.ORCHESTRATOR,
            ),
            volatility=MemoryVolatility.STABLE,
            rag_eligible=True,
        )
        store.upsert(record)
        seeded.append(record)
    return tuple(seeded)
