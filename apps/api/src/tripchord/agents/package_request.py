from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, cast

from pydantic import Field, JsonValue, TypeAdapter, ValidationError, model_validator

from tripchord.agents.agent_budget import AgentBudgetExceeded, current_agent_budget
from tripchord.agents.model_gateway import (
    ModelClient,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    compact_json,
)
from tripchord.agents.models import (
    AgentRole,
    EvidenceRecord,
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
)
from tripchord.domain.common import DomainModel
from tripchord.planning.flexible_dates import FlexibleTravelWindow
from tripchord.planning.package import PackageIntent

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_FULL_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})"
    r"\s*(?:月|[-/.])\s*(?P<day>\d{1,2})\s*日?"
)
_MONTH_PATTERN = re.compile(r"(?P<year>20\d{2})\s*(?:年|[-/.])\s*(?P<month>\d{1,2})\s*月?")
_SAME_MONTH_RANGE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*年\s*(?P<month>\d{1,2})\s*月\s*"
    r"(?P<start>\d{1,2})\s*日?\s*(?:-|–|—|~|～|到|至)\s*"
    r"(?P<end>\d{1,2})\s*日"
)
_DATE_WINDOW_BOUNDARY_PATTERN = re.compile(
    r"(?P<start_year>20\d{2})\s*(?:年|[-/.])\s*(?P<start_month>\d{1,2})"
    r"\s*(?:月|[-/.])\s*(?P<start_day>\d{1,2})\s*日?\s*(?:起|开始).*?"
    r"(?:需在|截至|不晚于|最晚在|最晚于).*?"
    r"(?P<end_year>20\d{2})\s*(?:年|[-/.])\s*(?P<end_month>\d{1,2})"
    r"\s*(?:月|[-/.])\s*(?P<end_day>\d{1,2})\s*日?"
    r"\s*(?:边界(?:内完成)?|之前|前|内)?"
)
_BARE_MONTH_PATTERN = re.compile(r"(?<!\d)(?P<month>\d{1,2})\s*月")
_MONTH_DAY_PATTERN = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日"
)
_EXPLICIT_RETURN_HOME_BEFORE_DEADLINE = (
    r"(?:回到[^,，。；;]{0,12}?|"
    r"回(?!程|返|来|去)[\u4e00-\u9fffA-Za-z]{1,12}?)"
)
_EXPLICIT_RETURN_HOME_AFTER_DATE = (
    r"(?:回到[^,，。；;]{0,12}|"
    r"回(?!程|返|来|去)[\u4e00-\u9fffA-Za-z]{1,12})"
)
_ARRIVAL_OR_COMPLETION_BEFORE_DATE = re.compile(
    rf"(?:{_EXPLICIT_RETURN_HOME_BEFORE_DEADLINE}|抵达[^,，。；;]{{0,12}}?|"
    r"到达[^,，。；;]{0,12}?|结束|完成)"
    r"(?:不晚于|最晚(?:在|于)?|截至|截止(?:到)?)\s*$"
)
_ARRIVAL_DEADLINE_BEFORE_DATE = re.compile(
    r"(?:不晚于|最晚(?:在|于)?|截至|截止(?:到)?|需在)\s*$"
)
_ARRIVAL_OR_COMPLETION_AFTER_DATE = re.compile(
    r"^\s*(?:边界)?\s*(?:内|前|之前|以前)?\s*"
    rf"(?:{_EXPLICIT_RETURN_HOME_AFTER_DATE}|抵达|到达|结束|完成)"
)
_DURATION_PATTERN = re.compile(
    r"(?P<minimum>\d{1,2})\s*(?:-|–|—|~|～|到|至)\s*"
    r"(?P<maximum>\d{1,2})\s*(?P<unit>晚|夜|天)"
)
_SINGLE_DURATION_PATTERN = re.compile(r"(?P<count>\d{1,2})\s*(?P<unit>晚|夜|天)")
_BUDGET_AMOUNT_PATTERN = (
    r"(?P<symbol>[¥￥$])?\s*(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<scale>万|千)?\s*(?P<currency>人民币|元|CNY|RMB|美元|USD)?"
)
_BUDGET_PATTERN = re.compile(
    r"(?:总预算|预算(?:上限)?)\s*(?:为|是|不超过|控制在|约|大概|[:：])?\s*"
    + _BUDGET_AMOUNT_PATTERN,
    re.IGNORECASE,
)
_SCOPED_BUDGET_PATTERN = re.compile(
    r"(?P<scope>人均|每人|单人|机票|航班|酒店|住宿)\s*(?:总)?预算(?:上限)?"
    r"\s*(?:为|是|不超过|控制在|约|大概|[:：])?\s*" + _BUDGET_AMOUNT_PATTERN,
    re.IGNORECASE,
)
_FIELD_LABELS = (
    "出发城市",
    "从哪里出发",
    "出发日期",
    "出发时间",
    "出发窗口",
    "开始日期",
    "目的地",
    "出发地",
    "到达地",
    "去哪里",
    "返回日期",
    "返程日期",
    "出行人数",
    "房间数",
    "总预算",
    "去程",
    "返程",
    "回程",
    "人数",
    "酒店",
    "偏好",
    "预算",
)
_FIELD_LABEL_ALTERNATION = "|".join(
    re.escape(item) for item in sorted(_FIELD_LABELS, key=len, reverse=True)
)
_CRITICAL_FIELDS = frozenset(
    {
        "origin",
        "destination",
        "earliest_departure",
        "latest_departure",
        "min_nights",
        "max_nights",
        "adults",
        "rooms",
    }
)
_MODEL_FACT_FIELDS = (
    "origin",
    "destination",
    "earliest_departure",
    "latest_departure",
    "min_nights",
    "max_nights",
    "adults",
    "children_ages",
    "rooms",
    "currency",
    "budget_cents",
    "require_checked_baggage",
    "require_breakfast",
)
_LOCATION_IATA_BY_ALIAS = {
    "杭州": "HGH",
    "hangzhou": "HGH",
    "hgh": "HGH",
    "马累": "MLE",
    "male": "MLE",
    "malé": "MLE",
    "mle": "MLE",
    "马尔代夫": "MLE",
}
_CHINESE_SMALL_NUMBERS = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4}


class PackageRequestState(StrEnum):
    READY = "ready"
    HUMAN_BLOCK = "human_block"


class RequirementFactSource(StrEnum):
    EXPLICIT_TEXT = "explicit_text"
    STRUCTURED_USER_OVERRIDE = "structured_user_override"
    DETERMINISTIC_DERIVATION = "deterministic_derivation"
    SYSTEM_DEFAULT = "system_default"


class ExtractedRequirementFact(DomainModel):
    field: str = Field(min_length=1)
    value: JsonValue
    source: RequirementFactSource
    evidence_text: str = Field(min_length=1)
    explicit: bool


class UnresolvedRequirement(DomainModel):
    field: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    critical: bool
    model_proposal: JsonValue | None = None


class RequirementConflict(DomainModel):
    field: str = Field(min_length=1)
    deterministic_value: JsonValue
    model_value: JsonValue
    reason: str = Field(min_length=1)


class PackageRequirementRequest(DomainModel):
    text: str = Field(min_length=1)
    trip_id: str | None = None
    reference_date: date = Field(default_factory=date.today)
    breakfast_mode: PreferenceMode | None = None
    breakfast_weight: float | None = Field(default=None, ge=0, le=1)
    # Optional structured override; the same canonical tuple is also extracted
    # from Chinese text below.
    children_ages: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_breakfast_override(self) -> PackageRequirementRequest:
        if self.breakfast_mode is None or self.breakfast_weight is None:
            return self
        canonical_weight = {
            PreferenceMode.REQUIRED: 1.0,
            PreferenceMode.FORBIDDEN: 1.0,
            PreferenceMode.INDIFFERENT: 0.0,
        }.get(self.breakfast_mode)
        if canonical_weight is not None and self.breakfast_weight != canonical_weight:
            raise ValueError(
                f"{self.breakfast_mode.value} breakfast mode requires weight "
                f"{canonical_weight:g}; use weighted mode for a tunable weight"
            )
        return self

    @model_validator(mode="after")
    def validate_children_ages(self) -> PackageRequirementRequest:
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        return self


class PackageIntentTemplate(DomainModel):
    """Package intent fields that are stable before a flexible date pair is selected."""

    trip_id: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    rooms: int = Field(ge=1, le=8)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    budget_cents: int | None = Field(default=None, ge=0)
    require_checked_baggage: bool | None = None
    allow_connections: bool | None = None
    require_breakfast: bool | None = None
    require_non_basic_lodging: bool = False
    require_non_remote_lodging: bool = False
    breakfast_preference_mode: PreferenceMode | None = None
    breakfast_preference_weight: float | None = Field(default=None, ge=0, le=1)
    minimum_arrival_to_boat_minutes: int = Field(default=120, ge=0, le=1440)
    minimum_airport_buffer_minutes: int = Field(default=180, ge=0, le=1440)
    latest_arrival_date: date | None = None

    def materialize(self, departure_date: date, return_date: date) -> PackageIntent:
        return PackageIntent(
            trip_id=self.trip_id,
            origin=self.origin,
            destination=self.destination,
            start_date=departure_date,
            end_date=return_date,
            adults=self.adults,
            children=self.children,
            children_ages=self.children_ages,
            infants=self.infants,
            rooms=self.rooms,
            currency=self.currency,
            budget_cents=self.budget_cents,
            require_checked_baggage=self.require_checked_baggage,
            allow_connections=self.allow_connections,
            require_breakfast=self.require_breakfast,
            require_non_basic_lodging=self.require_non_basic_lodging,
            require_non_remote_lodging=self.require_non_remote_lodging,
            breakfast_preference_mode=self.breakfast_preference_mode,
            breakfast_preference_weight=self.breakfast_preference_weight,
            minimum_arrival_to_boat_minutes=self.minimum_arrival_to_boat_minutes,
            minimum_airport_buffer_minutes=self.minimum_airport_buffer_minutes,
            latest_arrival_date=self.latest_arrival_date,
        )


# These are the preference keys whose semantics are represented directly by
# PackageIntentTemplate.  Keep this list deliberately narrow: a preference
# that is not represented here must remain visible as an unapplied diagnostic
# rather than being presented as if it affected live ranking.
_INTENT_TEMPLATE_PREFERENCE_KEYS = frozenset(
    {
        "checked_baggage",
        "flight_connections",
        "hotel_breakfast",
        "lodging_quality",
        "lodging_location",
    }
)


def project_preferences_to_intent_template(
    template: PackageIntentTemplate,
    preferences: PreferenceConstitution,
) -> tuple[PackageIntentTemplate, tuple[str, ...]]:
    """Project effective typed preferences into executable package intent.

    The constitution has already resolved current-trip rules over durable
    rules by source priority.  This function therefore only translates the
    resulting effective rule; it never lets a durable rule overwrite an
    explicit current request.  Unsupported keys are returned for a diagnostic
    and are intentionally not used to alter the intent.
    """

    updates: dict[str, object] = {}

    baggage = preferences.effective("checked_baggage")
    if baggage is not None and baggage.mode in {
        PreferenceMode.REQUIRED,
        PreferenceMode.FORBIDDEN,
    }:
        updates["require_checked_baggage"] = (
            baggage.expected
            if isinstance(baggage.expected, bool)
            else baggage.mode == PreferenceMode.REQUIRED
        )

    connections = preferences.effective("flight_connections")
    if connections is not None and connections.mode in {
        PreferenceMode.REQUIRED,
        PreferenceMode.FORBIDDEN,
    }:
        updates["allow_connections"] = (
            connections.expected
            if isinstance(connections.expected, bool)
            else connections.mode == PreferenceMode.REQUIRED
        )

    breakfast = preferences.effective("hotel_breakfast")
    if breakfast is not None:
        if breakfast.mode in {PreferenceMode.REQUIRED, PreferenceMode.FORBIDDEN}:
            updates["require_breakfast"] = (
                breakfast.expected
                if isinstance(breakfast.expected, bool)
                else breakfast.mode == PreferenceMode.REQUIRED
            )
        elif breakfast.mode == PreferenceMode.WEIGHTED:
            updates["require_breakfast"] = None
            updates["breakfast_preference_mode"] = breakfast.mode
            updates["breakfast_preference_weight"] = breakfast.weight
        elif breakfast.mode == PreferenceMode.INDIFFERENT:
            updates["require_breakfast"] = None
            updates["breakfast_preference_mode"] = breakfast.mode
            updates["breakfast_preference_weight"] = 0.0

    lodging_quality = preferences.effective("lodging_quality")
    if (
        lodging_quality is not None
        and lodging_quality.mode == PreferenceMode.REQUIRED
        and lodging_quality.expected == "not_basic"
    ):
        # This preference is an executable deterministic safety boundary.  A
        # model may describe trade-offs among eligible rooms, but it cannot
        # re-admit a room classified as windowless/basic.
        updates["require_non_basic_lodging"] = True

    lodging_location = preferences.effective("lodging_location")
    if (
        lodging_location is not None
        and lodging_location.mode == PreferenceMode.REQUIRED
        and lodging_location.expected == "convenient_not_remote"
    ):
        # Location convenience is an evidence-backed hard gate.  An exact
        # place search is not enough: a quote still needs an explicit address
        # plus nearby service/commercial/transport evidence.
        updates["require_non_remote_lodging"] = True

    unsupported = tuple(
        sorted(
            rule.key
            for rule in preferences.effective_rules()
            if rule.key not in _INTENT_TEMPLATE_PREFERENCE_KEYS
        )
    )
    return template.model_copy(update=updates), unsupported


class ModelPreferenceProposal(DomainModel):
    key: str = Field(min_length=1)
    mode: PreferenceMode
    weight: float = Field(default=0.5, ge=0, le=1)
    expected: JsonValue | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mode_semantics(self) -> ModelPreferenceProposal:
        canonical_weight = {
            PreferenceMode.REQUIRED: 1.0,
            PreferenceMode.FORBIDDEN: 1.0,
            PreferenceMode.INDIFFERENT: 0.0,
        }.get(self.mode)
        if canonical_weight is not None and self.weight != canonical_weight:
            raise ValueError(
                f"{self.mode.value} preference requires canonical weight {canonical_weight:g}"
            )
        if self.mode == PreferenceMode.INDIFFERENT and self.expected is not None:
            raise ValueError("indifferent preference must not declare an expected value")
        if (
            self.mode in {PreferenceMode.REQUIRED, PreferenceMode.FORBIDDEN}
            and self.expected is None
        ):
            raise ValueError("required/forbidden preference must declare an expected value")
        return self


class ModelPackageRequirementProposal(DomainModel):
    origin: str | None = None
    destination: str | None = None
    earliest_departure: date | None = None
    latest_departure: date | None = None
    min_nights: int | None = Field(default=None, ge=1, le=60)
    max_nights: int | None = Field(default=None, ge=1, le=60)
    adults: int | None = Field(default=None, ge=1, le=20)
    children: int | None = Field(default=None, ge=0, le=20)
    children_ages: tuple[int, ...] | None = None
    infants: int | None = Field(default=None, ge=0, le=10)
    rooms: int | None = Field(default=None, ge=1, le=8)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    budget_cents: int | None = Field(default=None, ge=0)
    require_checked_baggage: bool | None = None
    require_breakfast: bool | None = None
    preferences: tuple[ModelPreferenceProposal, ...] = ()
    unresolved: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_ranges_and_preferences(self) -> ModelPackageRequirementProposal:
        if self.children_ages is not None and any(
            age < 0 or age > 17 for age in self.children_ages
        ):
            raise ValueError("children ages must be between 0 and 17")
        if self.children is not None and self.children > 0 and (
            self.children_ages is None or len(self.children_ages) != self.children
        ):
            raise ValueError("children_ages must contain exactly one age for every child")
        if (
            self.earliest_departure is not None
            and self.latest_departure is not None
            and self.latest_departure < self.earliest_departure
        ):
            raise ValueError("latest_departure must not be before earliest_departure")
        if (
            self.min_nights is not None
            and self.max_nights is not None
            and self.max_nights < self.min_nights
        ):
            raise ValueError("max_nights must not be less than min_nights")
        keys = tuple(item.key for item in self.preferences)
        if len(keys) != len(set(keys)):
            raise ValueError("model preference proposal keys must be unique")
        return self


class HybridPackageRequirementResult(DomainModel):
    state: PackageRequestState
    window: FlexibleTravelWindow | None
    intent_template: PackageIntentTemplate | None
    preferences: PreferenceConstitution
    facts: tuple[ExtractedRequirementFact, ...]
    model_proposal: ModelPackageRequirementProposal | None = None
    unresolved: tuple[UnresolvedRequirement, ...] = ()
    conflicts: tuple[RequirementConflict, ...] = ()
    context_evidence: tuple[EvidenceRecord, ...] = Field(min_length=1)
    claim_boundary: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_state(self) -> HybridPackageRequirementResult:
        blocking_unresolved = any(item.critical for item in self.unresolved)
        if self.state == PackageRequestState.READY:
            if self.window is None or self.intent_template is None:
                raise ValueError("ready result requires a window and intent template")
            if blocking_unresolved or self.conflicts:
                raise ValueError("ready result cannot contain blocking uncertainty")
        elif not blocking_unresolved and not self.conflicts:
            raise ValueError("human_block requires a critical unresolved field or conflict")
        return self


@dataclass
class _Draft:
    values: dict[str, object] = field(default_factory=dict)
    facts: dict[str, ExtractedRequirementFact] = field(default_factory=dict)
    preferences: dict[str, PreferenceRule] = field(default_factory=dict)
    conflicts: list[RequirementConflict] = field(default_factory=list)
    unresolved: list[UnresolvedRequirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    latest_return_date: date | None = None
    latest_arrival_date: date | None = None
    return_date_targets: tuple[date, ...] = ()


class HybridPackageRequirementAgent:
    """Deterministic-first Chinese package request parser with an optional model proposer."""

    def __init__(
        self,
        *,
        model_client: ModelClient | None = None,
        model_router: ModelRouter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if model_client is not None and model_router is not None:
            raise ValueError("provide either model_client or model_router, not both")
        self._model_client = model_client
        self._model_router = model_router
        self._now = now or (lambda: datetime.now(UTC))

    async def parse(
        self,
        request: PackageRequirementRequest | str,
        *,
        trip_id: str | None = None,
        breakfast_mode: PreferenceMode | None = None,
        breakfast_weight: float | None = None,
    ) -> HybridPackageRequirementResult:
        captured_at = self._utc_now()
        if isinstance(request, PackageRequirementRequest):
            normalized_request = request
            if "reference_date" not in request.model_fields_set:
                normalized_request = request.model_copy(
                    update={"reference_date": captured_at.date()}
                )
        else:
            normalized_request = PackageRequirementRequest(
                text=request,
                trip_id=trip_id,
                reference_date=captured_at.date(),
                breakfast_mode=breakfast_mode,
                breakfast_weight=breakfast_weight,
            )
        draft = self._extract_deterministic(normalized_request, captured_at)
        proposal: ModelPackageRequirementProposal | None = None
        model_response: ModelResponse | None = None
        model_error: str | None = None
        if self._model_client is not None or self._model_router is not None:
            budget = current_agent_budget()
            try:
                if budget is not None:
                    await budget.admit("interpret-package-requirements", AgentRole.CONTEXT)
            except AgentBudgetExceeded as exc:
                model_error = f"需求理解 Agent 未获运行预算：{exc}"
            else:
                proposal, model_response, model_error = await self._propose_with_model(
                    normalized_request,
                    draft,
                )
            if proposal is not None:
                self._reconcile_model(proposal, draft, captured_at)
            elif model_error is not None:
                draft.unresolved.append(
                    UnresolvedRequirement(
                        field="model_proposal",
                        reason=model_error,
                        critical=False,
                    )
                )

        children = self._int_value(draft.values.get("children", 0))
        ages = draft.values.get("children_ages")
        if children > 0 and (
            not isinstance(ages, (list, tuple)) or len(ages) != children
        ):
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="children_ages",
                    reason="儿童年龄必须逐一提供且与儿童人数一致，未知时不得发布完整 party 总价",
                    critical=True,
                ),
            )

        self._add_missing_critical_fields(draft, proposal)
        window = self._build_window(draft)
        intent_template = self._build_intent_template(normalized_request, draft)
        blocking = bool(draft.conflicts) or any(item.critical for item in draft.unresolved)
        breakfast_rule = draft.preferences.get("hotel_breakfast")
        if (
            blocking
            and breakfast_rule is not None
            and breakfast_rule.mode == PreferenceMode.WEIGHTED
        ):
            self._mark_breakfast_weight_not_applied(draft)
        state = PackageRequestState.HUMAN_BLOCK if blocking else PackageRequestState.READY
        facts = tuple(draft.facts[key] for key in sorted(draft.facts))
        preferences = PreferenceConstitution(
            rules=tuple(draft.preferences[key] for key in sorted(draft.preferences))
        )
        evidence = self._context_evidence(
            facts=facts,
            proposal=proposal,
            model_response=model_response,
            unresolved=tuple(draft.unresolved),
            conflicts=tuple(draft.conflicts),
            captured_at=captured_at,
        )
        return HybridPackageRequirementResult(
            state=state,
            window=window,
            intent_template=intent_template,
            preferences=preferences,
            facts=facts,
            model_proposal=proposal,
            unresolved=tuple(draft.unresolved),
            conflicts=tuple(draft.conflicts),
            context_evidence=evidence,
            claim_boundary=self._claim_boundary(state, draft, model_error),
        )

    def _extract_deterministic(
        self,
        request: PackageRequirementRequest,
        captured_at: datetime,
    ) -> _Draft:
        draft = _Draft()
        text = request.text.strip()
        self._extract_locations(text, draft)
        self._normalize_destination_scope(draft)
        self._resolve_location_codes(draft)
        self._extract_dates(text, request.reference_date, draft)
        self._validate_departure_recency(draft, request.reference_date)
        self._extract_duration(text, draft)
        self._apply_return_boundary(draft)
        self._extract_party(text, draft)
        if request.children_ages:
            self._set_fact(
                draft, "children_ages", list(request.children_ages),
                RequirementFactSource.STRUCTURED_USER_OVERRIDE,
                "用户通过结构化控件提供儿童年龄", overwrite=True,
            )
        self._extract_budget_and_currency(text, draft)
        self._extract_baggage(text, draft, captured_at)
        self._extract_breakfast(text, draft, captured_at)
        self._extract_other_preferences(text, draft, captured_at)
        self._apply_breakfast_override(request, draft, captured_at)
        if "currency" not in draft.values:
            self._set_fact(
                draft,
                "currency",
                "CNY",
                RequirementFactSource.SYSTEM_DEFAULT,
                "默认比较币种为 CNY；这不是用户明确指定的币种",
                explicit=False,
            )
        self._validate_exact_date_duration(draft)
        return draft

    def _extract_locations(self, text: str, draft: _Draft) -> None:
        labelled_fields = {
            "origin": ("出发地", "出发城市", "从哪里出发"),
            "destination": ("目的地", "到达地", "去哪里"),
        }
        for fact_field, labels in labelled_fields.items():
            matches = self._label_values(text, labels)
            self._set_unique_text_match(draft, fact_field, matches)
        if "origin" not in draft.values or "destination" not in draft.values:
            natural = re.search(
                r"从\s*([\u4e00-\u9fffA-Za-z]{2,20}?)(?:出发)?\s*"
                r"(?:去|到|飞往)\s*([\u4e00-\u9fffA-Za-z]{2,20}?)"
                r"(?=\s*(?:玩|旅行|旅游|\d|，|,|。|$))",
                text,
            )
            if natural is not None:
                if "origin" not in draft.values:
                    self._set_fact(
                        draft,
                        "origin",
                        natural.group(1),
                        RequirementFactSource.EXPLICIT_TEXT,
                        natural.group(0),
                    )
                if "destination" not in draft.values:
                    self._set_fact(
                        draft,
                        "destination",
                        natural.group(2),
                        RequirementFactSource.EXPLICIT_TEXT,
                        natural.group(0),
                    )
        if "origin" not in draft.values:
            origin = re.search(
                r"从\s*([\u4e00-\u9fffA-Za-z]{2,20})\s*出发",
                text,
            )
            if origin is not None:
                self._set_fact(
                    draft,
                    "origin",
                    origin.group(1),
                    RequirementFactSource.EXPLICIT_TEXT,
                    origin.group(0),
                )
        if "origin" not in draft.values:
            known_aliases = sorted(
                (alias for alias in _LOCATION_IATA_BY_ALIAS if len(alias) >= 2),
                key=len,
                reverse=True,
            )
            origin = re.search(
                rf"(?:从\s*)?(?P<origin>{'|'.join(map(re.escape, known_aliases))})\s*出发",
                text,
                flags=re.IGNORECASE,
            )
            if origin is not None:
                self._set_fact(
                    draft,
                    "origin",
                    origin.group("origin"),
                    RequirementFactSource.EXPLICIT_TEXT,
                    origin.group(0),
                )
        if "destination" not in draft.values:
            aliases = sorted(_LOCATION_IATA_BY_ALIAS, key=len, reverse=True)
            destination = re.search(
                rf"(?:规划|前往|去往|去|到|飞往)\s*"
                rf"(?P<destination>{'|'.join(map(re.escape, aliases))})"
                rf"|[，,、]\s*(?P<after_comma>{'|'.join(map(re.escape, aliases))})",
                text,
                flags=re.IGNORECASE,
            )
            if destination is not None:
                value = destination.group("destination") or destination.group("after_comma")
                self._set_fact(
                    draft,
                    "destination",
                    value,
                    RequirementFactSource.EXPLICIT_TEXT,
                    destination.group(0),
                )

    def _normalize_destination_scope(self, draft: _Draft) -> None:
        """Keep a travel scope qualifier out of the gateway identity.

        ``马尔代夫周边游`` describes a trip scope, not a different airport or
        destination identity.  The gateway remains the trusted MALÉ mapping;
        island choices are represented by lodging evidence later in planning.
        """
        destination = draft.values.get("destination")
        if not isinstance(destination, str):
            return
        normalized = re.sub(r"(?:周边)?游$", "", destination.strip())
        if normalized and normalized != destination:
            self._set_fact(
                draft,
                "destination",
                normalized,
                RequirementFactSource.EXPLICIT_TEXT,
                "目的地中的“周边游”是行程范围修饰，不改变马尔代夫 gateway 身份",
                overwrite=True,
            )

    def _resolve_location_codes(self, draft: _Draft) -> None:
        for location_field, code_field in (
            ("origin", "origin_code"),
            ("destination", "destination_code"),
        ):
            raw_location = draft.values.get(location_field)
            if not isinstance(raw_location, str):
                continue
            location = raw_location.strip()
            normalized = re.sub(r"\s+", "", location).casefold()
            code: str | None
            if (
                len(location) == 3
                and location.isascii()
                and location.isalpha()
                and location == location.upper()
            ):
                code = location
                source = RequirementFactSource.EXPLICIT_TEXT
                evidence_text = f"用户明确提供三字母 IATA：{location} → {code}"
                explicit = True
            else:
                code = _LOCATION_IATA_BY_ALIAS.get(normalized)
                source = RequirementFactSource.DETERMINISTIC_DERIVATION
                evidence_text = f"受信地点身份表命中：{location} → {code}"
                explicit = False
            if code is None:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field=code_field,
                        reason=(
                            f"地点“{location}”未命中受信 IATA 身份表；"
                            "为避免模型猜测或伪造机场代码，已阻塞实时航班搜索"
                        ),
                        critical=True,
                    ),
                )
                continue
            self._set_fact(
                draft,
                code_field,
                code,
                source,
                evidence_text,
                explicit=explicit,
            )
            draft.notes.append("地点 IATA 仅来自用户明示或受信身份表，未使用模型猜测")

    def _extract_dates(
        self,
        text: str,
        reference_date: date,
        draft: _Draft,
    ) -> None:
        arrival_boundaries = self._explicit_latest_arrival_boundaries(
            text,
            reference_date,
        )
        if arrival_boundaries:
            latest_arrival, evidence_text = arrival_boundaries[0]
            draft.latest_arrival_date = latest_arrival
            self._set_fact(
                draft,
                "latest_arrival_date",
                latest_arrival,
                RequirementFactSource.EXPLICIT_TEXT,
                evidence_text,
            )
            unique_arrivals = tuple(
                dict.fromkeys(value for value, _ in arrival_boundaries)
            )
            if len(unique_arrivals) > 1:
                draft.conflicts.append(
                    RequirementConflict(
                        field="latest_arrival_date",
                        deterministic_value=unique_arrivals[0].isoformat(),
                        model_value=unique_arrivals[1].isoformat(),
                        reason="用户输入了多个不同的明确抵达或完成边界",
                    )
                )
        same_month_range = _SAME_MONTH_RANGE_PATTERN.search(text)
        if same_month_range is not None:
            try:
                range_departure = date(
                    int(same_month_range.group("year")),
                    int(same_month_range.group("month")),
                    int(same_month_range.group("start")),
                )
                range_return = date(
                    int(same_month_range.group("year")),
                    int(same_month_range.group("month")),
                    int(same_month_range.group("end")),
                )
            except ValueError:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field="date_window",
                        reason=(
                            f"用户提供的日期范围无效：{same_month_range.group(0)}；"
                            "禁止降级成整月窗口"
                        ),
                        critical=True,
                    ),
                )
                return
            else:
                self._set_fact(
                    draft,
                    "earliest_departure",
                    range_departure,
                    RequirementFactSource.EXPLICIT_TEXT,
                    same_month_range.group(0),
                )
                self._set_fact(
                    draft,
                    "latest_departure",
                    range_departure,
                    RequirementFactSource.EXPLICIT_TEXT,
                    same_month_range.group(0),
                )
                self._set_fact(
                    draft,
                    "exact_return_date",
                    range_return,
                    RequirementFactSource.EXPLICIT_TEXT,
                    same_month_range.group(0),
                )
                return
        semantic_window = _DATE_WINDOW_BOUNDARY_PATTERN.search(text)
        if semantic_window is not None:
            try:
                earliest_departure = date(
                    int(semantic_window.group("start_year")),
                    int(semantic_window.group("start_month")),
                    int(semantic_window.group("start_day")),
                )
                latest_arrival = date(
                    int(semantic_window.group("end_year")),
                    int(semantic_window.group("end_month")),
                    int(semantic_window.group("end_day")),
                )
            except ValueError:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field="date_window",
                        reason=(
                            f"用户提供的日期窗口无效：{semantic_window.group(0)}；"
                            "禁止降级成固定出返日期"
                        ),
                        critical=True,
                    ),
                )
                return
            self._set_fact(
                draft,
                "earliest_departure",
                earliest_departure,
                RequirementFactSource.EXPLICIT_TEXT,
                semantic_window.group(0),
            )
            draft.latest_return_date = latest_arrival
            draft.return_date_targets = (
                latest_arrival - timedelta(days=1),
                latest_arrival,
            )
            return
        relative_departure = re.search(r"(?:从\s*)?明天(?:开始|起)?", text)
        deadline = re.search(
            r"(?:到|截至|不晚于)\s*(?P<month>\d{1,2})\s*月\s*"
            r"(?P<day>\d{1,2})\s*日?\s*(?:前)?",
            text,
        )
        if (
            (relative_departure is not None or deadline is not None)
            and _FULL_DATE_PATTERN.search(text) is None
        ):
            relative_departure_date: date | None = None
            if relative_departure is not None:
                relative_departure_date = reference_date + timedelta(days=1)
                self._set_fact(
                    draft,
                    "earliest_departure",
                    relative_departure_date,
                    RequirementFactSource.EXPLICIT_TEXT,
                    relative_departure.group(0),
                )
                self._set_fact(
                    draft,
                    "latest_departure",
                    relative_departure_date,
                    RequirementFactSource.EXPLICIT_TEXT,
                    relative_departure.group(0),
                )
            if deadline is not None:
                month = int(deadline.group("month"))
                day = int(deadline.group("day"))
                year = (
                    reference_date.year
                    if month >= reference_date.month
                    else reference_date.year + 1
                )
                try:
                    return_deadline = date(year, month, day)
                except ValueError:
                    self._add_unresolved(
                        draft,
                        UnresolvedRequirement(
                            field="date_window",
                            reason=f"用户提供的返程边界无效：{deadline.group(0)}",
                            critical=True,
                        ),
                    )
                    return
                draft.latest_return_date = return_deadline
                # A natural-language arrival deadline is an explicit search
                # boundary, not a single guessed return leg.  Preserve the
                # boundary and probe the last two legal return dates so the
                # formal planner can compare the user's "before Sep 10"
                # wording without silently dropping Sep 10 itself.  The
                # actual arrival verifier remains authoritative for the
                # inclusive home-arrival deadline.
                if (
                    relative_departure_date is None
                    or return_deadline > relative_departure_date
                ):
                    draft.return_date_targets = tuple(
                        dict.fromkeys(
                            (
                                return_deadline - timedelta(days=1),
                                return_deadline,
                            )
                        )
                    )
            return
        parsed_dates: list[tuple[date, str]] = []
        invalid_dates: list[str] = []
        for match in _FULL_DATE_PATTERN.finditer(text):
            prefix = text[max(0, match.start() - 18) : match.start()]
            if re.search(r"(?:当前日期|今天|今日|现在|基准日期)\s*(?:是|为|：|:)\s*$", prefix):
                continue
            try:
                value = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                invalid_dates.append(match.group(0))
                continue
            parsed_dates.append((value, match.group(0)))
        if invalid_dates:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="date_window",
                    reason=(
                        "用户提供了无效日期 "
                        + "、".join(dict.fromkeys(invalid_dates))
                        + "；禁止把非法日号降级成月份搜索"
                    ),
                    critical=True,
                ),
            )
            return
        if parsed_dates:
            contextual_departures: list[tuple[date, str]] = []
            for match in _FULL_DATE_PATTERN.finditer(text):
                date_end = match.start() + len(match.group(0).rstrip())
                suffix = text[date_end : date_end + 32]
                contextual = re.match(
                    r"[ \t]*(?:从(?P<origin>[\u4e00-\u9fffA-Za-z]{2,20})[ \t]*)?出发",
                    suffix,
                )
                if contextual is None or re.match(
                    r"[ \t]*(?:从[\u4e00-\u9fffA-Za-z]{2,20}[ \t]*出发[ \t]*)?"
                    r"(?:返程|回程|返回|回来)",
                    suffix,
                ):
                    continue
                labelled_origin = contextual.group("origin")
                if labelled_origin is not None:
                    origin_code = _LOCATION_IATA_BY_ALIAS.get(
                        re.sub(r"\s+", "", labelled_origin).casefold()
                    )
                    if origin_code != draft.values.get("origin_code"):
                        continue
                contextual_departures.append(
                    (
                        date(
                            int(match.group("year")),
                            int(match.group("month")),
                            int(match.group("day")),
                        ),
                        match.group(0),
                    )
                )
            departure_dates = self._dates_from_labels(
                text,
                ("去程", "出发日期", "出发时间"),
                reference_date,
            )
            departure_dates = tuple(contextual_departures) + departure_dates
            return_dates = self._dates_from_labels(
                text,
                ("返程", "回程", "返回日期", "返程日期"),
                reference_date,
            )
            contextual_returns: list[tuple[date, str]] = []
            for match in _FULL_DATE_PATTERN.finditer(text):
                date_end = match.start() + len(match.group(0).rstrip())
                suffix = text[date_end : date_end + 32]
                if not re.match(
                    r"[ \t]*(?:从[\u4e00-\u9fffA-Za-z]{2,20}[ \t]*出发[ \t]*)?"
                    r"(?:返程|回程|返回|回来)",
                    suffix,
                ):
                    continue
                prefix_tail = text[max(0, match.start() - 24) : match.start()]
                if re.search(r"(?:去程|出发日期|出发时间)", prefix_tail):
                    continue
                contextual_returns.append(
                    (
                        date(
                            int(match.group("year")),
                            int(match.group("month")),
                            int(match.group("day")),
                        ),
                        match.group(0),
                    )
                )
            return_dates = tuple(contextual_returns) + return_dates
            for suffix_match in re.finditer(
                r"(?P<value>(?:\d{1,2}\s*月\s*\d{1,2}\s*日"
                r"(?:\s*(?:与|和|或|/|、)\s*\d{1,2}\s*月\s*\d{1,2}\s*日)*)\s*返程)",
                text,
            ):
                return_dates += self._dates_from_labels(
                    f"返程：{suffix_match.group('value')}",
                    ("返程",),
                    reference_date,
                )
            departure_dates += self._dates_from_labels(
                text,
                ("出发窗口", "开始日期"),
                reference_date,
            )
            if departure_dates or return_dates:
                departure: date | None = None
                if departure_dates:
                    departure, departure_text = departure_dates[0]
                    self._set_fact(
                        draft,
                        "earliest_departure",
                        departure,
                        RequirementFactSource.EXPLICIT_TEXT,
                        departure_text,
                    )
                    self._set_fact(
                        draft,
                        "latest_departure",
                        departure,
                        RequirementFactSource.EXPLICIT_TEXT,
                        departure_text,
                    )
                    unique_departures = tuple(dict.fromkeys(item[0] for item in departure_dates))
                    if len(unique_departures) > 1:
                        draft.conflicts.append(
                            RequirementConflict(
                                field="departure_date",
                                deterministic_value=unique_departures[0].isoformat(),
                                model_value=unique_departures[1].isoformat(),
                                reason="用户输入了多个不同的明确去程日期",
                            )
                        )
                return_date: date | None = None
                if return_dates:
                    return_date, return_text = return_dates[0]
                    unique_returns = tuple(dict.fromkeys(item[0] for item in return_dates))
                    if len(unique_returns) == 1:
                        self._set_fact(
                            draft,
                            "exact_return_date",
                            return_date,
                            RequirementFactSource.EXPLICIT_TEXT,
                            return_text,
                        )
                    else:
                        draft.return_date_targets = unique_returns
                        draft.latest_return_date = max(unique_returns)
                        draft.notes.append("多个明确返程日期作为候选目标共同纳入日期遍历")
                        minimum = draft.values.get("min_nights")
                        if departure is not None and isinstance(minimum, int):
                            derived_latest = max(unique_returns) - timedelta(days=minimum)
                            if derived_latest >= departure:
                                self._set_fact(
                                    draft,
                                    "latest_departure",
                                    derived_latest,
                                    RequirementFactSource.DETERMINISTIC_DERIVATION,
                                    "按最晚返程边界和最短行程时长推导最晚可出发日",
                                    explicit=False,
                                    overwrite=True,
                                )
                if departure is not None and return_date is not None and return_date <= departure:
                    draft.conflicts.append(
                        RequirementConflict(
                            field="date_range",
                            deterministic_value=departure.isoformat(),
                            model_value=return_date.isoformat(),
                            reason="用户文本中的返程日期不晚于出发日期",
                        )
                    )
                return
            departure, departure_text = parsed_dates[0]
            self._set_fact(
                draft,
                "earliest_departure",
                departure,
                RequirementFactSource.EXPLICIT_TEXT,
                departure_text,
            )
            self._set_fact(
                draft,
                "latest_departure",
                departure,
                RequirementFactSource.EXPLICIT_TEXT,
                departure_text,
            )
            if len(parsed_dates) >= 2:
                return_date, return_text = parsed_dates[1]
                self._set_fact(
                    draft,
                    "exact_return_date",
                    return_date,
                    RequirementFactSource.EXPLICIT_TEXT,
                    return_text,
                )
                if return_date <= departure:
                    draft.conflicts.append(
                        RequirementConflict(
                            field="date_range",
                            deterministic_value=departure.isoformat(),
                            model_value=return_date.isoformat(),
                            reason="用户文本中的返程日期不晚于出发日期",
                        )
                    )
            return
        month_match = _MONTH_PATTERN.search(text)
        if month_match is not None:
            year = int(month_match.group("year"))
            month = int(month_match.group("month"))
            evidence_text = month_match.group(0)
        else:
            bare_month = _BARE_MONTH_PATTERN.search(text)
            if bare_month is None:
                return
            month = int(bare_month.group("month"))
            year = reference_date.year if month >= reference_date.month else reference_date.year + 1
            evidence_text = (
                f"{bare_month.group(0)}（相对 {reference_date.isoformat()} 确定性解析为 {year} 年）"
            )
        if not 1 <= month <= 12:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="date_window",
                    reason=f"用户提供的月份 {month} 不在 1 至 12 月范围内",
                    critical=True,
                ),
            )
            return
        last_day = calendar.monthrange(year, month)[1]
        earliest = date(year, month, 1)
        self._set_fact(
            draft,
            "earliest_departure",
            earliest,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            evidence_text,
        )
        self._set_fact(
            draft,
            "latest_departure",
            date(year, month, last_day),
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            evidence_text,
        )
        draft.notes.append("月份需求已确定性展开为该月首日至末日的可选出发窗口")

    def _explicit_latest_arrival_boundaries(
        self,
        text: str,
        reference_date: date,
    ) -> tuple[tuple[date, str], ...]:
        """Return only dates explicitly bound to home-arrival or completion.

        A normal ``X 日返程`` is the date the return leg leaves the
        destination.  It must not silently become a home-arrival deadline.
        """

        dated_spans: list[tuple[int, int, date]] = []
        full_spans: list[tuple[int, int]] = []
        for match in _FULL_DATE_PATTERN.finditer(text):
            try:
                value = date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                )
            except ValueError:
                continue
            dated_spans.append((match.start(), match.end(), value))
            full_spans.append((match.start(), match.end()))
        for match in _MONTH_DAY_PATTERN.finditer(text):
            if any(
                match.start() < full_end and match.end() > full_start
                for full_start, full_end in full_spans
            ):
                continue
            month = int(match.group("month"))
            day = int(match.group("day"))
            year = (
                reference_date.year
                if month >= reference_date.month
                else reference_date.year + 1
            )
            try:
                value = date(year, month, day)
            except ValueError:
                continue
            dated_spans.append((match.start(), match.end(), value))

        boundaries: list[tuple[date, str]] = []
        for start, end, value in sorted(dated_spans):
            prefix = text[max(0, start - 32) : start]
            suffix = text[end : min(len(text), end + 32)]
            semantic_before = _ARRIVAL_OR_COMPLETION_BEFORE_DATE.search(prefix)
            deadline_before = _ARRIVAL_DEADLINE_BEFORE_DATE.search(prefix)
            semantic_after = _ARRIVAL_OR_COMPLETION_AFTER_DATE.match(suffix)
            if semantic_before is None and not (
                deadline_before is not None and semantic_after is not None
            ):
                continue
            if semantic_before is not None:
                evidence_start = max(0, start - 24)
            else:
                assert deadline_before is not None
                evidence_start = max(0, start - len(deadline_before.group(0)))
            evidence_end = (
                min(len(text), end + len(semantic_after.group(0)))
                if semantic_after is not None
                else end
            )
            boundaries.append(
                (value, text[evidence_start:evidence_end].strip(" \\t,，。；;"))
            )
        return tuple(boundaries)

    def _validate_departure_recency(self, draft: _Draft, reference_date: date) -> None:
        earliest = draft.values.get("earliest_departure")
        latest = draft.values.get("latest_departure")
        if not isinstance(earliest, date) or not isinstance(latest, date):
            return
        if latest < reference_date:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="date_window",
                    reason=(
                        f"全部可选去程日期均早于基准日期 {reference_date.isoformat()}，"
                        "需要用户提供未来日期"
                    ),
                    critical=True,
                ),
            )
            return
        if earliest < reference_date:
            self._set_fact(
                draft,
                "earliest_departure",
                reference_date,
                RequirementFactSource.DETERMINISTIC_DERIVATION,
                f"按基准日期 {reference_date.isoformat()} 排除已过去的出发日",
                explicit=False,
                overwrite=True,
            )
            draft.notes.append("当前月份中已过去的日期已从搜索窗口排除")

    def _extract_duration(self, text: str, draft: _Draft) -> None:
        ranged_matches = tuple(_DURATION_PATTERN.finditer(text))
        if ranged_matches:
            night_ranges = tuple(
                item for item in ranged_matches if item.group("unit") in {"晚", "夜"}
            )
            selected = night_ranges[0] if night_ranges else ranged_matches[0]
            selected_nights = self._night_range(
                int(selected.group("minimum")),
                int(selected.group("maximum")),
                selected.group("unit"),
            )
            if selected_nights is None:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field="duration",
                        reason="包含 1 天的行程可能是零晚日游，无法安全转换为住宿夜数",
                        critical=True,
                    ),
                )
                return
            minimum, maximum = selected_nights
            self._set_duration_facts(
                draft,
                minimum,
                maximum,
                selected.group(0),
                selected.group("unit"),
            )
            for other in ranged_matches:
                other_nights = self._night_range(
                    int(other.group("minimum")),
                    int(other.group("maximum")),
                    other.group("unit"),
                )
                if other_nights is not None and other_nights != selected_nights:
                    draft.conflicts.append(
                        RequirementConflict(
                            field="duration",
                            deterministic_value={
                                "min_nights": minimum,
                                "max_nights": maximum,
                            },
                            model_value={
                                "min_nights": other_nights[0],
                                "max_nights": other_nights[1],
                            },
                            reason="用户文本中的天数与夜数范围互相矛盾",
                        )
                    )
                    break
            return
        singles = tuple(_SINGLE_DURATION_PATTERN.finditer(text))
        if not singles:
            return
        night_matches = tuple(item for item in singles if item.group("unit") in {"晚", "夜"})
        selected = night_matches[0] if night_matches else singles[0]
        selected_range = self._night_range(
            int(selected.group("count")),
            int(selected.group("count")),
            selected.group("unit"),
        )
        if selected_range is None:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="duration",
                    reason="1 天行程可能是零晚日游，无法安全转换为住宿夜数",
                    critical=True,
                ),
            )
            return
        self._set_duration_facts(
            draft,
            selected_range[0],
            selected_range[1],
            selected.group(0),
            selected.group("unit"),
        )
        for other in singles:
            other_range = self._night_range(
                int(other.group("count")),
                int(other.group("count")),
                other.group("unit"),
            )
            if other_range is not None and other_range != selected_range:
                draft.conflicts.append(
                    RequirementConflict(
                        field="duration",
                        deterministic_value=selected_range[0],
                        model_value=other_range[0],
                        reason="用户文本中的天数与夜数互相矛盾",
                    )
                )
                break

    def _night_range(
        self,
        minimum: int,
        maximum: int,
        unit: str,
    ) -> tuple[int, int] | None:
        if minimum > maximum:
            minimum, maximum = maximum, minimum
        if unit == "天":
            minimum -= 1
            maximum -= 1
        if minimum < 1:
            return None
        return minimum, maximum

    def _set_duration_facts(
        self,
        draft: _Draft,
        minimum: int,
        maximum: int,
        evidence_text: str,
        unit: str,
    ) -> None:
        self._set_fact(
            draft,
            "min_nights",
            minimum,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            evidence_text,
        )
        self._set_fact(
            draft,
            "max_nights",
            maximum,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            evidence_text,
        )
        if unit == "天":
            draft.notes.append("自由行“天数”按自然日口径转换为“天数减一”的住宿夜数")

    def _extract_party(self, text: str, draft: _Draft) -> None:
        adults = re.search(r"(\d{1,2})\s*(?:名|位|个)?\s*成人", text)
        if adults is None:
            adults = re.search(
                rf"({'|'.join(_CHINESE_SMALL_NUMBERS)})\s*(?:名|位|个)?\s*成人",
                text,
            )
        if adults is None:
            adults = re.search(r"(?:人数|出行人数)\s*[:：]?\s*(\d{1,2})\s*人", text)
        if adults is None and re.search(
            r"(?:我和(?:女朋友|女友)|本人和(?:女朋友|女友)|两个人|两位伴侣)",
            text,
        ):
            self._set_fact(
                draft,
                "adults",
                2,
                RequirementFactSource.EXPLICIT_TEXT,
                "我和女朋友两个人",
            )
        if adults is not None:
            self._set_fact(
                draft,
                "adults",
                (
                    int(adults.group(1))
                    if adults.group(1).isdigit()
                    else _CHINESE_SMALL_NUMBERS[adults.group(1)]
                ),
                RequirementFactSource.EXPLICIT_TEXT,
                adults.group(0),
            )
        children = re.search(r"(\d{1,2})\s*(?:名|位|个)?\s*儿童", text)
        if children is not None:
            self._set_fact(
                draft, "children", int(children.group(1)),
                RequirementFactSource.EXPLICIT_TEXT, children.group(0)
            )
        ages = re.findall(r"(\d{1,2})\s*岁", text)
        if ages:
            self._set_fact(
                draft, "children_ages", [int(age) for age in ages],
                RequirementFactSource.EXPLICIT_TEXT, "、".join(ages) + "岁",
            )
        infants = re.search(r"(\d{1,2})\s*(?:名|位|个)?\s*婴儿", text)
        if infants is not None:
            self._set_fact(
                draft, "infants", int(infants.group(1)),
                RequirementFactSource.EXPLICIT_TEXT, infants.group(0)
            )
        rooms = re.search(r"(\d{1,2})\s*间\s*(?:房|客房|房间)", text)
        if rooms is None:
            rooms = re.search(r"(?:酒店|房间数)\s*[:：]?\s*(\d{1,2})\s*间", text)
        if rooms is None and re.search(r"(?:本人和女友|本人和女朋友|女友|女朋友|情侣|夫妻)", text):
            self._set_fact(
                draft,
                "rooms",
                1,
                RequirementFactSource.DETERMINISTIC_DERIVATION,
                "两位伴侣按一间房比较；如需分房应在确认前调整",
                explicit=False,
            )
        if rooms is not None:
            self._set_fact(
                draft,
                "rooms",
                int(rooms.group(1)),
                RequirementFactSource.EXPLICIT_TEXT,
                rooms.group(0),
            )
        if (
            rooms is None
            and "rooms" not in draft.values
            and draft.values.get("adults") == 2
            and draft.values.get("children", 0) == 0
            and draft.values.get("infants", 0) == 0
        ):
            self._set_fact(
                draft,
                "rooms",
                1,
                RequirementFactSource.SYSTEM_DEFAULT,
                "用户未提供房间数；2位成人默认按1间房比较，可在确认前调整",
                explicit=False,
            )
            draft.notes.append("2位成人未提供房间数，已采用可见的系统默认值1间房")

    def _apply_return_boundary(self, draft: _Draft) -> None:
        latest_return = draft.latest_return_date
        earliest = draft.values.get("earliest_departure")
        minimum = draft.values.get("min_nights")
        if not isinstance(latest_return, date) or not isinstance(earliest, date):
            return
        if not isinstance(minimum, int):
            return
        latest_departure = latest_return - timedelta(days=minimum)
        if latest_departure < earliest:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="date_window",
                    reason="最晚返程边界早于最短行程可覆盖的出发日",
                    critical=True,
                ),
            )
            return
        self._set_fact(
            draft,
            "latest_departure",
            latest_departure,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            "按最晚返程边界和最短行程时长推导最晚可出发日",
            explicit=False,
            overwrite=True,
        )

    def _extract_budget_and_currency(self, text: str, draft: _Draft) -> None:
        scoped_budget = _SCOPED_BUDGET_PATTERN.search(text)
        if scoped_budget is not None:
            cents, currency = self._parse_budget_match(scoped_budget)
            if currency is not None:
                self._set_fact(
                    draft,
                    "currency",
                    currency,
                    RequirementFactSource.EXPLICIT_TEXT,
                    scoped_budget.group(0),
                )
            scope = scoped_budget.group("scope")
            if scope in {"人均", "每人", "单人"}:
                adults = draft.values.get("adults")
                if cents is not None and isinstance(adults, int):
                    self._set_fact(
                        draft,
                        "budget_cents",
                        cents * adults,
                        RequirementFactSource.DETERMINISTIC_DERIVATION,
                        (f"{scoped_budget.group(0)} × {adults} 名成人，归一化为全体成人整包总预算"),
                    )
                    draft.notes.append("人均预算已按明确成人数换算为全体成人整包总预算")
                else:
                    self._add_unresolved(
                        draft,
                        UnresolvedRequirement(
                            field="budget_scope",
                            reason="人均预算缺少可用成人数，未擅自转换为整包总预算",
                            critical=False,
                        ),
                    )
                return
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="budget_scope",
                    reason=(f"{scope}预算是分项预算，未冒充机票与酒店合计的整包总预算"),
                    critical=False,
                ),
            )
            draft.notes.append("分项预算未写入整包总预算字段")
            return
        budget = _BUDGET_PATTERN.search(text)
        if budget is not None:
            cents, currency = self._parse_budget_match(budget)
            if cents is not None:
                self._set_fact(
                    draft,
                    "budget_cents",
                    cents,
                    RequirementFactSource.EXPLICIT_TEXT,
                    budget.group(0),
                )
            if currency is not None:
                self._set_fact(
                    draft,
                    "currency",
                    currency,
                    RequirementFactSource.EXPLICIT_TEXT,
                    budget.group(0),
                )
            return
        if re.search(r"(?:美元|USD|\$)", text, flags=re.IGNORECASE):
            self._set_fact(
                draft,
                "currency",
                "USD",
                RequirementFactSource.EXPLICIT_TEXT,
                "用户文本显式包含美元币种",
            )
        elif re.search(r"(?:人民币|CNY|RMB|￥|¥)", text, flags=re.IGNORECASE):
            self._set_fact(
                draft,
                "currency",
                "CNY",
                RequirementFactSource.EXPLICIT_TEXT,
                "用户文本显式包含人民币币种",
            )

    def _parse_budget_match(self, match: re.Match[str]) -> tuple[int | None, str | None]:
        try:
            amount = Decimal(match.group("amount"))
        except InvalidOperation:
            return None, None
        scale = {"万": Decimal(10_000), "千": Decimal(1_000)}.get(
            match.group("scale"),
            Decimal(1),
        )
        cents_decimal = amount * scale * Decimal(100)
        cents = (
            int(cents_decimal)
            if cents_decimal >= 0 and cents_decimal == cents_decimal.to_integral_value()
            else None
        )
        currency_text = (match.group("currency") or match.group("symbol") or "").upper()
        currency = (
            "USD" if currency_text in {"$", "美元", "USD"} else ("CNY" if currency_text else None)
        )
        return cents, currency

    def _extract_baggage(
        self,
        text: str,
        draft: _Draft,
        captured_at: datetime,
    ) -> None:
        indifferent = re.search(r"(?:行李|托运行李)\s*(?:无要求|不限|不作要求)", text)
        not_required = re.search(
            r"(?:无|不带|不需要|不要)\s*(?:托运)?行李|(?:不含|无需)\s*托运行李",
            text,
        )
        forbidden = re.search(r"(?:禁止|明确不要)\s*(?:包含|含)?\s*托运行李", text)
        required = re.search(
            r"(?:必须|需要|要)\s*(?:包含|含|有)?\s*托运行李|托运行李\s*(?:必须|需要)",
            text,
        )
        if (
            forbidden is not None
            and not_required is not None
            and forbidden.start() <= not_required.start()
            and forbidden.end() >= not_required.end()
        ):
            not_required = None
        baggage_modes = {
            PreferenceMode.INDIFFERENT: indifferent or not_required,
            PreferenceMode.FORBIDDEN: forbidden,
            PreferenceMode.REQUIRED: required,
        }
        active_modes = tuple(mode for mode, match in baggage_modes.items() if match is not None)
        if len(active_modes) > 1:
            draft.conflicts.append(
                RequirementConflict(
                    field="preference:checked_baggage",
                    deterministic_value=[mode.value for mode in active_modes],
                    model_value=[
                        match.group(0) for match in baggage_modes.values() if match is not None
                    ],
                    reason="用户文本同时声明了互斥的行李偏好状态，需要用户裁决",
                )
            )
            return
        if indifferent is not None:
            value: bool | None = None
            mode = PreferenceMode.INDIFFERENT
            evidence = indifferent.group(0)
        elif forbidden is not None:
            value = False
            mode = PreferenceMode.FORBIDDEN
            evidence = forbidden.group(0)
        elif not_required is not None:
            value = False
            mode = PreferenceMode.INDIFFERENT
            evidence = not_required.group(0)
        elif required is not None:
            value = True
            mode = PreferenceMode.REQUIRED
            evidence = required.group(0)
        else:
            return
        self._set_fact(
            draft,
            "require_checked_baggage",
            value,
            RequirementFactSource.EXPLICIT_TEXT,
            evidence,
        )
        draft.preferences["checked_baggage"] = self._preference_rule(
            key="checked_baggage",
            mode=mode,
            weight=self._default_weight(mode),
            expected=False if value is False else (None if value is None else True),
            reason=evidence,
            captured_at=captured_at,
        )

    def _extract_breakfast(
        self,
        text: str,
        draft: _Draft,
        captured_at: datetime,
    ) -> None:
        indifferent = re.search(r"早餐\s*(?:无要求|不限|不作要求)", text)
        forbidden = re.search(r"(?:不要|不需要|无需)\s*早餐|早餐\s*(?:不要|不需要)", text)
        required = re.search(
            r"(?:必须|需要|要)\s*(?:包含|含|有)?\s*早餐|早餐\s*(?:必须|需要)",
            text,
        )
        weighted = re.search(r"早餐\s*(?:重要|优先|比较重要)", text)
        weight_match = re.search(
            r"早餐.{0,8}?(?:权重|重要性)\s*[:：]?\s*(\d{1,3})(?:%|％)",
            text,
        )
        signals = {
            PreferenceMode.INDIFFERENT: indifferent,
            PreferenceMode.FORBIDDEN: forbidden,
            PreferenceMode.REQUIRED: required,
            PreferenceMode.WEIGHTED: weighted or weight_match,
        }
        active_modes = tuple(mode for mode, match in signals.items() if match is not None)
        if len(active_modes) > 1:
            draft.conflicts.append(
                RequirementConflict(
                    field="preference:hotel_breakfast",
                    deterministic_value=[mode.value for mode in active_modes],
                    model_value=[match.group(0) for match in signals.values() if match is not None],
                    reason="用户文本同时声明了互斥的早餐偏好状态，需要用户裁决",
                )
            )
            return
        if weight_match is not None and int(weight_match.group(1)) > 100:
            draft.conflicts.append(
                RequirementConflict(
                    field="preference:hotel_breakfast",
                    deterministic_value=weight_match.group(0),
                    model_value=int(weight_match.group(1)),
                    reason="早餐权重必须在 0% 至 100% 之间，禁止静默截断",
                )
            )
            return
        if indifferent is not None:
            value: bool | None = None
            mode = PreferenceMode.INDIFFERENT
            evidence = indifferent.group(0)
        elif forbidden is not None:
            value = False
            mode = PreferenceMode.FORBIDDEN
            evidence = forbidden.group(0)
        elif required is not None:
            value = True
            mode = PreferenceMode.REQUIRED
            evidence = required.group(0)
        elif weighted is not None or weight_match is not None:
            value = None
            mode = PreferenceMode.WEIGHTED
            evidence = (weighted or weight_match).group(0)  # type: ignore[union-attr]
        else:
            return
        weight = (
            int(weight_match.group(1)) / 100
            if weight_match is not None
            else self._default_weight(mode)
        )
        self._set_fact(
            draft,
            "require_breakfast",
            value,
            RequirementFactSource.EXPLICIT_TEXT,
            evidence,
        )
        draft.preferences["hotel_breakfast"] = self._preference_rule(
            key="hotel_breakfast",
            mode=mode,
            weight=weight,
            expected=None if mode == PreferenceMode.INDIFFERENT else True,
            reason=evidence,
            captured_at=captured_at,
        )

    def _extract_other_preferences(
        self,
        text: str,
        draft: _Draft,
        captured_at: datetime,
    ) -> None:
        if match := re.search(r"(?:星级|酒店星级)\s*(?:无要求|不限|不作要求)", text):
            draft.preferences["hotel_star_rating"] = self._preference_rule(
                key="hotel_star_rating",
                mode=PreferenceMode.INDIFFERENT,
                weight=0,
                expected=None,
                reason=match.group(0),
                captured_at=captured_at,
            )
        if match := re.search(r"(?:不接受|不要|拒绝)\s*(?:中转|转机)|必须\s*直飞", text):
            draft.preferences["flight_connections"] = self._preference_rule(
                key="flight_connections",
                mode=PreferenceMode.FORBIDDEN,
                weight=1,
                expected=False,
                reason=match.group(0),
                captured_at=captured_at,
            )
        elif match := re.search(r"(?:接受|可以|可)\s*(?:中转|转机)", text):
            draft.preferences["flight_connections"] = self._preference_rule(
                key="flight_connections",
                mode=PreferenceMode.INDIFFERENT,
                weight=0,
                expected=None,
                reason=match.group(0),
                captured_at=captured_at,
            )
        if match := re.search(r"(?:几个|多个|多种)\s*方案.*?(?:对比|比较)|方案\s*对比", text):
            draft.preferences["compare_budget_options"] = self._preference_rule(
                key="compare_budget_options",
                mode=PreferenceMode.REQUIRED,
                weight=1,
                expected=True,
                reason=match.group(0),
                captured_at=captured_at,
            )
        if match := re.search(
            r"(?:酒店|住宿)(?:不能(?:太)?|不可(?:太)?)简陋|可稍有品质|(?:酒店|住宿)品质",
            text,
        ):
            draft.preferences["lodging_quality"] = self._preference_rule(
                key="lodging_quality",
                mode=PreferenceMode.REQUIRED,
                weight=1,
                expected="not_basic",
                reason=match.group(0),
                captured_at=captured_at,
            )
        if match := re.search(
            r"(?:酒店|住宿|地址|位置)[^，。；;\n]{0,12}"
            r"(?:不能(?:太)?|不可(?:太)?)[^，。；;\n]{0,8}(?:偏僻|偏)|"
            r"交通便利|位置方便",
            text,
        ):
            draft.preferences["lodging_location"] = self._preference_rule(
                key="lodging_location",
                mode=PreferenceMode.REQUIRED,
                weight=1,
                expected="convenient_not_remote",
                reason=match.group(0),
                captured_at=captured_at,
            )
        if match := re.search(r"价格不能过高|价格不宜过高|价格合理|价格适中|不能太贵", text):
            draft.preferences["lodging_price"] = self._preference_rule(
                key="lodging_price",
                mode=PreferenceMode.WEIGHTED,
                weight=0.75,
                expected="reasonable_not_high",
                reason=(
                    f"{match.group(0)}；用户未提供数字预算，按相对偏好比较，不推导硬性价格上限"
                ),
                captured_at=captured_at,
            )
        if match := re.search(r"(?:机场附近(?:可以|可|能)?住|住机场附近|机场附近住宿)", text):
            draft.preferences["airport_lodging_fallback"] = self._preference_rule(
                key="airport_lodging_fallback",
                mode=PreferenceMode.INDIFFERENT,
                weight=0,
                expected=None,
                reason=f"{match.group(0)}；机场住宿仅作为可选过渡，不替代岛屿方案比较",
                captured_at=captured_at,
            )
        if match := re.search(r"关注有没有更好的选择|更好的选择|岛屿方案", text):
            draft.preferences["lodging_zone_comparison"] = self._preference_rule(
                key="lodging_zone_comparison",
                mode=PreferenceMode.REQUIRED,
                weight=1,
                expected=True,
                reason=f"{match.group(0)}；必须比较机场过渡与交通便利岛屿住宿",
                captured_at=captured_at,
            )

    def _apply_breakfast_override(
        self,
        request: PackageRequirementRequest,
        draft: _Draft,
        captured_at: datetime,
    ) -> None:
        if request.breakfast_mode is None and request.breakfast_weight is None:
            return
        mode = request.breakfast_mode or PreferenceMode.WEIGHTED
        weight = (
            request.breakfast_weight
            if request.breakfast_weight is not None
            else self._default_weight(mode)
        )
        hard_value = {
            PreferenceMode.REQUIRED: True,
            PreferenceMode.FORBIDDEN: False,
        }.get(mode)
        self._set_fact(
            draft,
            "require_breakfast",
            hard_value,
            RequirementFactSource.STRUCTURED_USER_OVERRIDE,
            "用户通过结构化控件覆盖早餐偏好",
            overwrite=True,
        )
        draft.preferences["hotel_breakfast"] = self._preference_rule(
            key="hotel_breakfast",
            mode=mode,
            weight=weight,
            expected=None if mode == PreferenceMode.INDIFFERENT else True,
            reason="用户通过结构化控件设置早餐模式或权重",
            captured_at=captured_at,
        )

    def _mark_breakfast_weight_not_applied(self, draft: _Draft) -> None:
        self._add_unresolved(
            draft,
            UnresolvedRequirement(
                field="preference_application:hotel_breakfast",
                reason=(
                    "需求仍处于人工阻塞状态，尚未启动实时报价与 Planner，"
                    "因此早餐权重未应用到候选排序"
                ),
                critical=False,
            ),
        )
        draft.notes.append("需求阻塞期间未执行早餐偏好软评分")

    def _validate_exact_date_duration(self, draft: _Draft) -> None:
        departure = draft.values.get("earliest_departure")
        return_date = draft.values.get("exact_return_date")
        if not isinstance(departure, date) or not isinstance(return_date, date):
            return
        nights = (return_date - departure).days
        if nights <= 0:
            if not any(item.field == "date_range" for item in draft.conflicts):
                draft.conflicts.append(
                    RequirementConflict(
                        field="date_range",
                        deterministic_value=departure.isoformat(),
                        model_value=return_date.isoformat(),
                        reason="用户文本中的返程日期不晚于出发日期",
                    )
                )
            return
        minimum = draft.values.get("min_nights")
        maximum = draft.values.get("max_nights")
        if (
            isinstance(minimum, int)
            and isinstance(maximum, int)
            and not minimum <= nights <= maximum
        ):
            draft.conflicts.append(
                RequirementConflict(
                    field="duration",
                    deterministic_value={"min_nights": minimum, "max_nights": maximum},
                    model_value=nights,
                    reason="明确出返日期对应夜数不在用户声明的时长范围内",
                )
            )
            return
        self._set_fact(
            draft,
            "min_nights",
            nights,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            "由明确出发和返程日期计算",
            overwrite=True,
        )
        self._set_fact(
            draft,
            "max_nights",
            nights,
            RequirementFactSource.DETERMINISTIC_DERIVATION,
            "由明确出发和返程日期计算",
            overwrite=True,
        )

    async def _propose_with_model(
        self,
        request: PackageRequirementRequest,
        draft: _Draft,
    ) -> tuple[
        ModelPackageRequirementProposal | None,
        ModelResponse | None,
        str | None,
    ]:
        schema = _JSON_OBJECT.validate_python(ModelPackageRequirementProposal.model_json_schema())
        locked = {
            field_name: self._json_value(value)
            for field_name, value in draft.values.items()
            if draft.facts[field_name].explicit
        }
        model_request = ModelRequest(
            role=AgentRole.CONTEXT,
            system=(
                "你是自由行需求提案 Agent。只输出符合 schema 的 JSON。"
                "locked_facts 是用户文本经确定性抽取的锁定事实，不得改写；"
                "对缺失或歧义字段只能提出 proposal 并列入 unresolved，不能猜成已确认事实。"
                "偏好可用 required、weighted、forbidden、indifferent 四态；"
                "不得生成报价、库存或可订承诺。"
            ),
            messages=(
                ModelMessage(
                    role="user",
                    content=compact_json(
                        {
                            "request_text": request.text,
                            "reference_date": request.reference_date.isoformat(),
                            "locked_facts": locked,
                            "deterministic_preferences": [
                                rule.model_dump(mode="json") for rule in draft.preferences.values()
                            ],
                        }
                    ),
                ),
            ),
            response_schema=schema,
            temperature=0,
            risk_level=1,
        )
        try:
            if self._model_router is not None:
                routed = await self._model_router.complete(model_request)
                response = routed.response
            else:
                assert self._model_client is not None
                response = await self._model_client.complete(model_request)
            raw: Any = json.loads(response.text)
            proposal = ModelPackageRequirementProposal.model_validate(raw)
        except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError) as exc:
            return None, None, f"模型结构化提案无效，已忽略：{type(exc).__name__}"
        return proposal, response, None

    def _reconcile_model(
        self,
        proposal: ModelPackageRequirementProposal,
        draft: _Draft,
        captured_at: datetime,
    ) -> None:
        for field_name in _MODEL_FACT_FIELDS:
            proposed = getattr(proposal, field_name)
            if proposed is None:
                continue
            fact = draft.facts.get(field_name)
            if fact is not None and fact.explicit:
                current = draft.values[field_name]
                if self._json_value(current) != self._json_value(proposed):
                    self._add_unresolved(
                        draft,
                        UnresolvedRequirement(
                            field=f"ignored_model_conflict:{field_name}",
                            reason="模型提案与用户明确值冲突；已忽略模型值并保留锁定用户值",
                            critical=False,
                            model_proposal=self._json_value(proposed),
                        ),
                    )
                continue
            if field_name in _CRITICAL_FIELDS and field_name not in draft.values:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field=field_name,
                        reason="关键字段只有模型提案，必须由用户确认后才能搜索",
                        critical=True,
                        model_proposal=self._json_value(proposed),
                    ),
                )
            elif field_name not in draft.values:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field=field_name,
                        reason="模型提出了非关键值，但未提升为用户硬约束",
                        critical=False,
                        model_proposal=self._json_value(proposed),
                    ),
                )
        for model_rule in proposal.preferences:
            explicit_rule = draft.preferences.get(model_rule.key)
            if explicit_rule is not None:
                if (
                    explicit_rule.mode != model_rule.mode
                    or explicit_rule.expected != model_rule.expected
                    or abs(explicit_rule.weight - model_rule.weight) > 1e-9
                ):
                    self._add_unresolved(
                        draft,
                        UnresolvedRequirement(
                            field=f"ignored_model_conflict:preference:{model_rule.key}",
                            reason=("模型偏好提案与用户明确偏好冲突；已忽略模型偏好并保留用户规则"),
                            critical=False,
                            model_proposal=_JSON_VALUE.validate_python(
                                model_rule.model_dump(mode="json")
                            ),
                        ),
                    )
                continue
            if model_rule.mode in {PreferenceMode.REQUIRED, PreferenceMode.FORBIDDEN}:
                self._add_unresolved(
                    draft,
                    UnresolvedRequirement(
                        field=f"preference:{model_rule.key}",
                        reason="模型不能独自把推断偏好升级为必须或禁止，等待用户确认",
                        critical=False,
                        model_proposal=_JSON_VALUE.validate_python(
                            model_rule.model_dump(mode="json")
                        ),
                    ),
                )
                continue
            draft.preferences[model_rule.key] = PreferenceRule(
                key=model_rule.key,
                mode=model_rule.mode,
                weight=model_rule.weight,
                expected=model_rule.expected,
                source=PreferenceSource.INFERRED_CURRENT_CONTEXT,
                reason=model_rule.reason,
                created_at=captured_at,
            )
            if model_rule.key == "hotel_breakfast" and model_rule.mode == PreferenceMode.WEIGHTED:
                self._mark_breakfast_weight_not_applied(draft)
        for unresolved_field in proposal.unresolved:
            if unresolved_field in draft.values:
                continue
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field=unresolved_field,
                    reason="模型标记该字段仍有歧义，等待用户确认",
                    critical=unresolved_field in _CRITICAL_FIELDS,
                ),
            )

    def _add_missing_critical_fields(
        self,
        draft: _Draft,
        proposal: ModelPackageRequirementProposal | None,
    ) -> None:
        for field_name in sorted(_CRITICAL_FIELDS):
            if field_name in draft.values:
                continue
            proposed = getattr(proposal, field_name, None) if proposal is not None else None
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field=field_name,
                    reason="用户文本未明确该关键字段，已阻塞而不是猜测",
                    critical=True,
                    model_proposal=(self._json_value(proposed) if proposed is not None else None),
                ),
            )

    def _build_window(self, draft: _Draft) -> FlexibleTravelWindow | None:
        if not _CRITICAL_FIELDS.issubset(draft.values):
            return None
        try:
            return FlexibleTravelWindow(
                origin=str(draft.values["origin"]),
                destination=str(draft.values["destination"]),
                origin_code=(
                    str(draft.values["origin_code"]) if "origin_code" in draft.values else None
                ),
                destination_code=(
                    str(draft.values["destination_code"])
                    if "destination_code" in draft.values
                    else None
                ),
                earliest_departure=self._date_value(draft.values["earliest_departure"]),
                latest_departure=self._date_value(draft.values["latest_departure"]),
                min_nights=self._int_value(draft.values["min_nights"]),
                max_nights=self._int_value(draft.values["max_nights"]),
                adults=self._int_value(draft.values["adults"]),
                children=self._int_value(draft.values.get("children", 0)),
                infants=self._int_value(draft.values.get("infants", 0)),
                rooms=self._int_value(draft.values["rooms"]),
                currency=str(draft.values["currency"]),
                latest_return_date=draft.latest_return_date,
                latest_arrival_date=draft.latest_arrival_date,
                return_date_targets=draft.return_date_targets,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="travel_window",
                    reason=f"确定性字段无法组成合法日期窗口：{exc}",
                    critical=True,
                ),
            )
            return None

    def _build_intent_template(
        self,
        request: PackageRequirementRequest,
        draft: _Draft,
    ) -> PackageIntentTemplate | None:
        required = {"origin", "destination", "adults", "rooms", "currency"}
        if not required.issubset(draft.values):
            return None
        trip_id = request.trip_id or (
            "request-" + hashlib.sha256(request.text.encode("utf-8")).hexdigest()[:16]
        )
        try:
            breakfast_rule = draft.preferences.get("hotel_breakfast")
            connection_rule = draft.preferences.get("flight_connections")
            return PackageIntentTemplate(
                trip_id=trip_id,
                origin=str(draft.values["origin"]),
                destination=str(draft.values["destination"]),
                adults=self._int_value(draft.values["adults"]),
                children=self._int_value(draft.values.get("children", 0)),
                children_ages=tuple(
                    int(age)
                    for age in cast(
                        list[int] | tuple[int, ...],
                        draft.values.get("children_ages", ()),
                    )
                ),
                infants=self._int_value(draft.values.get("infants", 0)),
                rooms=self._int_value(draft.values["rooms"]),
                currency=str(draft.values["currency"]),
                budget_cents=self._optional_int(draft.values.get("budget_cents")),
                require_checked_baggage=self._optional_bool(
                    draft.values.get("require_checked_baggage")
                ),
                allow_connections=(
                    False
                    if connection_rule is not None
                    and connection_rule.mode == PreferenceMode.FORBIDDEN
                    else None
                ),
                require_breakfast=self._optional_bool(draft.values.get("require_breakfast")),
                breakfast_preference_mode=(
                    breakfast_rule.mode if breakfast_rule is not None else None
                ),
                breakfast_preference_weight=(
                    breakfast_rule.weight if breakfast_rule is not None else None
                ),
                latest_arrival_date=draft.latest_arrival_date,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            self._add_unresolved(
                draft,
                UnresolvedRequirement(
                    field="intent_template",
                    reason=f"确定性字段无法组成合法 PackageIntent 模板：{exc}",
                    critical=True,
                ),
            )
            return None

    def _context_evidence(
        self,
        *,
        facts: tuple[ExtractedRequirementFact, ...],
        proposal: ModelPackageRequirementProposal | None,
        model_response: ModelResponse | None,
        unresolved: tuple[UnresolvedRequirement, ...],
        conflicts: tuple[RequirementConflict, ...],
        captured_at: datetime,
    ) -> tuple[EvidenceRecord, ...]:
        records: list[EvidenceRecord] = []
        for fact in facts:
            source = {
                RequirementFactSource.EXPLICIT_TEXT: "user:explicit_current_trip",
                RequirementFactSource.STRUCTURED_USER_OVERRIDE: (
                    "user:structured_current_trip_override"
                ),
                RequirementFactSource.DETERMINISTIC_DERIVATION: (
                    "tripchord:deterministic-normalizer"
                ),
                RequirementFactSource.SYSTEM_DEFAULT: "tripchord:declared-default",
            }[fact.source]
            records.append(
                self._evidence_record(
                    topic="package_requirement_fact",
                    subject=fact.field,
                    payload=_JSON_OBJECT.validate_python(fact.model_dump(mode="json")),
                    source=source,
                    confidence=0.5 if fact.source == RequirementFactSource.SYSTEM_DEFAULT else 1,
                    captured_at=captured_at,
                )
            )
        if proposal is not None and model_response is not None:
            records.append(
                self._evidence_record(
                    topic="package_requirement_model_proposal",
                    subject="optional_model_proposal",
                    payload={
                        "proposal": _JSON_VALUE.validate_python(proposal.model_dump(mode="json")),
                        "provider": model_response.provider,
                        "model": model_response.model,
                        "token_usage": model_response.usage.total_tokens,
                    },
                    source=f"{model_response.provider}:{model_response.model}",
                    confidence=0.65,
                    captured_at=captured_at,
                )
            )
        if unresolved or conflicts:
            blocking = bool(conflicts) or any(item.critical for item in unresolved)
            records.append(
                self._evidence_record(
                    topic="package_requirement_gate",
                    subject=("human_block_audit" if blocking else "non_blocking_requirement_audit"),
                    payload={
                        "unresolved": _JSON_VALUE.validate_python(
                            [item.model_dump(mode="json") for item in unresolved]
                        ),
                        "conflicts": _JSON_VALUE.validate_python(
                            [item.model_dump(mode="json") for item in conflicts]
                        ),
                    },
                    source="tripchord:requirement-gate",
                    confidence=1,
                    captured_at=captured_at,
                )
            )
        return tuple(records)

    def _evidence_record(
        self,
        *,
        topic: str,
        subject: str,
        payload: dict[str, JsonValue],
        source: str,
        confidence: float,
        captured_at: datetime,
    ) -> EvidenceRecord:
        digest = hashlib.sha256(
            compact_json(
                {
                    "topic": topic,
                    "subject": subject,
                    "payload": payload,
                    "source": source,
                }
            ).encode("utf-8")
        ).hexdigest()[:20]
        return EvidenceRecord(
            id=f"requirement:{digest}",
            topic=topic,
            subject=subject,
            payload=payload,
            source=source,
            captured_at=captured_at,
            confidence=confidence,
            owner_agent=AgentRole.CONTEXT,
        )

    def _label_values(self, text: str, labels: tuple[str, ...]) -> tuple[str, ...]:
        target_labels = "|".join(re.escape(item) for item in sorted(labels, key=len, reverse=True))
        pattern = re.compile(
            rf"(?:{target_labels})\s*[:：]?\s*(?P<value>.+?)"
            rf"(?=\s*(?:[，,；;。\n]|(?:{_FIELD_LABEL_ALTERNATION})\s*[:：]?|$))",
            flags=re.IGNORECASE,
        )
        return tuple(
            value for match in pattern.finditer(text) if (value := match.group("value").strip())
        )

    def _dates_from_labels(
        self,
        text: str,
        labels: tuple[str, ...],
        reference_date: date,
    ) -> tuple[tuple[date, str], ...]:
        values: list[tuple[date, str]] = []
        for labelled_value in self._label_values(text, labels):
            for match in _FULL_DATE_PATTERN.finditer(labelled_value):
                values.append(
                    (
                        date(
                            int(match.group("year")),
                            int(match.group("month")),
                            int(match.group("day")),
                        ),
                        match.group(0),
                    )
                )
            for match in re.finditer(
                r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日",
                labelled_value,
            ):
                try:
                    value = date(
                        reference_date.year
                        if int(match.group("month")) >= reference_date.month
                        else reference_date.year + 1,
                        int(match.group("month")),
                        int(match.group("day")),
                    )
                except ValueError:
                    continue
                values.append((value, match.group(0)))
        return tuple(values)

    def _set_unique_text_match(
        self,
        draft: _Draft,
        field_name: str,
        values: tuple[str, ...],
    ) -> None:
        unique = tuple(dict.fromkeys(values))
        if not unique:
            return
        self._set_fact(
            draft,
            field_name,
            unique[0],
            RequirementFactSource.EXPLICIT_TEXT,
            unique[0],
        )
        if len(unique) > 1:
            draft.conflicts.append(
                RequirementConflict(
                    field=field_name,
                    deterministic_value=unique[0],
                    model_value=unique[1],
                    reason="用户文本中出现多个不同的明确值",
                )
            )

    def _set_fact(
        self,
        draft: _Draft,
        field_name: str,
        value: object,
        source: RequirementFactSource,
        evidence_text: str,
        *,
        explicit: bool = True,
        overwrite: bool = False,
    ) -> None:
        if field_name in draft.values and not overwrite:
            if self._json_value(draft.values[field_name]) != self._json_value(value):
                draft.conflicts.append(
                    RequirementConflict(
                        field=field_name,
                        deterministic_value=self._json_value(draft.values[field_name]),
                        model_value=self._json_value(value),
                        reason="确定性抽取发现互相冲突的用户值",
                    )
                )
            return
        draft.values[field_name] = value
        draft.facts[field_name] = ExtractedRequirementFact(
            field=field_name,
            value=self._json_value(value),
            source=source,
            evidence_text=evidence_text,
            explicit=explicit,
        )

    def _add_unresolved(
        self,
        draft: _Draft,
        item: UnresolvedRequirement,
    ) -> None:
        if any(existing.field == item.field for existing in draft.unresolved):
            return
        draft.unresolved.append(item)

    def _preference_rule(
        self,
        *,
        key: str,
        mode: PreferenceMode,
        weight: float,
        expected: JsonValue | None,
        reason: str,
        captured_at: datetime,
    ) -> PreferenceRule:
        return PreferenceRule(
            key=key,
            mode=mode,
            weight=weight,
            expected=expected,
            source=PreferenceSource.EXPLICIT_CURRENT_TRIP,
            reason=reason,
            created_at=captured_at,
        )

    def _claim_boundary(
        self,
        state: PackageRequestState,
        draft: _Draft,
        model_error: str | None,
    ) -> str:
        normalization = "；".join(dict.fromkeys(draft.notes))
        if state == PackageRequestState.HUMAN_BLOCK:
            return (
                "仅保留用户文本中的可定位事实与模型提案；存在关键字段缺失或冲突，"
                "已阻塞搜索和规划，不会用模型猜测补齐。"
                + (f" {normalization}。" if normalization else "")
            )
        boundary = (
            "硬约束来自用户明确文本、结构化用户覆盖或声明过的确定性归一化；"
            "模型仅提供待核对的歧义与偏好提案，不能覆盖锁定值。"
        )
        if "exact_return_date" in draft.values:
            boundary += "明确出返日期已确定性换算为住宿夜数，"
        else:
            boundary += "日期窗口尚未选定具体出返日期，"
        boundary += "本结果也不包含报价、库存或可订承诺。"
        if normalization:
            boundary += f" {normalization}。"
        if any(item.field.startswith("ignored_model_conflict:") for item in draft.unresolved):
            boundary += " 与锁定用户值冲突的低权威模型提案已记录并忽略，未阻塞执行。"
        if model_error is not None:
            boundary += " 无效模型提案已忽略，确定性结果仍可独立使用。"
        return boundary

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("requirement agent clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)

    def _json_value(self, value: object) -> JsonValue:
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return _JSON_VALUE.validate_python(value)

    def _date_value(self, value: object) -> date:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise TypeError("value is not a date")

    def _optional_int(self, value: object | None) -> int | None:
        if value is None:
            return None
        return self._int_value(value)

    def _int_value(self, value: object) -> int:
        if isinstance(value, bool):
            raise TypeError("boolean is not an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return int(value)
        raise TypeError("value is not an integer")

    def _optional_bool(self, value: object | None) -> bool | None:
        if value is None or isinstance(value, bool):
            return value
        raise TypeError("value is not a boolean")

    def _default_weight(self, mode: PreferenceMode) -> float:
        return {
            PreferenceMode.REQUIRED: 1,
            PreferenceMode.WEIGHTED: 0.7,
            PreferenceMode.FORBIDDEN: 1,
            PreferenceMode.INDIFFERENT: 0,
        }[mode]
