from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from tripchord.planning.event_contracts import (
    EventDisposition,
    OfferValueSnapshot,
    resolve_offer_event,
)
from tripchord.planning.offer_semantics import (
    OfferIdentityConfidence,
    OfferIdentitySource,
    OfferSemanticChange,
    semantic_offer_diff,
    stable_offer_identity,
)
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackageEventKind,
    PackagePlaceKey,
)

NOW = datetime(2026, 8, 3, 9, tzinfo=UTC)


def lodging(
    quote_id: str,
    *,
    property_name: str = "Kaani Palm Beach",
    total_cents: int = 320_000,
    captured_at: datetime = NOW,
    evidence_ref: str = "browser:quote:v1",
    provider_property_id: str | None = "hotel-100",
    provider_room_id: str | None = "room-king",
    provider_offer_id: str | None = "rate-flex",
    cancellation_policy: str | None = "free cancellation",
    payment_policy: str | None = "online_prepay",
    room_name: str | None = "Deluxe King Room",
) -> NormalizedLodgingQuote:
    return NormalizedLodgingQuote(
        id=quote_id,
        provider="ctrip",
        total_for_party_cents=total_cents,
        taxes_and_fees_included=True,
        captured_at=captured_at,
        expires_at=captured_at + timedelta(minutes=10),
        evidence_refs=(evidence_ref,),
        provider_offer_id=provider_offer_id,
        property_name=property_name,
        area=PackageArea.DESTINATION_ISLAND,
        place_key=PackagePlaceKey.MAAFUSHI,
        check_in=date(2026, 8, 20),
        check_out=date(2026, 8, 25),
        adults=2,
        rooms=1,
        breakfast_included=False,
        provider_property_id=provider_property_id,
        provider_room_id=provider_room_id,
        room_name=room_name,
        bed_type="king",
        cancellation_policy=cancellation_policy,
        payment_policy=payment_policy,
    )


def flight(
    quote_id: str,
    *,
    outbound_numbers: tuple[str, ...],
    return_numbers: tuple[str, ...],
    provider_offer_id: str | None = None,
) -> NormalizedFlightQuote:
    return NormalizedFlightQuote(
        id=quote_id,
        provider="ctrip",
        provider_offer_id=provider_offer_id,
        total_for_party_cents=500_000,
        taxes_and_fees_included=True,
        captured_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        evidence_refs=("browser:flight:v1",),
        origin="HGH",
        destination="MLE",
        adults=2,
        outbound_depart_at=datetime(2026, 8, 20, 8, tzinfo=UTC),
        outbound_arrive_at=datetime(2026, 8, 20, 18, tzinfo=UTC),
        return_depart_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
        return_arrive_at=datetime(2026, 8, 25, 20, tzinfo=UTC),
        outbound_flight_numbers=outbound_numbers,
        return_flight_numbers=return_numbers,
        cabin_class="economy",
    )


def resolve(
    kind: PackageEventKind,
    old: NormalizedLodgingQuote,
    *observations: NormalizedLodgingQuote,
):
    return resolve_offer_event(
        event_id="event-1",
        trip_id="trip-1",
        kind=kind,
        target_component_id=old.id,
        source="ctrip-read-only",
        occurred_at=NOW,
        observed_at=NOW + timedelta(minutes=1),
        old=old,
        compatible_observations=observations,
    )


def test_stable_offer_identity_ignores_dom_id_price_and_observation_receipt() -> None:
    before = lodging("dom-card-1")
    repriced = lodging(
        "dom-card-99",
        total_cents=335_000,
        captured_at=NOW + timedelta(minutes=1),
        evidence_ref="browser:quote:v2",
    )

    assert stable_offer_identity(before) == stable_offer_identity(repriced)
    identity = stable_offer_identity(before)
    assert identity.product_source == OfferIdentitySource.PROVIDER_OFFICIAL_ID
    assert identity.product_confidence == OfferIdentityConfidence.HIGH
    assert not identity.product_ambiguous
    assert not identity.offer_ambiguous
    difference = semantic_offer_diff(before, repriced)
    assert difference.change == OfferSemanticChange.PRICE_CHANGED
    assert difference.same_offer
    assert difference.price_changed


def test_same_price_refetch_is_refresh_and_never_creates_a_repair_candidate() -> None:
    before = lodging("dom-card-1")
    refreshed = lodging(
        "dom-card-99",
        captured_at=NOW + timedelta(minutes=1),
        evidence_ref="browser:quote:v2",
    )

    replacement, resolution = resolve(PackageEventKind.PRICE_CHANGED, before, refreshed)

    assert replacement is None
    assert resolution.disposition == EventDisposition.REFRESH
    assert not resolution.verified_change
    assert resolution.semantic_diff is not None
    assert not resolution.semantic_diff.price_changed
    assert resolution.envelope.old_value.total_for_party_cents == 320_000
    assert resolution.envelope.new_value is not None
    assert resolution.envelope.new_value.total_for_party_cents == 320_000
    assert len(resolution.envelope.dedupe_key) == 64


def test_price_change_requires_same_stable_offer_and_real_amount_delta() -> None:
    before = lodging("dom-card-1")
    repriced = lodging("dom-card-2", total_cents=335_000)
    another_hotel = lodging(
        "dom-card-3",
        property_name="Arena Beach Hotel",
        total_cents=310_000,
        provider_property_id="hotel-200",
        provider_room_id="room-twin",
        provider_offer_id="rate-arena",
    )

    replacement, verified = resolve(PackageEventKind.PRICE_CHANGED, before, repriced)
    wrong_replacement, blocked = resolve(
        PackageEventKind.PRICE_CHANGED,
        before,
        another_hotel,
    )

    assert replacement == repriced
    assert verified.disposition == EventDisposition.LOCAL_REPAIR
    assert verified.verified_change
    assert wrong_replacement is None
    assert blocked.disposition == EventDisposition.HUMAN_BLOCK
    assert blocked.candidate_pool_expansion_required


def test_sold_out_excludes_same_stable_offer_even_when_dom_id_changes() -> None:
    before = lodging("dom-card-1")
    same_product = lodging("dom-card-after-refresh")
    alternative = lodging(
        "dom-card-alternative",
        property_name="Arena Beach Hotel",
        total_cents=340_000,
        provider_property_id="hotel-200",
        provider_room_id="room-twin",
        provider_offer_id="rate-arena",
    )

    replacement, resolution = resolve(
        PackageEventKind.SOLD_OUT,
        before,
        same_product,
        alternative,
    )

    assert replacement == alternative
    assert resolution.disposition == EventDisposition.LOCAL_REPAIR
    assert resolution.envelope.new_value is not None
    assert resolution.envelope.new_value.stable_offer_key != (
        resolution.envelope.old_value.stable_offer_key
    )


def test_retried_equivalent_events_have_the_same_dedupe_key() -> None:
    before = lodging("dom-card-1")
    repriced = lodging("dom-card-2", total_cents=335_000)

    _, first = resolve(PackageEventKind.PRICE_CHANGED, before, repriced)
    _, second = resolve_offer_event(
        event_id="retry-with-a-different-id",
        trip_id="trip-1",
        kind=PackageEventKind.PRICE_CHANGED,
        target_component_id=before.id,
        source="ctrip-read-only",
        occurred_at=NOW + timedelta(seconds=30),
        observed_at=NOW + timedelta(minutes=2),
        old=before,
        compatible_observations=(repriced,),
    )

    assert first.envelope.dedupe_key == second.envelope.dedupe_key


def test_legacy_event_snapshot_loads_as_low_confidence_ambiguous_identity() -> None:
    snapshot = OfferValueSnapshot.model_validate(
        {
            "transient_offer_id": "legacy-dom-id",
            "stable_offer_key": "a" * 64,
            "provider": "ctrip",
            "total_for_party_cents": 320_000,
            "currency": "CNY",
            "availability": "available",
            "captured_at": NOW,
            "evidence_refs": ["browser:legacy"],
        }
    )

    assert snapshot.stable_product_key is None
    assert snapshot.identity_ambiguous
    assert snapshot.product_identity_confidence == OfferIdentityConfidence.LOW


def test_semantic_fingerprint_without_provider_ids_is_explicitly_ambiguous() -> None:
    before = lodging(
        "dom-1",
        provider_property_id=None,
        provider_room_id=None,
        provider_offer_id=None,
        room_name=None,
    )
    repriced = lodging(
        "dom-2",
        total_cents=330_000,
        provider_property_id=None,
        provider_room_id=None,
        provider_offer_id=None,
        room_name=None,
    )

    identity = stable_offer_identity(before)
    difference = semantic_offer_diff(before, repriced)
    replacement, resolution = resolve(PackageEventKind.PRICE_CHANGED, before, repriced)

    assert identity.product_source == OfferIdentitySource.SEMANTIC_FINGERPRINT
    assert identity.product_ambiguous
    assert identity.offer_ambiguous
    assert "room_name_missing" in identity.ambiguity_reasons
    assert difference.change == OfferSemanticChange.IDENTITY_AMBIGUOUS
    assert not difference.same_product
    assert replacement is None
    assert resolution.disposition == EventDisposition.HUMAN_BLOCK


def test_flight_numbers_prevent_same_schedule_codeshare_collision() -> None:
    old = flight(
        "flight-old",
        outbound_numbers=("CX123", "CX601"),
        return_numbers=("CX602", "CX124"),
    )
    same_schedule_other_flights = flight(
        "flight-other",
        outbound_numbers=("SQ831", "SQ438"),
        return_numbers=("SQ437", "SQ832"),
    )

    difference = semantic_offer_diff(old, same_schedule_other_flights)

    assert difference.change == OfferSemanticChange.DIFFERENT_PRODUCT
    assert difference.different_product_confirmed
    assert not difference.same_product


def test_official_room_identity_wins_over_labels_but_not_over_a_different_room_id() -> None:
    original = lodging("original")
    relabelled_same_room = lodging(
        "relabelled",
        property_name="Kaani Palm Beach Hotel",
        room_name="豪华特大床房",
    )
    different_room = lodging(
        "different-room",
        provider_room_id="room-twin",
        provider_offer_id="rate-twin",
        room_name="Deluxe Twin Room",
    )

    same = semantic_offer_diff(original, relabelled_same_room)
    different = semantic_offer_diff(original, different_room)

    assert same.same_product
    assert same.same_offer
    assert different.different_product_confirmed
    assert different.change == OfferSemanticChange.DIFFERENT_PRODUCT


def test_sold_out_does_not_treat_same_product_changed_terms_as_replacement() -> None:
    old = lodging("old-rate")
    same_room_stricter_rate = lodging(
        "new-rate",
        provider_offer_id="rate-nonrefundable",
        cancellation_policy="non-refundable",
    )

    replacement, resolution = resolve(
        PackageEventKind.SOLD_OUT,
        old,
        same_room_stricter_rate,
    )

    assert replacement is None
    assert resolution.disposition == EventDisposition.NO_CHANGE
    assert resolution.semantic_diff is not None
    assert resolution.semantic_diff.same_product
    assert not resolution.semantic_diff.same_offer
    assert resolution.semantic_diff.change == OfferSemanticChange.TERMS_CHANGED


def test_payment_policy_change_is_an_offer_term_change_not_a_price_change() -> None:
    old = lodging("old-payment")
    changed = lodging(
        "new-payment",
        provider_offer_id="rate-pay-at-property",
        payment_policy="pay_at_property",
    )

    difference = semantic_offer_diff(old, changed)

    assert difference.same_product
    assert not difference.same_offer
    assert not difference.price_changed
    assert difference.change == OfferSemanticChange.TERMS_CHANGED
