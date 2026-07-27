from datetime import date

import pytest
from tripchord.planning.requirements import ChineseRequirementParser


def test_parser_extracts_required_fields_with_evidence() -> None:
    result = ChineseRequirementParser().parse(
        "从上海去北京，10月1日到10月4日，预算5000元，2人，每天最多2个景点，喜欢历史和本地菜。",
        default_year=2026,
    )

    assert result.complete
    spec = result.to_spec()
    assert spec.origin == "上海"
    assert spec.destinations == ("北京",)
    assert spec.start_date == date(2026, 10, 1)
    assert spec.end_date == date(2026, 10, 4)
    assert spec.party.adults == 2
    assert spec.max_main_activities_per_day == 2
    assert spec.budget is not None and spec.budget.amount == 5000
    assert "历史" in spec.interests
    assert "本地菜" in spec.interests
    assert {item.field for item in result.evidence} >= {
        "origin",
        "destination",
        "start_date",
        "end_date",
        "budget_cny",
    }


def test_parser_asks_instead_of_inventing_missing_dates() -> None:
    result = ChineseRequirementParser().parse("想从上海去北京看看历史建筑", default_year=2026)

    assert not result.complete
    assert result.missing_fields == ("start_date", "end_date")
    assert len(result.clarifying_questions) == 2
    with pytest.raises(ValueError, match="missing fields"):
        result.to_spec()
