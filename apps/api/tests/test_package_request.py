from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from tripchord.agents.agent_budget import AgentBudgetLedger, bind_agent_budget
from tripchord.agents.model_gateway import (
    ModelResponse,
    ModelRouter,
    ScriptedModelClient,
)
from tripchord.agents.models import AgentRole, PreferenceMode, PreferenceSource
from tripchord.agents.package_request import (
    HybridPackageRequirementAgent,
    PackageRequestState,
    PackageRequirementRequest,
    RequirementFactSource,
    project_preferences_to_intent_template,
)
from tripchord.planning.flexible_dates import FlexibleTravelWindow

NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
ORIGINAL_REQUEST = """出发地：杭州
目的地：马累
去程：2026-8月
返程：玩5-8天
人数：2名成人
酒店：1间房
偏好：提供几个方案对比一下预算、早餐无要求、星级无要求、无行李、接受中转"""


def fixed_now() -> datetime:
    return NOW


def future_now() -> datetime:
    return datetime(2030, 1, 15, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_deterministic_parser_handles_original_request_without_model() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(ORIGINAL_REQUEST)

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.destination == "马累"
    assert result.window.origin_code == "HGH"
    assert result.window.destination_code == "MLE"
    assert result.window.earliest_departure == date(2026, 8, 1)
    assert result.window.latest_departure == date(2026, 8, 31)
    assert (result.window.min_nights, result.window.max_nights) == (4, 7)
    assert (result.window.adults, result.window.rooms) == (2, 1)
    assert result.window.currency == "CNY"

    template = result.intent_template
    assert template is not None
    assert template.budget_cents is None
    assert template.require_checked_baggage is False
    assert template.allow_connections is None
    assert template.require_breakfast is None
    assert template.breakfast_preference_mode == PreferenceMode.INDIFFERENT
    assert template.breakfast_preference_weight == 0
    materialized = template.materialize(date(2026, 8, 12), date(2026, 8, 18))
    assert materialized.night_count == 6
    assert materialized.origin == "杭州"

    preferences = {rule.key: rule for rule in result.preferences.rules}
    assert preferences["hotel_breakfast"].mode == PreferenceMode.INDIFFERENT
    assert preferences["hotel_star_rating"].mode == PreferenceMode.INDIFFERENT
    assert preferences["checked_baggage"].mode == PreferenceMode.INDIFFERENT
    assert preferences["checked_baggage"].expected is False
    assert preferences["flight_connections"].expected is None
    assert preferences["compare_budget_options"].mode == PreferenceMode.REQUIRED
    assert not result.unresolved
    assert not result.conflicts
    assert "不包含报价、库存或可订承诺" in result.claim_boundary
    assert all(item.owner_agent == AgentRole.CONTEXT for item in result.context_evidence)
    origin_code = next(item for item in result.facts if item.field == "origin_code")
    destination_code = next(item for item in result.facts if item.field == "destination_code")
    assert origin_code.value == "HGH"
    assert destination_code.value == "MLE"
    assert origin_code.source == RequirementFactSource.DETERMINISTIC_DERIVATION
    assert destination_code.source == RequirementFactSource.DETERMINISTIC_DERIVATION
    assert origin_code.explicit is False
    assert destination_code.explicit is False
    assert "受信地点身份表" in origin_code.evidence_text
    assert "受信地点身份表" in destination_code.evidence_text


@pytest.mark.asyncio
async def test_natural_flexible_window_defaults_two_adults_to_one_room() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=(
                "我想从杭州出发去马尔代夫，2026年8月20日起、最晚在2026年9月10日前完成，"
                "玩4到8天。先按2位成人，不带儿童。"
            ),
            reference_date=date(2026, 8, 20),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.latest_departure == date(2026, 9, 7)
    assert (result.window.adults, result.window.children, result.window.rooms) == (2, 0, 1)
    rooms = next(item for item in result.facts if item.field == "rooms")
    assert rooms.value == 1
    assert rooms.source == RequirementFactSource.SYSTEM_DEFAULT
    assert rooms.explicit is False
    assert "默认值1间房" in result.claim_boundary


@pytest.mark.asyncio
async def test_explicit_no_connections_becomes_a_typed_execution_constraint() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，2026年8月出发，玩5晚，"
        "2名成人，1间房，不接受中转"
    )

    assert result.state == PackageRequestState.READY
    assert result.intent_template is not None
    assert result.intent_template.allow_connections is False
    materialized = result.intent_template.materialize(
        date(2026, 8, 12), date(2026, 8, 17)
    )
    assert materialized.allow_connections is False
    rule = result.preferences.effective("flight_connections")
    assert rule is not None
    assert rule.mode == PreferenceMode.FORBIDDEN


@pytest.mark.asyncio
async def test_unknown_city_iata_is_not_guessed_and_blocks_live_search() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：曼谷，2026年8月出发，玩5晚，2名成人，1间房"
    )

    assert result.state == PackageRequestState.HUMAN_BLOCK
    assert result.window is not None
    assert result.window.origin_code == "HGH"
    assert result.window.destination_code is None
    assert not any(item.field == "destination_code" for item in result.facts)
    unresolved = next(item for item in result.unresolved if item.field == "destination_code")
    assert unresolved.critical
    assert "未命中受信 IATA 身份表" in unresolved.reason
    assert "避免模型猜测或伪造机场代码" in unresolved.reason


@pytest.mark.asyncio
async def test_explicit_uppercase_iata_is_preserved_as_user_evidence() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：HGH，目的地：MLE，2026年8月出发，玩5晚，2名成人，1间房"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin_code == "HGH"
    assert result.window.destination_code == "MLE"
    code_facts = {
        item.field: item
        for item in result.facts
        if item.field in {"origin_code", "destination_code"}
    }
    assert code_facts["origin_code"].source == RequirementFactSource.EXPLICIT_TEXT
    assert code_facts["destination_code"].source == RequirementFactSource.EXPLICIT_TEXT
    assert code_facts["origin_code"].explicit is True
    assert code_facts["destination_code"].explicit is True


@pytest.mark.asyncio
async def test_structured_breakfast_weight_is_a_user_override() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=ORIGINAL_REQUEST,
            breakfast_weight=0.95,
        )
    )

    breakfast = result.preferences.effective("hotel_breakfast")
    assert breakfast is not None
    assert breakfast.mode == PreferenceMode.WEIGHTED
    assert breakfast.weight == 0.95
    assert breakfast.source == PreferenceSource.EXPLICIT_CURRENT_TRIP
    assert result.intent_template is not None
    assert result.intent_template.require_breakfast is None
    assert result.intent_template.breakfast_preference_mode == PreferenceMode.WEIGHTED
    assert result.intent_template.breakfast_preference_weight == 0.95
    fact = next(item for item in result.facts if item.field == "require_breakfast")
    assert fact.source == RequirementFactSource.STRUCTURED_USER_OVERRIDE
    assert not any(
        item.field == "preference_application:hotel_breakfast" for item in result.unresolved
    )
    assert "尚未进入实时 Planner 软评分" not in result.claim_boundary
    assert result.state == PackageRequestState.READY


@pytest.mark.asyncio
async def test_scripted_model_can_add_a_weighted_preference_but_not_hard_facts() -> None:
    client = ScriptedModelClient(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "preferences": [
                            {
                                "key": "quiet_room",
                                "mode": "weighted",
                                "weight": 0.8,
                                "expected": True,
                                "reason": "用户提到想安静休息，但未设为硬约束",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                provider="placeholder",
                model="placeholder",
            ),
        ),
        model="requirement-proposer",
    )
    ledger = AgentBudgetLedger()
    with bind_agent_budget(ledger):
        result = await HybridPackageRequirementAgent(
            model_client=client,
            now=fixed_now,
        ).parse(ORIGINAL_REQUEST)

    assert result.state == PackageRequestState.READY
    quiet_room = result.preferences.effective("quiet_room")
    assert quiet_room is not None
    assert quiet_room.mode == PreferenceMode.WEIGHTED
    assert quiet_room.weight == 0.8
    assert quiet_room.source == PreferenceSource.INFERRED_CURRENT_CONTEXT
    assert result.model_proposal is not None
    assert ledger.audit().admissions[0].task_id == "interpret-package-requirements"
    assert ledger.audit().admissions[0].role == AgentRole.CONTEXT
    assert client.requests[0].response_schema is not None
    prompt = json.loads(client.requests[0].messages[0].content)
    assert prompt["locked_facts"]["origin"] == "杭州"
    assert prompt["locked_facts"]["min_nights"] == 4
    assert any(
        item.topic == "package_requirement_model_proposal" for item in result.context_evidence
    )


@pytest.mark.asyncio
async def test_model_conflict_is_ignored_and_explicit_user_values_stay_locked() -> None:
    control = ScriptedModelClient(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "origin": "上海",
                        "require_breakfast": True,
                        "preferences": [
                            {
                                "key": "hotel_breakfast",
                                "mode": "required",
                                "weight": 1,
                                "expected": True,
                                "reason": "模型自行推断早餐重要",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                provider="placeholder",
                model="placeholder",
            ),
        ),
        model="control-model",
    )
    router = ModelRouter(
        {AgentRole.CONTEXT: control},
        high_risk_client=control,
    )
    result = await HybridPackageRequirementAgent(
        model_router=router,
        now=fixed_now,
    ).parse(ORIGINAL_REQUEST)

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.intent_template is not None
    assert result.intent_template.require_breakfast is None
    assert not result.conflicts
    ignored_fields = {item.field for item in result.unresolved}
    assert {
        "ignored_model_conflict:origin",
        "ignored_model_conflict:require_breakfast",
        "ignored_model_conflict:preference:hotel_breakfast",
    } <= ignored_fields
    assert "低权威模型提案已记录并忽略" in result.claim_boundary


@pytest.mark.asyncio
async def test_model_only_proposals_cannot_fill_missing_critical_fields() -> None:
    client = ScriptedModelClient(
        (
            ModelResponse(
                text=json.dumps(
                    {
                        "destination": "马累",
                        "earliest_departure": "2026-08-01",
                        "latest_departure": "2026-08-31",
                        "min_nights": 5,
                        "max_nights": 8,
                        "adults": 2,
                        "rooms": 1,
                    },
                    ensure_ascii=False,
                ),
                provider="placeholder",
                model="placeholder",
            ),
        )
    )
    result = await HybridPackageRequirementAgent(
        model_client=client,
        now=fixed_now,
    ).parse("出发地：杭州，想暑假去海岛自由行")

    assert result.state == PackageRequestState.HUMAN_BLOCK
    assert result.window is None
    assert result.intent_template is None
    unresolved = {item.field: item for item in result.unresolved}
    assert unresolved["destination"].model_proposal == "马累"
    assert unresolved["destination"].critical
    assert (
        "用户文本未明确" in unresolved["adults"].reason
        or "只有模型提案" in unresolved["adults"].reason
    )


@pytest.mark.asyncio
async def test_invalid_model_output_is_ignored_when_deterministic_facts_are_complete() -> None:
    client = ScriptedModelClient(
        (
            ModelResponse(
                text='{"preferences":[{"key":"hotel_breakfast","mode":"not-a-mode"}]}',
                provider="placeholder",
                model="placeholder",
            ),
        )
    )
    result = await HybridPackageRequirementAgent(
        model_client=client,
        now=fixed_now,
    ).parse(ORIGINAL_REQUEST)

    assert result.state == PackageRequestState.READY
    assert result.model_proposal is None
    model_issue = next(item for item in result.unresolved if item.field == "model_proposal")
    assert not model_issue.critical
    assert "无效模型提案已忽略" in result.claim_boundary


@pytest.mark.asyncio
async def test_exact_dates_deterministically_materialize_a_single_duration() -> None:
    text = (
        "出发地：杭州\n目的地：马累\n去程：2026-08-12\n"
        "返程：2026-08-18\n人数：2名成人\n酒店：1间房\n预算3万元人民币"
    )
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(text)

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == result.window.latest_departure
    assert (result.window.min_nights, result.window.max_nights) == (6, 6)
    assert result.intent_template is not None
    assert result.intent_template.budget_cents == 3_000_000


@pytest.mark.asyncio
async def test_natural_language_route_does_not_absorb_trailing_trip_words() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "从杭州出发去马累玩5晚，2名成人，住1间房，2026年8月出发"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.destination == "马累"


@pytest.mark.asyncio
async def test_yearless_month_uses_declared_reference_date_without_model_guessing() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text="出发地：杭州，目的地：马累，8月出发，玩5晚，2名成人，1间房",
            reference_date=date(2026, 7, 30),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 8, 1)
    assert result.window.latest_departure == date(2026, 8, 31)


@pytest.mark.asyncio
async def test_request_default_reference_date_uses_injected_agent_clock() -> None:
    result = await HybridPackageRequirementAgent(now=future_now).parse(
        PackageRequirementRequest(text="出发地：杭州，目的地：马累，8月出发，玩5晚，2名成人，1间房")
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2030, 8, 1)
    assert result.window.latest_departure == date(2030, 8, 31)


@pytest.mark.asyncio
async def test_same_month_date_range_is_checked_without_model() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，2026年8月12日至18日，2名成人，1间房"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 8, 12)
    assert result.window.latest_departure == date(2026, 8, 12)
    assert result.window.min_nights == result.window.max_nights == 6


@pytest.mark.asyncio
async def test_explicit_nights_take_precedence_in_five_days_four_nights() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，2026年8月出发，玩5天4晚，2名成人，1间房"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.min_nights == result.window.max_nights == 4
    assert not result.conflicts


@pytest.mark.asyncio
async def test_invalid_full_date_blocks_instead_of_falling_back_to_month() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，去程：2026-02-30，玩5晚，2名成人，1间房"
    )

    assert result.state == PackageRequestState.HUMAN_BLOCK
    assert result.window is None
    date_issue = next(item for item in result.unresolved if item.field == "date_window")
    assert date_issue.critical
    assert "禁止把非法日号降级" in date_issue.reason


@pytest.mark.asyncio
async def test_date_labels_are_respected_even_when_return_appears_first() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，返程：2026-08-18，去程：2026-08-12，2名成人，1间房"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 8, 12)
    assert result.window.latest_departure == date(2026, 8, 12)
    assert result.window.min_nights == result.window.max_nights == 6


@pytest.mark.asyncio
async def test_real_natural_trip_request_ignores_current_date_and_keeps_return_targets() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=(
                "当前日期 2026-08-19；杭州出发，马尔代夫及合理周边组合。"
                "出发窗口从 2026-08-20 开始，4到8天，2位成人（本人和女朋友）。"
                "住宿不能简陋或偏僻，可以有一定品质但价格不能过高；"
                "搜索时同时覆盖9月9日与9月10日返程。"
            ),
            reference_date=date(2026, 8, 19),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.destination == "马尔代夫"
    assert result.window.earliest_departure == date(2026, 8, 20)
    assert result.window.latest_departure == date(2026, 9, 7)
    assert result.window.latest_return_date == date(2026, 9, 10)
    assert result.window.latest_arrival_date == date(2026, 9, 10)
    assert result.window.return_date_targets == (date(2026, 9, 9), date(2026, 9, 10))
    assert (result.window.min_nights, result.window.max_nights) == (3, 7)
    assert result.window.adults == 2
    assert result.window.rooms == 1
    preferences = {rule.key: rule for rule in result.preferences.rules}
    assert preferences["lodging_quality"].mode == PreferenceMode.REQUIRED
    assert preferences["lodging_quality"].expected == "not_basic"
    assert preferences["lodging_location"].mode == PreferenceMode.REQUIRED
    assert preferences["lodging_location"].expected == "convenient_not_remote"
    assert preferences["lodging_price"].mode == PreferenceMode.WEIGHTED
    assert preferences["lodging_price"].expected == "reasonable_not_high"
    assert result.intent_template is not None
    projected, unapplied = project_preferences_to_intent_template(
        result.intent_template,
        result.preferences,
    )
    assert projected.require_non_basic_lodging is True
    assert projected.require_non_remote_lodging is True
    assert "lodging_quality" not in unapplied
    assert "lodging_location" not in unapplied


@pytest.mark.asyncio
async def test_date_start_and_completion_boundary_form_a_flexible_window() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=(
                "2026-08-20起，需在2026-09-10边界内完成；杭州出发去马尔代夫，"
                "4-8天，2名成人，1间房。"
            ),
            reference_date=date(2026, 8, 19),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 8, 20)
    assert result.window.latest_departure == date(2026, 9, 7)
    assert result.window.latest_arrival_date == date(2026, 9, 10)
    assert result.window.latest_return_date == date(2026, 9, 10)
    assert result.window.return_date_targets == (date(2026, 9, 9), date(2026, 9, 10))
    assert (result.window.min_nights, result.window.max_nights) == (3, 7)


@pytest.mark.asyncio
async def test_verbatim_maldives_request_parses_gateway_and_island_comparison() -> None:
    text = (
        "我要从杭州出发去马尔代夫周边游，时间：从明天开始到9月10日前的4-8天游，"
        "人数：我和女朋友两个人，偏好：酒店不能太简陋，地址不能太偏，可以稍微有点品质但价格不能过高，"
        "到达和返程可以住机场附近，但也要关注有没有更好的选择。"
    )
    result = await HybridPackageRequirementAgent(
        now=lambda: datetime(2026, 8, 19, 12, tzinfo=UTC)
    ).parse(
        PackageRequirementRequest(text=text, reference_date=date(2026, 8, 19))
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.origin_code == "HGH"
    assert result.window.destination == "马尔代夫"
    assert result.window.destination_code == "MLE"
    assert result.window.earliest_departure == date(2026, 8, 20)
    assert result.window.latest_departure == date(2026, 9, 7)
    assert result.window.latest_arrival_date == date(2026, 9, 10)
    assert result.window.latest_return_date == date(2026, 9, 10)
    assert result.window.return_date_targets == (date(2026, 9, 9), date(2026, 9, 10))
    assert (result.window.min_nights, result.window.max_nights) == (3, 7)
    assert (result.window.adults, result.window.rooms) == (2, 1)
    preferences = {rule.key: rule for rule in result.preferences.rules}
    assert preferences["lodging_quality"].expected == "not_basic"
    assert preferences["lodging_location"].expected == "convenient_not_remote"
    assert preferences["lodging_price"].expected == "reasonable_not_high"
    assert preferences["airport_lodging_fallback"].mode == PreferenceMode.INDIFFERENT
    assert preferences["lodging_zone_comparison"].mode == PreferenceMode.REQUIRED


@pytest.mark.asyncio
async def test_real_request_binds_return_home_deadline_into_intent_template() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=(
                "杭州出发去马尔代夫，出发窗口从2026-08-20开始，4到8天，2位成人，1间房，"
                "搜索9月9日和9月10日返程，实际回杭州不晚于9月10日"
            ),
            reference_date=date(2026, 8, 19),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.latest_arrival_date == date(2026, 9, 10)
    assert result.intent_template is not None
    assert result.intent_template.latest_arrival_date == date(2026, 9, 10)


@pytest.mark.asyncio
async def test_real_request_couple_alias_and_known_origin_phrase() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=(
                "当前日期 2026-08-19；杭州出发，马尔代夫。出发窗口从 2026-08-20 开始，"
                "4到8天，2位成人（本人和女友）；搜索时覆盖9月9日与9月10日返程。"
            ),
            reference_date=date(2026, 8, 19),
        )
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.rooms == 1


def test_return_boundary_filters_pairs_and_preserves_requested_return_targets() -> None:
    window = FlexibleTravelWindow(
        origin="杭州",
        destination="马尔代夫",
        earliest_departure=date(2026, 8, 20),
        latest_departure=date(2026, 9, 7),
        min_nights=3,
        max_nights=7,
        latest_return_date=date(2026, 9, 10),
        return_date_targets=(date(2026, 9, 9), date(2026, 9, 10)),
    )

    pairs = window.all_date_pairs()
    assert (date(2026, 9, 2), date(2026, 9, 9)) in pairs
    assert (date(2026, 9, 3), date(2026, 9, 10)) in pairs
    assert all(return_date <= date(2026, 9, 10) for _, return_date in pairs)


def test_actual_return_home_boundary_also_limits_date_pairs_without_search_targets() -> None:
    window = FlexibleTravelWindow(
        origin="杭州",
        destination="马尔代夫",
        earliest_departure=date(2026, 8, 20),
        latest_departure=date(2026, 9, 7),
        min_nights=3,
        max_nights=7,
        latest_arrival_date=date(2026, 9, 10),
    )

    pairs = window.all_date_pairs()

    assert pairs
    assert all(return_date <= date(2026, 9, 10) for _, return_date in pairs)


@pytest.mark.asyncio
async def test_single_line_labels_without_colons_do_not_swallow_next_field() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地 杭州 目的地 马累 去程 2026-08-12 返程 2026-08-18 人数 2名成人 酒店 1间房"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.origin == "杭州"
    assert result.window.destination == "马累"
    assert result.window.min_nights == result.window.max_nights == 6


@pytest.mark.asyncio
async def test_past_departure_window_blocks_against_declared_reference_date() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        PackageRequirementRequest(
            text=("出发地：杭州，目的地：马累，去程：2026-07-20，返程：2026-07-25，2名成人，1间房"),
            reference_date=date(2026, 7, 30),
        )
    )

    assert result.state == PackageRequestState.HUMAN_BLOCK
    assert any(
        item.field == "date_window" and "早于基准日期" in item.reason for item in result.unresolved
    )


@pytest.mark.asyncio
async def test_per_person_budget_is_converted_but_component_budget_is_not() -> None:
    base = "出发地：杭州，目的地：马累，2026年8月出发，玩5晚，2名成人，1间房，"
    per_person = await HybridPackageRequirementAgent(now=fixed_now).parse(
        base + "人均预算1万元人民币"
    )
    flight_only = await HybridPackageRequirementAgent(now=fixed_now).parse(
        base + "机票预算2万元人民币"
    )

    assert per_person.state == PackageRequestState.READY
    assert per_person.intent_template is not None
    assert per_person.intent_template.budget_cents == 2_000_000
    assert "人均预算已按明确成人数换算" in per_person.claim_boundary

    assert flight_only.state == PackageRequestState.READY
    assert flight_only.intent_template is not None
    assert flight_only.intent_template.budget_cents is None
    budget_scope = next(item for item in flight_only.unresolved if item.field == "budget_scope")
    assert not budget_scope.critical
    assert "分项预算" in budget_scope.reason


@pytest.mark.asyncio
async def test_conflicting_breakfast_text_requires_human_resolution() -> None:
    result = await HybridPackageRequirementAgent(now=fixed_now).parse(
        "出发地：杭州，目的地：马累，2026年8月出发，玩5晚，2名成人，1间房，早餐无要求但必须有早餐"
    )

    assert result.state == PackageRequestState.HUMAN_BLOCK
    conflict = next(item for item in result.conflicts if item.field == "preference:hotel_breakfast")
    assert "互斥" in conflict.reason


def test_non_weighted_breakfast_mode_rejects_noncanonical_weight() -> None:
    with pytest.raises(ValueError, match="use weighted mode"):
        PackageRequirementRequest(
            text=ORIGINAL_REQUEST,
            breakfast_mode=PreferenceMode.REQUIRED,
            breakfast_weight=0.8,
        )


@pytest.mark.asyncio
async def test_full_chinese_fixed_departure_and_return_dates_are_ready() -> None:
    result = await HybridPackageRequirementAgent(
        now=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    ).parse(
        "2026年9月3日从杭州出发去马尔代夫，2026年9月9日返程，2名成人，1间房。"
        "住宿不能简陋或偏僻，兼顾品质与价格；比较机场附近过渡住宿和更优选择。"
        "只查询、比较和建议，不下单、不付款。"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 9, 3)
    assert result.window.latest_departure == date(2026, 9, 3)
    assert result.window.latest_arrival_date == date(2026, 9, 9)
    assert (result.window.min_nights, result.window.max_nights) == (6, 6)


@pytest.mark.asyncio
async def test_return_date_with_destination_departure_context_is_not_second_outbound() -> None:
    result = await HybridPackageRequirementAgent(
        now=lambda: datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    ).parse(
        "2026年9月3日从杭州出发去马尔代夫，2026年9月9日从马累出发返程，"
        "2名成人，1间房。住宿不能简陋或偏僻，兼顾品质与价格。"
    )

    assert result.state == PackageRequestState.READY
    assert result.window is not None
    assert result.window.earliest_departure == date(2026, 9, 3)
    assert result.window.latest_departure == date(2026, 9, 3)
    assert result.window.latest_arrival_date == date(2026, 9, 9)
    assert (result.window.min_nights, result.window.max_nights) == (6, 6)
