from datetime import UTC, datetime

from tripchord.agents.models import (
    PreferenceConstitution,
    PreferenceMode,
    PreferenceRule,
    PreferenceSource,
)
from tripchord.agents.package_request import (
    PackageIntentTemplate,
    project_preferences_to_intent_template,
)


def _template() -> PackageIntentTemplate:
    return PackageIntentTemplate(
        trip_id="trip-1",
        origin="杭州",
        destination="马累",
        adults=2,
        rooms=1,
    )


def _rule(
    key: str, mode: PreferenceMode, expected: object, source: PreferenceSource
) -> PreferenceRule:
    return PreferenceRule(
        key=key,
        mode=mode,
        expected=expected,
        weight=1 if mode in {PreferenceMode.REQUIRED, PreferenceMode.FORBIDDEN} else 0.5,
        source=source,
        created_at=datetime(2026, 8, 20, tzinfo=UTC),
    )


def test_effective_preferences_project_into_executable_template() -> None:
    constitution = PreferenceConstitution(
        rules=(
            _rule(
                "hotel_breakfast",
                PreferenceMode.REQUIRED,
                True,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
            _rule(
                "checked_baggage",
                PreferenceMode.REQUIRED,
                True,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
            _rule(
                "flight_connections",
                PreferenceMode.FORBIDDEN,
                False,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
        )
    )

    projected, unapplied = project_preferences_to_intent_template(_template(), constitution)

    assert projected.require_breakfast is True
    assert projected.require_checked_baggage is True
    assert projected.allow_connections is False
    assert unapplied == ()


def test_current_trip_rule_wins_and_unsupported_rule_is_diagnostic_only() -> None:
    constitution = PreferenceConstitution(
        rules=(
            _rule(
                "checked_baggage",
                PreferenceMode.REQUIRED,
                True,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
            _rule(
                "checked_baggage",
                PreferenceMode.FORBIDDEN,
                False,
                PreferenceSource.EXPLICIT_CURRENT_TRIP,
            ),
            _rule(
                "hotel_star_rating",
                PreferenceMode.REQUIRED,
                "4_plus",
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
        )
    )

    projected, unapplied = project_preferences_to_intent_template(_template(), constitution)

    assert projected.require_checked_baggage is False
    assert unapplied == ("hotel_star_rating",)


def test_indifferent_or_weighted_boolean_preferences_do_not_create_hard_requirements() -> None:
    constitution = PreferenceConstitution(
        rules=(
            _rule(
                "checked_baggage",
                PreferenceMode.INDIFFERENT,
                None,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
            _rule(
                "flight_connections",
                PreferenceMode.WEIGHTED,
                True,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
            _rule(
                "hotel_breakfast",
                PreferenceMode.WEIGHTED,
                True,
                PreferenceSource.EXPLICIT_LONG_TERM,
            ),
        )
    )

    projected, _ = project_preferences_to_intent_template(_template(), constitution)

    assert projected.require_checked_baggage is None
    assert projected.allow_connections is None
    assert projected.require_breakfast is None
    assert projected.breakfast_preference_mode == PreferenceMode.WEIGHTED
    assert projected.breakfast_preference_weight == 0.5
