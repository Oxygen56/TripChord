from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel


class LivePlanModificationScope(StrEnum):
    LODGING = "lodging"
    FLIGHT = "flight"
    TRANSFER = "transfer"
    GLOBAL = "global"


class LivePlanModificationStatus(StrEnum):
    MODIFIED = "modified"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"
    GLOBAL_REPLAN = "global_replan"


class LodgingRoomFeature(StrEnum):
    SEA_VIEW = "sea_view"


class LivePlanDatePatch(DomainModel):
    departure_date: date | None = None
    return_date: date | None = None

    @property
    def complete(self) -> bool:
        return self.departure_date is not None and self.return_date is not None


class LivePlanModificationIntent(DomainModel):
    instruction: str = Field(min_length=1, max_length=2000)
    affected_scope: LivePlanModificationScope | None = None
    preserve_scopes: tuple[LivePlanModificationScope, ...] = ()
    exclude_current_property: bool = False
    required_room_features: tuple[LodgingRoomFeature, ...] = ()
    require_breakfast: bool | None = None
    require_non_basic_lodging: bool | None = None
    require_non_remote_lodging: bool | None = None
    date_patch: LivePlanDatePatch | None = None
    unresolved_reasons: tuple[str, ...] = ()
    parse_boundary: str = (
        "当前文本优先于既有偏好；只解析明确写出的范围、保留项、房型特征、"
        "早餐、住宿品质、位置和完整去返日期。未识别或只有单边日期时不执行、不猜测。"
    )

    @model_validator(mode="after")
    def validate_resolution(self) -> LivePlanModificationIntent:
        if len(self.preserve_scopes) != len(set(self.preserve_scopes)):
            raise ValueError("preserve scopes must be unique")
        if len(self.required_room_features) != len(set(self.required_room_features)):
            raise ValueError("required room features must be unique")
        if self.unresolved_reasons:
            return self
        if self.affected_scope is None:
            raise ValueError("resolved modification requires an affected scope")
        if self.affected_scope == LivePlanModificationScope.GLOBAL:
            if self.date_patch is None or not self.date_patch.complete:
                raise ValueError("global date modification requires both dates")
            if self.preserve_scopes:
                raise ValueError("global replan cannot promise preserved component scopes")
        return self


class LivePlanModificationSourceOutcome(DomainModel):
    provider: str = Field(min_length=1)
    state: str = Field(min_length=1)
    source_task_id: str | None = None
    quote_count: int = Field(default=0, ge=0)
    eligible_quote_count: int = Field(default=0, ge=0)
    evidence_refs: tuple[str, ...] = ()
    detail: str | None = None


class LivePlanModificationReceipt(DomainModel):
    status: LivePlanModificationStatus
    intent: LivePlanModificationIntent
    summary: str = Field(min_length=1)
    before_candidate_id: str | None = None
    after_candidate_id: str | None = None
    changed_component_ids: tuple[str, ...] = ()
    preserved_component_ids: tuple[str, ...] = ()
    before_confirmed_cny_cents: int | None = Field(default=None, ge=0)
    after_confirmed_cny_cents: int | None = Field(default=None, ge=0)
    difference_cny_cents: int | None = None
    source_task_ids: tuple[str, ...] = ()
    source_outcomes: tuple[LivePlanModificationSourceOutcome, ...] = ()
    verifier_passed: bool | None = None
    reverifier_passed: bool | None = None
    boundary: str = (
        "修改只使用本轮明确指令和可绑定证据；未提及组件默认保留。"
        "成功表示通过确定性复验，不是下单、库存锁定或结算价承诺。"
    )


_DEPARTURE_DATE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日?\s*(?:出发|起飞)"
)
_RETURN_DATE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日?\s*(?:返回|返程|回来)"
)
_DATE_LANGUAGE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日?|出发|返程|返回")


def _parsed_date(match: re.Match[str], *, default_year: int) -> date:
    return date(
        int(match.group("year") or default_year),
        int(match.group("month")),
        int(match.group("day")),
    )


def parse_live_plan_modification(
    instruction: str,
    *,
    current_departure_date: date,
) -> LivePlanModificationIntent:
    text = " ".join(instruction.strip().split())
    if not text:
        raise ValueError("modification instruction must not be empty")

    departure_match = _DEPARTURE_DATE.search(text)
    return_match = _RETURN_DATE.search(text)
    if departure_match is not None or return_match is not None or _DATE_LANGUAGE.search(text):
        if departure_match is None or return_match is None:
            return LivePlanModificationIntent(
                instruction=text,
                affected_scope=LivePlanModificationScope.GLOBAL,
                unresolved_reasons=("日期修改必须同时写明出发日和返回日",),
            )
        try:
            departure = _parsed_date(
                departure_match,
                default_year=current_departure_date.year,
            )
            returning = _parsed_date(
                return_match,
                default_year=current_departure_date.year,
            )
        except ValueError:
            return LivePlanModificationIntent(
                instruction=text,
                affected_scope=LivePlanModificationScope.GLOBAL,
                unresolved_reasons=("日期格式无效，未执行修改",),
            )
        if returning <= departure:
            return LivePlanModificationIntent(
                instruction=text,
                affected_scope=LivePlanModificationScope.GLOBAL,
                unresolved_reasons=("返回日必须晚于出发日",),
            )
        return LivePlanModificationIntent(
            instruction=text,
            affected_scope=LivePlanModificationScope.GLOBAL,
            date_patch=LivePlanDatePatch(
                departure_date=departure,
                return_date=returning,
            ),
        )

    lodging_requested = bool(re.search(r"酒店|住宿|房间|房型|海景", text))
    flight_requested = bool(re.search(r"航班|机票|起飞|降落", text))
    transfer_requested = bool(re.search(r"接驳|船班|快艇|渡轮", text))
    if not lodging_requested:
        scope = (
            LivePlanModificationScope.FLIGHT
            if flight_requested and not transfer_requested
            else LivePlanModificationScope.TRANSFER
            if transfer_requested and not flight_requested
            else None
        )
        if scope is not None:
            return LivePlanModificationIntent(
                instruction=text,
                affected_scope=scope,
                unresolved_reasons=("当前版本只支持住宿局部修改；该指令尚未执行",),
            )
        return LivePlanModificationIntent(
            instruction=text,
            unresolved_reasons=("未识别要修改的航班、住宿、接驳或完整日期",),
        )

    preserve: list[LivePlanModificationScope] = [
        LivePlanModificationScope.FLIGHT,
        LivePlanModificationScope.TRANSFER,
    ]
    if re.search(r"航班(?:也|一起)?(?:换|改)|(?:换|改)(?:一下|成)?航班", text):
        preserve.remove(LivePlanModificationScope.FLIGHT)
    if re.search(r"接驳(?:也|一起)?(?:换|改)|(?:换|改)(?:一下|成)?接驳", text):
        preserve.remove(LivePlanModificationScope.TRANSFER)
    if len(preserve) != 2:
        return LivePlanModificationIntent(
            instruction=text,
            affected_scope=LivePlanModificationScope.GLOBAL,
            unresolved_reasons=("住宿与其他组件同时修改时需要完整重新规划；当前未执行",),
        )

    required_features = (
        (LodgingRoomFeature.SEA_VIEW,) if re.search(r"海景|sea\s*view", text, re.I) else ()
    )
    exclude_current_property = bool(
        re.search(
            r"换(?:成)?(?:另|别|其他)?一(?:家|间)(?:[^，。；,]{0,24})?酒店"
            r"|换一家|另一家酒店",
            text,
        )
    )

    require_breakfast: bool | None = None
    if re.search(r"不要早餐|不含早餐|无需早餐", text):
        require_breakfast = False
    elif re.search(r"含早餐|要早餐|需要早餐|带早餐", text):
        require_breakfast = True

    require_non_basic: bool | None = None
    if re.search(r"不能(?:太)?简陋|不可(?:太)?简陋|不要基础房|不要无窗", text):
        require_non_basic = True
    elif re.search(r"可以(?:简陋|基础)|接受(?:简陋|基础)", text):
        require_non_basic = False

    require_non_remote: bool | None = None
    if re.search(r"不能(?:太)?偏僻|不可(?:太)?偏僻|位置方便|不要偏僻", text):
        require_non_remote = True
    elif re.search(r"可以偏僻|接受偏僻", text):
        require_non_remote = False

    if not (
        required_features
        or exclude_current_property
        or require_breakfast is not None
        or require_non_basic is not None
        or require_non_remote is not None
    ):
        return LivePlanModificationIntent(
            instruction=text,
            unresolved_reasons=("已识别住宿范围，但未识别可执行的修改条件",),
        )

    return LivePlanModificationIntent(
        instruction=text,
        affected_scope=LivePlanModificationScope.LODGING,
        preserve_scopes=tuple(preserve),
        exclude_current_property=exclude_current_property,
        required_room_features=required_features,
        require_breakfast=require_breakfast,
        require_non_basic_lodging=require_non_basic,
        require_non_remote_lodging=require_non_remote,
    )
