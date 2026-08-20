from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from tripchord.agents.package_request import HybridPackageRequirementAgent
from tripchord.domain.trip import TravelParty
from tripchord.planning.package import PackageIntent, PackageVerifier
from tripchord.providers.browser_bridge import BrowserSearchQuery


def test_complete_mixed_party_is_bound_end_to_end() -> None:
    party = TravelParty(adults=2, children=2, children_ages=(4, 9), infants=1, rooms=2)
    intent = PackageIntent(
        trip_id="party-test",
        origin="HGH",
        destination="MLE",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=party.adults,
        children=party.children,
        children_ages=party.children_ages,
        infants=party.infants,
        rooms=party.rooms,
    )
    query = BrowserSearchQuery(
        destination="MLE",
        start_date=intent.start_date,
        end_date=intent.end_date,
        adults=2,
        children=2,
        children_ages=(4, 9),
        infants=1,
        rooms=2,
        party_shape_supported=False,
        party_shape_failure="provider does not support this mixed party shape",
    )
    assert intent.children_ages == query.children_ages == party.children_ages


def test_chinese_ages_materialize_into_intent_template() -> None:
    import asyncio

    result = asyncio.run(
        HybridPackageRequirementAgent().parse(
            "从杭州去马尔代夫，2026年8月23日至8月30日，2成人2儿童，4岁和9岁，1婴儿，2间房"
        )
    )
    assert result.intent_template is not None
    assert result.intent_template.children_ages == (4, 9)
    assert result.intent_template.materialize(
        date(2026, 8, 23), date(2026, 8, 30)
    ).children_ages == (4, 9)


def test_verifier_rejects_component_child_age_mismatch() -> None:
    intent = PackageIntent(
        trip_id="party-test",
        origin="HGH",
        destination="MLE",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        children=2,
        children_ages=(4, 9),
    )
    quote = SimpleNamespace(
        id="quote:flight", adults=2, children=2, infants=0,
        children_ages=(4, 8),
    )
    candidate = SimpleNamespace(flight=quote, lodgings=(), transfers=())
    violations = PackageVerifier()._check_party(intent, candidate)
    assert any(item.code.value == "party_mismatch" for item in violations)


@pytest.mark.parametrize("ages", [(), (4,), (4, 18)])
def test_incomplete_or_invalid_child_ages_are_rejected(ages: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        TravelParty(adults=2, children=2, children_ages=ages)


def test_unknown_child_ages_block_chinese_requirement_publication() -> None:
    import asyncio

    result = asyncio.run(
        HybridPackageRequirementAgent().parse(
            "从杭州去马尔代夫，2026年8月23日至8月30日，2成人2儿童1婴儿，2间房"
        )
    )
    assert result.state.value == "human_block"
    assert any(item.field == "children_ages" and item.critical for item in result.unresolved)


def test_provider_query_rejects_child_age_count_mismatch() -> None:
    with pytest.raises(ValidationError):
        BrowserSearchQuery(
            destination="MLE",
            start_date=date(2026, 8, 23),
            adults=2,
            children=2,
            children_ages=(4,),
            party_shape_supported=False,
            party_shape_failure="explicit provider limitation",
        )
