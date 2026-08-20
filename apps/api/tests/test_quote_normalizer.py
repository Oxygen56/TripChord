from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from tripchord.agents.stay_area import system_stay_area_search_profile
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageArea,
    PackagePlaceKey,
)
from tripchord.providers.browser_bridge import (
    BrowserProvider,
    BrowserQuote,
    BrowserSearchQuery,
    BrowserVertical,
    LodgingInventoryConfirmedQuery,
    QuotePriceBasis,
    ctrip_trusted_flight_search_url,
    fliggy_trusted_flight_search_url,
    lodging_inventory_query_fingerprint_sha256,
    lodging_inventory_receipt_sha256,
    qunar_detail_seed_selection,
    qunar_trusted_flight_search_url,
    tongcheng_trusted_flight_search_url,
    trusted_search_url_contract,
)
from tripchord.providers.quote_normalizer import (
    PRODUCTION_VISIBLE_DOM_PARSER_VERSION,
    BrowserQuoteNormalizer,
    QuoteNormalizationCode,
    QuoteNormalizationStatus,
)

CAPTURED = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)


def query() -> BrowserSearchQuery:
    return BrowserSearchQuery(
        origin="HGH",
        destination="MLE",
        start_date=date(2026, 8, 23),
        end_date=date(2026, 8, 30),
        adults=2,
        rooms=1,
        origin_code="HGH",
        destination_code="MLE",
    )


def _browser_context(
    kind: BrowserVertical,
    provider: BrowserProvider,
    submitted_query: BrowserSearchQuery,
) -> dict[str, object]:
    submitted = submitted_query.model_dump(mode="json")
    confirmed = (
        {
            "origin": submitted["origin"],
            "destination": submitted["destination"],
            "start_date": submitted["start_date"],
            "end_date": submitted["end_date"],
            "adults": submitted["adults"],
        }
        if kind == BrowserVertical.FLIGHT
        else {
            "destination": submitted["destination"],
            "start_date": submitted["start_date"],
            "end_date": submitted["end_date"],
            "adults": submitted["adults"],
            "rooms": submitted["rooms"],
        }
    )
    return {
        "query": submitted,
        "driver": {
            "mode": "visible_form",
            "triggered": True,
            "provider": provider.value,
            "vertical": kind.value,
            "confirmed_query": confirmed,
            "readback_query": confirmed,
            "confirmation_scope": "confirmed_visible_search",
        },
        "price_text": "visible price",
        "visible_terms": ["tax included"],
        "extraction": "visible_dom",
    }


def browser_quote(
    kind: BrowserVertical,
    *,
    provider: BrowserProvider = BrowserProvider.CTRIP,
    amount: str = "4692",
    basis: QuotePriceBasis | None = None,
    taxes_included: bool | None = True,
    details_update: dict[str, object] | None = None,
    search_query: BrowserSearchQuery | None = None,
    page_url_override: str | None = None,
    title_override: str | None = None,
) -> BrowserQuote:
    domains = {
        BrowserProvider.CTRIP: "flights.ctrip.com",
        BrowserProvider.FLIGGY: "sjipiao.fliggy.com",
        BrowserProvider.QUNAR: "flight.qunar.com",
        BrowserProvider.TONGCHENG: "www.ly.com",
    }
    details = _browser_context(kind, provider, search_query or query())
    if kind == BrowserVertical.FLIGHT:
        flight_query = search_query or query()
        details.update(
            {
                "origin": "HGH",
                "destination": "MLE",
                "adults": 2,
                "outbound_departure_at": "2026-08-23T08:30:00+08:00",
                "outbound_arrival_at": "2026-08-23T18:35:00+05:00",
                "return_departure_at": "2026-08-30T10:45:00+05:00",
                "return_arrival_at": "2026-08-31T09:10:00+08:00",
                "checked_baggage_per_adult_kg": 0,
                "carrier_text": "fixture carrier",
                "connection_text": "one stop",
                "baggage_text": "no checked baggage",
                "workflow_kind": (
                    "combined_roundtrip_card"
                    if provider == BrowserProvider.QUNAR
                    else "staged_outbound_return"
                ),
                "combination_status": "round_trip_complete",
                "combination_id": f"{provider.value}-fixture-outbound-return",
                "journey_price_scope": "round_trip",
                "price_finality": "final_for_combination",
                "price_basis_evidence": "人均往返含税价 CNY 4692",
                "tax_evidence": "visible tax included",
                "availability": "available",
                "availability_evidence": "选择返程",
                "party_availability_status": (
                    "comparison_only"
                    if provider == BrowserProvider.FLIGGY
                    else "confirmed_for_party"
                ),
                "selection_evidence": (
                    "combined card contains both legs"
                    if provider == BrowserProvider.QUNAR
                    else "selected outbound summary matches return list"
                ),
                "action_trace": (
                    [{"action": "search"}]
                    if provider == BrowserProvider.QUNAR
                    else [{"action": "search"}, {"action": "select_outbound"}]
                ),
                "outbound_route_evidence": {
                    "direction": "outbound",
                    "source_scope": (
                        "combined_card_leg"
                        if provider == BrowserProvider.QUNAR
                        else "selected_outbound_summary"
                    ),
                    "expected_departure_code": "HGH",
                    "expected_arrival_code": "MLE",
                    "observed_departure_label": "HGH 杭州萧山",
                    "observed_arrival_label": "MLE 维拉纳",
                    "observed_departure_code": "HGH",
                    "observed_arrival_code": "MLE",
                    "departure_matches_requested": True,
                    "arrival_matches_requested": True,
                    "direction_order_confirmed": True,
                    "visible_evidence": "HGH 杭州萧山 → MLE 维拉纳",
                    "matches_expected": True,
                },
                "return_route_evidence": {
                    "direction": "return",
                    "source_scope": (
                        "combined_card_leg"
                        if provider == BrowserProvider.QUNAR
                        else "return_card"
                    ),
                    "expected_departure_code": "MLE",
                    "expected_arrival_code": "HGH",
                    "observed_departure_label": "MLE 维拉纳",
                    "observed_arrival_label": "HGH 杭州萧山",
                    "observed_departure_code": "MLE",
                    "observed_arrival_code": "HGH",
                    "departure_matches_requested": True,
                    "arrival_matches_requested": True,
                    "direction_order_confirmed": True,
                    "visible_evidence": "MLE 维拉纳 → HGH 杭州萧山",
                    "matches_expected": True,
                },
            }
        )
        if provider == BrowserProvider.QUNAR:
            details["party_price_comparison"] = {
                "schema": "tripchord.flight_party_comparison.v1",
                "verification": "server_owned_same_product",
                "provider": "qunar",
                "currency": "CNY",
                "start_date": flight_query.start_date.isoformat(),
                "end_date": flight_query.end_date.isoformat(),
                "origin_code": flight_query.origin_code,
                "destination_code": flight_query.destination_code,
                "same_product_id": "fixture-product",
                "query_hash": "q" * 64,
                "one_adult": {
                    "adults": 1,
                    "amount": 400000,
                    "same_product_id": "fixture-product",
                    "query_hash": "q" * 64,
                },
                "two_adults": {
                    "adults": 2,
                    "amount": 624400,
                    "same_product_id": "fixture-product",
                    "query_hash": "q" * 64,
                },
                "two_adult_amount": 624400,
            }
    else:
        details.update(
            {
                "destination": "MLE",
                "check_in": "2026-08-23",
                "check_out": "2026-08-30",
                "adults": 2,
                "rooms": 1,
                "area": "destination_island",
                "area_source": "visible_label",
                "breakfast_included": False,
                "room_text": "fixture room",
                "area_text": "destination island",
                "breakfast_text": "not included",
                "cancellation_text": "free cancellation",
                "transfer_text": "optional",
                "availability": "available",
            }
        )
    details.update(details_update or {})
    page_url = page_url_override or f"https://{domains[provider]}/search/results"
    selected_basis = (
        basis
        if basis is not None
        else (
            QuotePriceBasis.PER_PERSON
            if kind == BrowserVertical.FLIGHT
            else QuotePriceBasis.PER_NIGHT
        )
    )
    title = title_override or (
        "HGH-MLE round trip" if kind == BrowserVertical.FLIGHT else "Island Stay"
    )
    decimal_amount = Decimal(amount)
    amount_text = format(decimal_amount, "f")
    if "." in amount_text:
        amount_text = amount_text.rstrip("0").rstrip(".")
    if kind == BrowserVertical.FLIGHT:
        if selected_basis == QuotePriceBasis.PER_PERSON:
            visible_price = f"人均往返含税价 CNY {amount_text}"
        elif selected_basis == QuotePriceBasis.TOTAL_PARTY:
            visible_price = f"{details['adults']}名成人往返总价 CNY {amount_text}"
        else:
            visible_price = f"含税价 CNY {amount_text}"
        details["price_text"] = visible_price
        details["price_basis_evidence"] = visible_price
    payload = {
        "amount": amount_text,
        "currency": "CNY",
        "details": details,
        "kind": kind.value,
        "page_url": page_url,
        "price_basis": selected_basis.value,
        "provider": provider.value,
        "taxes_included": taxes_included,
        "title": title,
    }
    visible_evidence = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BrowserQuote(
        provider=provider,
        kind=kind,
        page_url=page_url,
        captured_at=CAPTURED,
        parser_version=PRODUCTION_VISIBLE_DOM_PARSER_VERSION,
        visible_evidence=visible_evidence,
        evidence_sha256=hashlib.sha256(visible_evidence.encode()).hexdigest(),
        currency="CNY",
        amount=decimal_amount,
        price_basis=selected_basis,
        taxes_included=taxes_included,
        title=title,
        details=details,
    )


def _trusted_flight_query(provider: BrowserProvider) -> BrowserSearchQuery:
    base = query()
    builder = {
        BrowserProvider.CTRIP: ctrip_trusted_flight_search_url,
        BrowserProvider.FLIGGY: fliggy_trusted_flight_search_url,
        BrowserProvider.QUNAR: qunar_trusted_flight_search_url,
        BrowserProvider.TONGCHENG: tongcheng_trusted_flight_search_url,
    }[provider]
    return base.model_copy(update={"search_url": builder(base)})


def _trusted_flight_driver(
    provider: BrowserProvider,
    search_query: BrowserSearchQuery,
) -> dict[str, object]:
    contract = trusted_search_url_contract(
        provider,
        BrowserVertical.FLIGHT,
        search_query,
    )
    assert contract is not None
    snapshot = search_query.model_dump(mode="json")
    confirmed = {
        "origin": snapshot["origin"],
        "destination": snapshot["destination"],
        "start_date": snapshot["start_date"],
        "end_date": snapshot["end_date"],
        "adults": snapshot["adults"],
    }
    readback = dict(contract.url_readback)
    return {
        "mode": "search_url",
        "triggered": True,
        "provider": provider.value,
        "vertical": "flight",
        "confirmed_query": confirmed,
        "readback_query": readback,
        "confirmation_scope": "trusted_exact_search_url",
        "url_confirmed_fields": list(readback),
        "party_availability_confirmed": contract.party_availability_confirmed,
        "pricing_context": contract.pricing_context,
    }


def _trusted_flight_quote(
    provider: BrowserProvider,
    search_query: BrowserSearchQuery,
    *,
    basis: QuotePriceBasis = QuotePriceBasis.PER_PERSON,
    driver_update: dict[str, object] | None = None,
) -> BrowserQuote:
    driver = _trusted_flight_driver(provider, search_query)
    driver.update(driver_update or {})
    return browser_quote(
        BrowserVertical.FLIGHT,
        provider=provider,
        basis=basis,
        details_update={"driver": driver},
        search_query=search_query,
    )


def _reseal_model_copy(
    source: BrowserQuote,
    *,
    details_update: dict[str, object],
    details_remove: tuple[str, ...] = (),
) -> BrowserQuote:
    details = json.loads(json.dumps(source.details))
    details.update(details_update)
    for key in details_remove:
        details.pop(key, None)
    amount_text = format(source.amount, "f")
    if "." in amount_text:
        amount_text = amount_text.rstrip("0").rstrip(".")
    payload = {
        "amount": amount_text,
        "currency": source.currency,
        "details": details,
        "kind": source.kind.value,
        "page_url": source.page_url,
        "price_basis": source.price_basis.value,
        "provider": source.provider.value,
        "taxes_included": source.taxes_included,
        "title": source.title,
    }
    visible_evidence = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return source.model_copy(
        update={
            "details": details,
            "visible_evidence": visible_evidence,
            "evidence_sha256": hashlib.sha256(visible_evidence.encode()).hexdigest(),
        }
    )


def _round_trip_window_transfers(
    start: str = "2026-08-23",
    end: str = "2026-08-30",
) -> list[dict[str, object]]:
    evidence = (
        "往返接送：马累机场 ↔ 胡鲁马累；24小时服务（UTC+05:00）；"
        "单程20分钟；需提前预约；含税总价 CNY 108（2名成人）"
    )
    common: dict[str, object] = {
        "currency": "CNY",
        "taxes_included": True,
        "tax_evidence": evidence,
        "price_basis": "total_party",
        "price_scope": "round_trip",
        "amount": "108",
        "price_evidence": evidence,
        "price_contract_key": "terminal-27-round-trip",
        "purchase_scope": "hotel_bound",
        "purchase_scope_evidence": evidence,
        "direction_evidence": evidence,
        "schedule_mode": "service_window",
        "duration_minutes": 20,
        "schedule_evidence": evidence,
        "operates_24_hours": True,
        "requires_reservation": True,
        "availability": "available",
        "evidence_text": evidence,
        "detail_url": "https://hotels.ctrip.com/hotels/detail/terminal-27",
        "evidence_sha256": "d" * 64,
    }
    return [
        {
            **common,
            "origin_area": "airport",
            "destination_area": "airport_island",
            "service_date": start,
            "service_window_start_at": f"{start}T00:00:00+05:00",
            "service_window_end_at": f"{start}T23:59:00+05:00",
        },
        {
            **common,
            "origin_area": "airport_island",
            "destination_area": "airport",
            "service_date": end,
            "service_window_start_at": f"{end}T00:00:00+05:00",
            "service_window_end_at": f"{end}T23:59:00+05:00",
        },
    ]


def test_flight_per_person_becomes_exact_integer_party_total() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(BrowserVertical.FLIGHT),
        query(),
    )

    assert result.status == QuoteNormalizationStatus.USABLE
    assert isinstance(result.quote, NormalizedFlightQuote)
    assert result.quote.total_for_party_cents == 938_400
    assert result.quote.adults == 2
    assert result.quote.outbound_arrive_at.isoformat() == "2026-08-23T18:35:00+05:00"
    assert result.quote.expires_at > result.quote.captured_at
    assert result.quote.checked_baggage_per_adult_kg == 0
    assert result.quote.evidence_refs[0].startswith("browser:ctrip:sha256:")
    assert len(result.quote.evidence_refs[0].rsplit(":", maxsplit=1)[-1]) == 64


def test_normalizer_preserves_explicit_provider_flight_identity_and_terms() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.FLIGHT,
            details_update={
                "provider_itinerary_id": "itinerary-42",
                "provider_offer_id": "offer-99",
                "outbound_flight_numbers": ["CX123", "CX601"],
                "return_flight_numbers": ["CX602", "CX124"],
                "cabin_class": "economy",
                "fare_basis_codes": ["YLOW", "YRETURN"],
                "fare_rule_summary": "changes with fee; non-refundable",
            },
        ),
        query(),
    )

    assert isinstance(result.quote, NormalizedFlightQuote)
    assert result.quote.provider_itinerary_id == "itinerary-42"
    assert result.quote.provider_offer_id == "offer-99"
    assert result.quote.outbound_flight_numbers == ("CX123", "CX601")
    assert result.quote.return_flight_numbers == ("CX602", "CX124")
    assert result.quote.cabin_class == "economy"
    assert result.quote.fare_basis_codes == ("YLOW", "YRETURN")
    assert result.quote.fare_rule_summary == "changes with fee; non-refundable"


def test_normalizer_preserves_explicit_lodging_room_rate_and_payment_identity() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "property_id": "hotel-100",
                "room_id": "room-king",
                "rate_plan_id": "rate-flex",
                "provider_offer_id": "offer-123",
                "room_text": "Deluxe King Room",
                "bed_text": "1 king bed",
                "cancellation_text": "free cancellation until 18:00",
                "payment_text": "pay online",
            },
        ),
        query(),
    )

    assert isinstance(result.quote, NormalizedLodgingQuote)
    assert result.quote.provider_property_id == "hotel-100"
    assert result.quote.provider_room_id == "room-king"
    assert result.quote.provider_rate_plan_id == "rate-flex"
    assert result.quote.provider_offer_id == "offer-123"
    assert result.quote.room_name == "Deluxe King Room"
    assert result.quote.bed_type == "1 king bed"
    assert result.quote.cancellation_policy == "free cancellation until 18:00"
    assert result.quote.payment_policy == "pay online"


def test_flight_route_accepts_visible_velana_chinese_transliteration() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    outbound = dict(source.details["outbound_route_evidence"])
    outbound.update(
        observed_arrival_label="韦拉纳",
        observed_arrival_code=None,
        visible_evidence="HGH 杭州萧山 → 韦拉纳国际机场",
    )
    accepted = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={"outbound_route_evidence": outbound},
        ),
        query(),
    )

    assert accepted.status == QuoteNormalizationStatus.USABLE


def test_lodging_destination_accepts_ctrip_selected_hulhumale_island_label() -> None:
    normalizer = BrowserQuoteNormalizer()

    assert normalizer._audited_lodging_destination_alias_matches(
        "胡鲁马累岛",
        {"options": {"expected_lodging_place_key": "hulhumale"}},
    )


def test_tongcheng_visible_form_flight_requires_exact_readback_and_normalizes() -> None:
    source = browser_quote(
        BrowserVertical.FLIGHT,
        provider=BrowserProvider.TONGCHENG,
        page_url_override="https://www.ly.com/eliflight/book1.html?fixture=1",
    )

    accepted = BrowserQuoteNormalizer().normalize(source, query())
    wrong_readback = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={
                "driver": {
                    **source.details["driver"],
                    "readback_query": {
                        **source.details["driver"]["readback_query"],
                        "adults": 1,
                    },
                }
            },
        ),
        query(),
    )

    assert accepted.status == QuoteNormalizationStatus.USABLE
    assert isinstance(accepted.quote, NormalizedFlightQuote)
    assert accepted.quote.provider == "tongcheng"
    assert wrong_readback.status == QuoteNormalizationStatus.REJECTED
    assert wrong_readback.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH


def test_tongcheng_total_label_without_party_comparison_is_display_only() -> None:
    exact_query = _trusted_flight_query(BrowserProvider.TONGCHENG)
    source = browser_quote(
        BrowserVertical.FLIGHT,
        provider=BrowserProvider.TONGCHENG,
        amount="4103",
        basis=QuotePriceBasis.TOTAL_PARTY,
        search_query=exact_query,
        details_update={
            "driver": _trusted_flight_driver(BrowserProvider.TONGCHENG, exact_query),
        },
    )
    total_labelled = _reseal_model_copy(
        source,
        details_update={
            "price_text": "¥4103含税总价",
            "price_basis_evidence": "¥4103含税总价",
            "price_basis_source": "visible_total_label_unverified_party_v1",
        },
    )

    normalized = BrowserQuoteNormalizer().normalize(total_labelled, exact_query)

    assert normalized.status == QuoteNormalizationStatus.USABLE
    assert normalized.quote is not None
    assert normalized.quote.party_total_known is False
    assert normalized.quote.total_for_party_cents is None
    assert normalized.quote.price_basis == "comparison_only"


def test_qunar_exact_party_result_card_is_usable_without_booking_control() -> None:
    exact_query = query().model_copy(
        update={"search_url": qunar_trusted_flight_search_url(query())}
    )
    quote = browser_quote(
        BrowserVertical.FLIGHT,
        provider=BrowserProvider.QUNAR,
        amount="6244",
        basis=QuotePriceBasis.TOTAL_PARTY,
        search_query=exact_query,
        details_update={
            "driver": _trusted_flight_driver(BrowserProvider.QUNAR, exact_query),
            "availability_evidence": (
                "exact_trusted_url_party_context: HGH→MLE, "
                "2026-08-23→2026-08-30, 2名成人；"
                "visible_result_card；inventory_not_locked"
            ),
            "price_text": "2名成人 含税总价 ¥6244",
            "price_basis_evidence": "2名成人 含税总价 ¥6244",
        },
    )

    accepted = BrowserQuoteNormalizer().normalize(quote, exact_query)
    assert accepted.status == QuoteNormalizationStatus.USABLE
    assert accepted.quote is not None
    assert accepted.quote.total_for_party_cents == 624_400

    weak = browser_quote(
        BrowserVertical.FLIGHT,
        provider=BrowserProvider.QUNAR,
        amount="6244",
        basis=QuotePriceBasis.TOTAL_PARTY,
        search_query=exact_query,
        details_update={
            "driver": _trusted_flight_driver(BrowserProvider.QUNAR, exact_query),
            "party_price_comparison": None,
            "availability_evidence": (
                "exact_trusted_url_party_context: HGH→MLE, "
                "2026-08-23→2026-08-30, 2名成人；"
                "visible_result_card；inventory_not_locked"
            ),
            "price_text": "2名成人 含税总价 ¥6244",
            "price_basis_evidence": "2名成人 含税总价 ¥6244",
        },
    )
    observed = BrowserQuoteNormalizer().normalize(weak, exact_query)
    assert observed.status == QuoteNormalizationStatus.USABLE
    assert observed.quote is not None
    assert observed.quote.party_total_known is False
    assert observed.quote.price_basis == "comparison_only"
    assert observed.quote.total_for_party_cents is None
    assert observed.quote.display_amount_cents == 624_400


def test_normalizer_rejects_outbound_preview_even_if_model_validation_was_bypassed() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    preview = _reseal_model_copy(
        source,
        details_update={"combination_status": "outbound_preview"},
    )

    result = BrowserQuoteNormalizer().normalize(preview, query())

    assert result.status == QuoteNormalizationStatus.REJECTED
    assert result.issues[0].code == QuoteNormalizationCode.INCOMPLETE_ROUND_TRIP
    assert result.issues[0].field == "combination_status"


def test_normalizer_requires_visible_route_evidence_for_both_directions() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    missing = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={},
            details_remove=("outbound_route_evidence",),
        ),
        query(),
    )
    wrong_return = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={
                "return_route_evidence": {
                    **source.details["return_route_evidence"],
                    "expected_arrival_code": "PEK",
                    "observed_arrival_label": "PEK 北京",
                    "visible_evidence": "MLE 维拉纳 → PEK 北京",
                }
            },
        ),
        query(),
    )
    query_only = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={
                "outbound_route_evidence": {
                    **source.details["outbound_route_evidence"],
                    "source_scope": "query",
                }
            },
        ),
        query(),
    )

    assert missing.status == QuoteNormalizationStatus.REJECTED
    assert missing.issues[0].field == "outbound_route_evidence"
    assert wrong_return.status == QuoteNormalizationStatus.REJECTED
    assert wrong_return.issues[0].field == "return_route_evidence"
    assert query_only.status == QuoteNormalizationStatus.REJECTED
    assert query_only.issues[0].field == "outbound_route_evidence.source_scope"


def test_normalizer_rejects_ambiguous_nonfinal_and_currencyless_flight_prices() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    cases = (
        "往返总价 含税 CNY 4692",
        "预估往返价 CNY 4692 /人",
        "人均往返含税价 4692",
    )
    for price_text in cases:
        rejected = BrowserQuoteNormalizer().normalize(
            _reseal_model_copy(
                source,
                details_update={
                    "price_text": price_text,
                    "price_basis_evidence": price_text,
                },
            ),
            query(),
        )
        assert rejected.status == QuoteNormalizationStatus.REJECTED
        assert rejected.issues[0].field in {"price_text", "price_basis"}


def test_normalizer_rejects_tax_conflict_and_missing_availability() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    tax_conflict = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={"tax_evidence": "含税，但部分税费另付"},
        ),
        query(),
    )
    missing_availability = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={},
            details_remove=("availability",),
        ),
        query(),
    )
    no_control = BrowserQuoteNormalizer().normalize(
        _reseal_model_copy(
            source,
            details_update={"availability_evidence": "价格已显示"},
        ),
        query(),
    )

    assert tax_conflict.status == QuoteNormalizationStatus.REJECTED
    assert tax_conflict.issues[0].code == QuoteNormalizationCode.TAXES_INCOMPLETE
    assert missing_availability.status == QuoteNormalizationStatus.REJECTED
    assert missing_availability.issues[0].field == "availability"
    assert no_control.status == QuoteNormalizationStatus.REJECTED
    assert no_control.issues[0].field == "availability_evidence"


def test_normalizer_rejects_transaction_action_even_if_model_validation_was_bypassed() -> None:
    source = browser_quote(BrowserVertical.FLIGHT)
    unsafe = _reseal_model_copy(
        source,
        details_update={
            "action_trace": [
                {"action": "search"},
                {"action": "select_outbound"},
                {"action": "payment"},
            ]
        },
    )

    result = BrowserQuoteNormalizer().normalize(unsafe, query())

    assert result.status == QuoteNormalizationStatus.REJECTED
    assert result.issues[0].code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
    assert result.issues[0].field == "action_trace"


def test_trusted_flight_urls_preserve_provider_specific_party_evidence() -> None:
    ctrip_query = _trusted_flight_query(BrowserProvider.CTRIP)
    fliggy_query = _trusted_flight_query(BrowserProvider.FLIGGY)
    qunar_query = _trusted_flight_query(BrowserProvider.QUNAR)

    ctrip = BrowserQuoteNormalizer().normalize(
        _trusted_flight_quote(BrowserProvider.CTRIP, ctrip_query),
        ctrip_query,
    )
    fliggy = BrowserQuoteNormalizer().normalize(
        _trusted_flight_quote(BrowserProvider.FLIGGY, fliggy_query),
        fliggy_query,
    )
    qunar = BrowserQuoteNormalizer().normalize(
        _trusted_flight_quote(BrowserProvider.QUNAR, qunar_query),
        qunar_query,
    )

    assert ctrip.status == QuoteNormalizationStatus.USABLE
    assert isinstance(ctrip.quote, NormalizedFlightQuote)
    assert ctrip.quote.party_availability_confirmed
    assert ctrip.quote.total_for_party_cents == 938_400
    assert fliggy.status == QuoteNormalizationStatus.USABLE
    assert isinstance(fliggy.quote, NormalizedFlightQuote)
    assert not fliggy.quote.party_availability_confirmed
    assert fliggy.quote.total_for_party_cents == 938_400
    assert qunar.status == QuoteNormalizationStatus.USABLE
    assert isinstance(qunar.quote, NormalizedFlightQuote)
    assert qunar.quote.party_availability_confirmed
    # The verified same-product comparison is authoritative for Qunar's
    # requested-party total; it is not synthesized from the visible fare.
    assert qunar.quote.total_for_party_cents == 624_400


def test_trusted_flight_urls_reject_host_path_route_date_count_and_extra_query_tampering() -> None:
    cases: list[tuple[BrowserProvider, str]] = []
    ctrip_query = _trusted_flight_query(BrowserProvider.CTRIP)
    assert ctrip_query.search_url is not None
    cases.extend(
        (
            (BrowserProvider.CTRIP, ctrip_query.search_url.replace("ctrip.com", "evil.test")),
            (
                BrowserProvider.CTRIP,
                ctrip_query.search_url.replace("/international/search/", "/order/"),
            ),
            (
                BrowserProvider.CTRIP,
                ctrip_query.search_url.replace("round-hgh-mle", "round-hgh-nrt"),
            ),
            (BrowserProvider.CTRIP, ctrip_query.search_url.replace("2026-08-30", "2026-08-29")),
            (BrowserProvider.CTRIP, ctrip_query.search_url.replace("adult=2", "adult=1")),
            (BrowserProvider.CTRIP, f"{ctrip_query.search_url}&coupon=secret"),
        )
    )
    fliggy_query = _trusted_flight_query(BrowserProvider.FLIGGY)
    assert fliggy_query.search_url is not None
    cases.extend(
        (
            (
                BrowserProvider.FLIGGY,
                fliggy_query.search_url.replace("sijipiao.fliggy.com", "sjipiao.fliggy.com"),
            ),
            (
                BrowserProvider.FLIGGY,
                fliggy_query.search_url.replace(
                    "/ie/flight_search_result.htm",
                    "/flight_search_result.htm",
                ),
            ),
            (BrowserProvider.FLIGGY, fliggy_query.search_url.replace("arrCity=MLE", "arrCity=NRT")),
            (
                BrowserProvider.FLIGGY,
                fliggy_query.search_url.replace("arrDate=2026-08-30", "arrDate=2026-08-29"),
            ),
            (BrowserProvider.FLIGGY, f"{fliggy_query.search_url}&adult=2"),
            (BrowserProvider.FLIGGY, f"{fliggy_query.search_url}&sessionToken=secret"),
        )
    )
    qunar_query = _trusted_flight_query(BrowserProvider.QUNAR)
    assert qunar_query.search_url is not None
    cases.extend(
        (
            (
                BrowserProvider.QUNAR,
                qunar_query.search_url.replace("flight.qunar.com", "evil.test"),
            ),
            (
                BrowserProvider.QUNAR,
                qunar_query.search_url.replace(
                    "/twell/flight/Search.jsp",
                    "/site/interroundtrip_compare.htm",
                ),
            ),
            (
                BrowserProvider.QUNAR,
                qunar_query.search_url.replace(
                    "%E6%9D%AD%E5%B7%9E",
                    "%E4%B8%8A%E6%B5%B7",
                ),
            ),
            (
                BrowserProvider.QUNAR,
                qunar_query.search_url.replace("toDate=2026-08-30", "toDate=2026-08-29"),
            ),
            (BrowserProvider.QUNAR, qunar_query.search_url.replace("adultNum=2", "adultNum=1")),
            (BrowserProvider.QUNAR, f"{qunar_query.search_url}&tracking=unexpected"),
            (
                BrowserProvider.QUNAR,
                qunar_query.search_url.replace(
                    "?from=flight_int_search&showTotalPr=0",
                    "?showTotalPr=0&from=flight_int_search",
                ),
            ),
        )
    )

    for provider, tampered_url in cases:
        canonical_query = {
            BrowserProvider.CTRIP: ctrip_query,
            BrowserProvider.FLIGGY: fliggy_query,
            BrowserProvider.QUNAR: qunar_query,
        }[provider]
        tampered_query = canonical_query.model_copy(update={"search_url": tampered_url})
        quote = browser_quote(
            BrowserVertical.FLIGHT,
            provider=provider,
            search_query=tampered_query,
            details_update={
                "driver": _trusted_flight_driver(provider, canonical_query),
            },
        )
        result = BrowserQuoteNormalizer().normalize(quote, tampered_query)
        assert result.status == QuoteNormalizationStatus.REJECTED
        assert result.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
        assert result.issues[0].field == "query.search_url"


def test_fliggy_trusted_url_is_per_person_pricing_context_not_party_availability() -> None:
    base = query().model_copy(update={"adults": 9})
    fliggy_query = base.model_copy(
        update={"search_url": fliggy_trusted_flight_search_url(base)}
    )
    driver = _trusted_flight_driver(BrowserProvider.FLIGGY, fliggy_query)
    quote = browser_quote(
        BrowserVertical.FLIGHT,
        provider=BrowserProvider.FLIGGY,
        search_query=fliggy_query,
        details_update={"driver": driver, "adults": 9},
    )
    normalized = BrowserQuoteNormalizer().normalize(quote, fliggy_query)
    false_party_claim = BrowserQuoteNormalizer().normalize(
        _trusted_flight_quote(
            BrowserProvider.FLIGGY,
            _trusted_flight_query(BrowserProvider.FLIGGY),
            driver_update={"party_availability_confirmed": True},
        ),
        _trusted_flight_query(BrowserProvider.FLIGGY),
    )
    total_party = BrowserQuoteNormalizer().normalize(
        _trusted_flight_quote(
            BrowserProvider.FLIGGY,
            _trusted_flight_query(BrowserProvider.FLIGGY),
            basis=QuotePriceBasis.TOTAL_PARTY,
        ),
        _trusted_flight_query(BrowserProvider.FLIGGY),
    )

    assert normalized.status == QuoteNormalizationStatus.USABLE
    assert isinstance(normalized.quote, NormalizedFlightQuote)
    assert normalized.quote.total_for_party_cents == 4_222_800
    assert not normalized.quote.party_availability_confirmed
    assert false_party_claim.status == QuoteNormalizationStatus.REJECTED
    assert false_party_claim.issues[0].field == "driver"
    assert total_party.status == QuoteNormalizationStatus.REJECTED
    assert total_party.issues[0].code == QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS


def test_gate_rejects_fixture_scope_parser_markers_and_tampered_evidence() -> None:
    required = {
        "origin": "HGH",
        "destination": "MLE",
        "start_date": "2026-08-23",
        "end_date": "2026-08-30",
        "adults": 2,
    }
    fixture_scope = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.FLIGHT,
            details_update={
                "driver": {
                    "mode": "visible_form",
                    "triggered": True,
                    "provider": "ctrip",
                    "vertical": "flight",
                    "confirmed_query": required,
                    "readback_query": required,
                    "confirmation_scope": "fixture",
                }
            },
        ),
        query(),
    )
    scripted = BrowserQuoteNormalizer().normalize(
        browser_quote(BrowserVertical.FLIGHT).model_copy(
            update={"parser_version": "scripted-visible-dom-v3"}
        ),
        query(),
    )
    tampered = BrowserQuoteNormalizer().normalize(
        browser_quote(BrowserVertical.FLIGHT).model_copy(update={"amount": Decimal("1")}),
        query(),
    )

    assert fixture_scope.status == QuoteNormalizationStatus.REJECTED
    assert fixture_scope.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
    assert scripted.status == QuoteNormalizationStatus.REJECTED
    assert scripted.issues[0].field == "parser_version"
    assert tampered.status == QuoteNormalizationStatus.REJECTED
    assert tampered.issues[0].field == "visible_evidence"


def test_normalizer_parser_version_matches_companion_and_rejects_all_other_markers() -> None:
    parser_source = (
        Path(__file__).resolve().parents[3]
        / "apps"
        / "browser-companion"
        / "src"
        / "parser.js"
    ).read_text()
    assert (
        f'const PARSER_VERSION = "{PRODUCTION_VISIBLE_DOM_PARSER_VERSION}";'
        in parser_source
    )
    # This is deliberately a cross-layer source contract, not a claim that a
    # production DOM was extracted. Real DOM success remains a live Done-Gate.
    for protocol_literal in (
        'combination_status: "round_trip_complete"',
        'journey_price_scope: "round_trip"',
        'price_finality: "final_for_combination"',
        'workflow_kind: workflowKind',
        'action_trace: actionTrace',
    ):
        assert protocol_literal in parser_source

    for marker in (
        "tripchord-visible-dom-v1",
        "tripchord-visible-dom-v2",
        "tripchord-visible-dom-v4",
        "unknown-parser",
    ):
        result = BrowserQuoteNormalizer().normalize(
            browser_quote(BrowserVertical.FLIGHT).model_copy(
                update={"parser_version": marker}
            ),
            query(),
        )
        assert result.status == QuoteNormalizationStatus.REJECTED
        assert result.issues[0].field == "parser_version"


def test_gate_requires_exact_visible_adults_rooms_dates_and_query_snapshot() -> None:
    missing_rooms = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "driver": {
                    "mode": "visible_form",
                    "triggered": True,
                    "provider": "ctrip",
                    "vertical": "lodging",
                    "confirmed_query": {
                        "destination": "MLE",
                        "start_date": "2026-08-23",
                        "end_date": "2026-08-30",
                        "adults": 2,
                    },
                    "readback_query": {
                        "destination": "MLE",
                        "start_date": "2026-08-23",
                        "end_date": "2026-08-30",
                        "adults": 2,
                    },
                    "confirmation_scope": "confirmed_visible_search",
                }
            },
        ),
        query(),
    )
    wrong_snapshot = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "query": {
                    **query().model_dump(mode="json"),
                    "rooms": 2,
                }
            },
        ),
        query(),
    )

    assert missing_rooms.status == QuoteNormalizationStatus.REJECTED
    assert missing_rooms.issues[0].field == "driver.confirmed_query"
    assert wrong_snapshot.status == QuoteNormalizationStatus.REJECTED
    assert wrong_snapshot.issues[0].field == "query"


def test_query_snapshot_omits_only_audited_planner_metadata() -> None:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    full_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "expected_lodging_place_key": "maafushi",
                "expected_package_area": "destination_island",
                "segment": "full",
                "__tripchord_allow_recent_quote_reuse": True,
                "__tripchord_reuse_exact_result_tab": True,
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
                "stay_plan_candidate_set": {
                    "schema_version": "test-only-planner-metadata"
                },
            },
        }
    )
    browser_snapshot = full_query.model_dump(mode="json")
    browser_snapshot["options"] = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    exact = browser_quote(
        BrowserVertical.LODGING,
        search_query=full_query,
        details_update={
            "query": browser_snapshot,
            "destination": "Maafushi",
            "area_text": "Maafushi",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )

    usable = BrowserQuoteNormalizer().normalize(exact, full_query)
    assert usable.status == QuoteNormalizationStatus.USABLE

    unsupported_query = full_query.model_copy(
        update={"options": {**full_query.options, "unattested_filter": "value"}}
    )
    rejected = BrowserQuoteNormalizer().normalize(exact, unsupported_query)
    assert rejected.status == QuoteNormalizationStatus.REJECTED
    assert rejected.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
    assert rejected.issues[0].field == "query"


def test_read_only_lodging_detail_receipts_are_provider_bound() -> None:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    exact_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "expected_lodging_place_key": "maafushi",
                "expected_package_area": "destination_island",
                "segment": "full",
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
            },
        }
    )
    safe_snapshot = exact_query.model_dump(mode="json")
    safe_snapshot["options"] = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    base_driver = _browser_context(
        BrowserVertical.LODGING,
        BrowserProvider.CTRIP,
        exact_query,
    )["driver"]
    assert isinstance(base_driver, dict)
    ctrip_driver = {
        **base_driver,
        "mode": "captured_read_only_detail",
        "detail_capture": {
            "source": "ctrip_visible_exact_view_details",
            "hotel_id": "131576087",
            "popup_opened": False,
            "preview_place_match": "exact",
        },
    }
    ctrip = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.CTRIP,
        search_query=exact_query,
        page_url_override=(
            "https://hotels.ctrip.com/hotels/detail/"
            "?hotelId=131576087&checkIn=2026-08-23&checkOut=2026-08-30"
        ),
        details_update={
            "query": safe_snapshot,
            "driver": ctrip_driver,
            "destination": "Maafushi",
            "area_text": "Maafushi",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )
    usable = BrowserQuoteNormalizer().normalize(ctrip, exact_query)
    assert usable.status == QuoteNormalizationStatus.USABLE

    localized_driver = json.loads(json.dumps(ctrip_driver))
    localized_driver["readback_query"]["destination"] = "马富施"
    localized_driver["readback_query"]["start_date"] = "2026年8月23日(星期日)"
    localized_driver["readback_query"]["end_date"] = "2026年8月30日(星期日)"
    localized = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.CTRIP,
        search_query=exact_query,
        page_url_override=ctrip.page_url,
        details_update={
            "query": safe_snapshot,
            "driver": localized_driver,
            "destination": "Maafushi",
            "area_text": "马富施",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )
    localized_result = BrowserQuoteNormalizer().normalize(localized, exact_query)
    assert localized_result.status == QuoteNormalizationStatus.USABLE

    wrong_place_driver = json.loads(json.dumps(ctrip_driver))
    wrong_place_driver["readback_query"]["destination"] = "胡鲁马累"
    wrong_place = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.CTRIP,
        search_query=exact_query,
        page_url_override=ctrip.page_url,
        details_update={
            "query": safe_snapshot,
            "driver": wrong_place_driver,
            "destination": "Maafushi",
            "area_text": "胡鲁马累",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "hulhumale",
            "lodging_place_matches_expected": False,
        },
    )
    wrong_place_result = BrowserQuoteNormalizer().normalize(wrong_place, exact_query)
    assert wrong_place_result.status == QuoteNormalizationStatus.REJECTED
    assert wrong_place_result.issues[0].field == "driver.readback_query.destination"

    unsafe_driver = json.loads(json.dumps(ctrip_driver))
    unsafe_driver["detail_capture"]["popup_opened"] = True
    unsafe = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.CTRIP,
        search_query=exact_query,
        page_url_override=ctrip.page_url,
        details_update={
            "query": safe_snapshot,
            "driver": unsafe_driver,
            "destination": "Maafushi",
            "area_text": "Maafushi",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )
    rejected = BrowserQuoteNormalizer().normalize(unsafe, exact_query)
    assert rejected.status == QuoteNormalizationStatus.REJECTED
    assert rejected.issues[0].code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
    assert rejected.issues[0].field == "driver.detail_capture"


def _qunar_confirmed_empty_parent_receipt(
    *,
    options: dict[str, object],
    start_date: str,
    end_date: str,
    quoted_property_id: str,
) -> tuple[dict[str, object], str]:
    def child(captured_at: str) -> dict[str, object]:
        return {
            "schema_version": "tripchord-lodging-inventory-receipt-v1",
            "parser_version": "tripchord-visible-dom-v3",
            "provider": "qunar",
            "state": "confirmed_empty",
            "confirmed_query": {
                "destination": "Maafushi",
                "start_date": start_date,
                "end_date": end_date,
                "adults": 2,
                "rooms": 1,
                "options": options,
            },
            "confirmation_scope": "confirmed_visible_search",
            "scan_limit": 12,
            "scanned_count": 0,
            "candidate_summaries": [],
            "explicit_empty_evidence": {
                "contract_version": "qunar-visible-zero-inventory-v1",
                "result_count_text": "共 0 家酒店满足条件",
                "empty_message": "很抱歉，没有找到相关的酒店",
            },
            "provider_pending_evidence": None,
            "page_url": "https://hotel.qunar.com/city/i-ka_maafushi/",
            "captured_at": captured_at,
        }

    first = child("2026-08-04T12:00:00Z")
    second = child("2026-08-04T12:00:02Z")
    query_fingerprint = lodging_inventory_query_fingerprint_sha256(
        first["confirmed_query"]
    )
    confirmed_query = LodgingInventoryConfirmedQuery.model_validate(
        first["confirmed_query"]
    )
    seed_offset, target_property_ids = qunar_detail_seed_selection(confirmed_query)
    assert quoted_property_id in target_property_ids
    lineage = {
        "schema_version": "tripchord-browser-lineage-hash-v1",
        "isolation_scope": "companion_owned_unfocused_normal_window_active_tab",
        "runtime_lineage_sha256": "1" * 64,
        "window_lineage_sha256": "2" * 64,
        "tab_lineage_sha256": "3" * 64,
    }
    chain = {
        "schema_version": "tripchord-qunar-empty-observation-chain-v1",
        "query_fingerprint_sha256": query_fingerprint,
        "observations": [
            {
                "ordinal": ordinal,
                "receipt": receipt,
                "receipt_sha256": lodging_inventory_receipt_sha256(receipt),
                "captured_at": receipt["captured_at"],
                "query_fingerprint_sha256": query_fingerprint,
                "lineage": lineage,
            }
            for ordinal, receipt in enumerate((first, second), start=1)
        ],
        "observed_interval_ms": 2_000,
        "detail_fallback": {
            "contract_version": "tripchord-qunar-detail-fallback-summary-v2",
            "attempted": True,
            "target_limit": 2,
            "seed_selection_policy": "query-fingerprint-rotation-v1",
            "seed_selection_offset": seed_offset,
            "target_property_ids": list(target_property_ids),
            "observed_results": [
                {
                    "property_id": property_id,
                    "state": (
                        "succeeded"
                        if property_id == quoted_property_id
                        else "failed"
                    ),
                    "verified_quote_count": int(property_id == quoted_property_id),
                }
                for property_id in target_property_ids
            ],
            "verified_quote_count": 1,
        },
        "sealed_at": "2026-08-04T12:00:03Z",
    }
    parent = {
        **second,
        "schema_version": "tripchord-lodging-inventory-receipt-v2",
        "observation_chain": chain,
    }
    return parent, lodging_inventory_receipt_sha256(parent)


def _qunar_pending_receipt(
    *, options: dict[str, object], start_date: str, end_date: str
) -> tuple[dict[str, object], str]:
    receipt = {
        "schema_version": "tripchord-lodging-inventory-receipt-v1",
        "parser_version": "tripchord-visible-dom-v3",
        "provider": "qunar",
        "state": "bounded_provider_pending",
        "confirmed_query": {
            "destination": "Maafushi",
            "start_date": start_date,
            "end_date": end_date,
            "adults": 2,
            "rooms": 1,
            "options": options,
        },
        "confirmation_scope": "confirmed_visible_search",
        "scan_limit": 12,
        "scanned_count": 0,
        "candidate_summaries": [],
        "explicit_empty_evidence": None,
        "provider_pending_evidence": {
            "contract_version": "qunar-visible-search-pending-v1",
            "result_count_text": "共 家酒店满足条件",
            "pending_message": "请稍等,您查询的结果正在实时搜索中...",
            "observed_duration_ms": 28_391,
        },
        "page_url": "https://hotel.qunar.com/city/i-ka_maafushi/",
        "captured_at": "2026-08-04T12:00:28.391Z",
    }
    return receipt, lodging_inventory_receipt_sha256(receipt)


def _qunar_read_only_detail_case(
    property_id: str = "2075",
) -> tuple[
    BrowserSearchQuery,
    dict[str, object],
    dict[str, object],
    str,
]:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    exact_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "expected_lodging_place_key": "maafushi",
                "expected_package_area": "destination_island",
                "segment": "full",
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
            },
        }
    )
    safe_snapshot = exact_query.model_dump(mode="json")
    safe_snapshot["options"] = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    audited_properties = {
        "2112": ("i-ka_maafushi_2112", "Kaani Palm Beach"),
        "2055": ("i-ka_maafushi_2055", "Kaani Grand Seaview"),
        "2071": ("i-ka_maafushi_2071", "Maafushi View"),
        "2072": ("i-ka_maafushi_2072", "Maafushi Village"),
        "2075": ("i-ka_maafushi_2075", "Maafushi Veli"),
        "2142": ("i-ka_maafushi_2142", "SEASUNBEACH"),
    }
    hotel_seq, property_name = audited_properties[property_id]
    parent_receipt, parent_receipt_sha = _qunar_confirmed_empty_parent_receipt(
        options=safe_snapshot["options"],
        start_date="2026-08-23",
        end_date="2026-08-30",
        quoted_property_id=property_id,
    )
    confirmed_query = LodgingInventoryConfirmedQuery.model_validate(
        parent_receipt["confirmed_query"]
    )
    seed_offset, target_property_ids = qunar_detail_seed_selection(confirmed_query)
    driver = _browser_context(
        BrowserVertical.LODGING,
        BrowserProvider.QUNAR,
        exact_query,
    )["driver"]
    assert isinstance(driver, dict)
    driver.update(
        {
            "mode": "captured_read_only_detail",
            "result_query_readback_confirmed": True,
            "result_query_readback_scope": "qunar_visible_result_form_fields",
            "result_query_readback_evidence": {
                "provider_destination_id": "i-ka_maafushi",
                "result_path": "/city/i-ka_maafushi",
                "destination_text": "马富施",
                "start_date_text": "2026-08-23",
                "end_date_text": "2026-08-30",
                "occupancy_text": "每间人数 2成人 0儿童",
                "room_scope": "audited_qunar_single_room_search_surface",
            },
            "qunar_detail_capture": {
                "source": "qunar_audited_read_only_lodging_detail",
                "contract_scope": "audited_qunar_exact_detail_url",
                "clicked_booking": False,
                "same_controlled_tab": True,
                "city_slug": "i-ka_maafushi",
                "hotel_seq": hotel_seq,
                "property_id": property_id,
                "property_name": property_name,
                "seed_selection_policy": "query-fingerprint-rotation-v1",
                "seed_selection_offset": seed_offset,
                "target_property_ids": list(target_property_ids),
                "list_inventory_receipt": parent_receipt,
                "list_inventory_receipt_sha256": parent_receipt_sha,
                "list_inventory_receipt_schema_version":
                    "tripchord-lodging-inventory-receipt-v2",
                "inventory_observation_chain_schema_version":
                    "tripchord-qunar-empty-observation-chain-v1",
                "inventory_observation_state": "confirmed_empty",
                "inventory_observation_count": 2,
                "inventory_observation_duration_ms": 2_000,
            },
        }
    )
    page_url = (
        f"https://hotel.qunar.com/city/i-ka_maafushi/dt-{property_id}/"
        "?#fromDate=2026-08-23&toDate=2026-08-30&q=&showMap=0"
    )
    details: dict[str, object] = {
        "query": safe_snapshot,
        "driver": driver,
        "destination": "Maafushi",
        "check_in": "2026-08-23",
        "check_out": "2026-08-30",
        "adults": 2,
        "rooms": 1,
        "city_slug": "i-ka_maafushi",
        "hotel_seq": hotel_seq,
        "property_id": property_id,
        "property_name": property_name,
        "location_evidence": "Maafushi, Kaafu Atoll, Maldives",
        "area_text": "Maafushi, Kaafu Atoll, Maldives",
        "area": "destination_island",
        "area_source": "exact_visible_maafushi_kaafu",
        "area_matches_expected": True,
        "expected_lodging_place_key": "maafushi",
        "observed_lodging_place_key": "maafushi",
        "lodging_place_matches_expected": True,
        "kaafu_area_confirmed": True,
        "room_text": "Deluxe Double Room",
        "rate_text": "Deluxe Double Room CNY 673 per night tax included",
        "availability": "available",
        "availability_text": "Available",
        "tax_evidence": "CNY 673 per night tax included",
        "price_text": "CNY 673 per night tax included",
        "price_unit_evidence": "CNY 673 per night tax included",
        "price_basis_source": "audited_qunar_lodging_detail_rate_contract",
        "price_finality": "final_for_rate",
        "clicked_booking": False,
        "extraction": "visible_dom_qunar_lodging_detail",
        "page_url": page_url,
    }
    return exact_query, driver, details, page_url


def test_qunar_read_only_lodging_detail_accepts_only_exact_allowlisted_receipt() -> None:
    exact_query, _, details, page_url = _qunar_read_only_detail_case()
    capture = details["driver"]["qunar_detail_capture"]
    assert capture["seed_selection_offset"] == 4
    assert capture["target_property_ids"] == ["2075", "2142"]
    quote = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.QUNAR,
        amount="673",
        search_query=exact_query,
        page_url_override=page_url,
        title_override=str(details["property_name"]),
        details_update=details,
    )

    result = BrowserQuoteNormalizer().normalize(quote, exact_query)

    assert result.status == QuoteNormalizationStatus.USABLE
    assert isinstance(result.quote, NormalizedLodgingQuote)
    assert result.quote.provider_property_id == "2075"
    assert result.quote.property_name == "Maafushi Veli"
    assert result.quote.total_for_party_cents == 471_100

    localized = _reseal_model_copy(
        quote,
        details_update={
            "location_evidence": "马富施，卡夫环礁，马尔代夫",
            "area_text": "马富施，卡夫环礁，马尔代夫",
        },
    )
    localized_result = BrowserQuoteNormalizer().normalize(localized, exact_query)
    assert localized_result.status == QuoteNormalizationStatus.USABLE

    pending_driver = json.loads(json.dumps(details["driver"]))
    pending_receipt, pending_receipt_sha = _qunar_pending_receipt(
        options={
            "expected_lodging_place_key": "maafushi",
            "expected_package_area": "destination_island",
            "segment": "full",
        },
        start_date="2026-08-23",
        end_date="2026-08-30",
    )
    pending_driver["qunar_detail_capture"].update(
        {
            "list_inventory_receipt": pending_receipt,
            "list_inventory_receipt_sha256": pending_receipt_sha,
            "list_inventory_receipt_schema_version":
                "tripchord-lodging-inventory-receipt-v1",
            "inventory_observation_chain_schema_version": None,
            "inventory_observation_state": "bounded_provider_pending",
            "inventory_observation_count": 1,
            "inventory_observation_duration_ms": 28_391,
        }
    )
    pending_quote = _reseal_model_copy(
        quote,
        details_update={"driver": pending_driver},
    )
    pending_result = BrowserQuoteNormalizer().normalize(
        pending_quote,
        exact_query,
    )
    assert pending_result.status == QuoteNormalizationStatus.USABLE
    assert isinstance(pending_result.quote, NormalizedLodgingQuote)

    second_query, _, second_details, second_page_url = (
        _qunar_read_only_detail_case("2142")
    )
    second_quote = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.QUNAR,
        amount="701",
        search_query=second_query,
        page_url_override=second_page_url,
        title_override=str(second_details["property_name"]),
        details_update=second_details,
    )
    second_result = BrowserQuoteNormalizer().normalize(second_quote, second_query)
    assert second_result.status == QuoteNormalizationStatus.USABLE
    assert isinstance(second_result.quote, NormalizedLodgingQuote)
    assert second_result.quote.provider_property_id == "2142"

    wrong_page_url = page_url.replace("dt-2075", "dt-2112")
    wrong_details = json.loads(json.dumps(details))
    wrong_details.update(
        {
            "hotel_seq": "i-ka_maafushi_2112",
            "property_id": "2112",
            "property_name": "Kaani Palm Beach",
            "page_url": wrong_page_url,
        }
    )
    wrong_capture = wrong_details["driver"]["qunar_detail_capture"]
    wrong_capture.update(
        {
            "hotel_seq": "i-ka_maafushi_2112",
            "property_id": "2112",
            "property_name": "Kaani Palm Beach",
        }
    )
    wrong_quote = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.QUNAR,
        amount="673",
        search_query=exact_query,
        page_url_override=wrong_page_url,
        title_override="Kaani Palm Beach",
        details_update=wrong_details,
    )
    wrong_result = BrowserQuoteNormalizer().normalize(wrong_quote, exact_query)
    assert wrong_result.status == QuoteNormalizationStatus.REJECTED
    assert wrong_result.issues[0].field == "driver.qunar_detail_capture"

    wrong_pending_details = json.loads(json.dumps(wrong_details))
    wrong_pending_driver = json.loads(json.dumps(pending_driver))
    wrong_pending_driver["qunar_detail_capture"].update(
        {
            "hotel_seq": "i-ka_maafushi_2112",
            "property_id": "2112",
            "property_name": "Kaani Palm Beach",
        }
    )
    wrong_pending_details["driver"] = wrong_pending_driver
    wrong_pending_quote = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.QUNAR,
        amount="673",
        search_query=exact_query,
        page_url_override=wrong_page_url,
        title_override="Kaani Palm Beach",
        details_update=wrong_pending_details,
    )
    wrong_pending_result = BrowserQuoteNormalizer().normalize(
        wrong_pending_quote,
        exact_query,
    )
    assert wrong_pending_result.status == QuoteNormalizationStatus.REJECTED
    assert wrong_pending_result.issues[0].field == "driver.qunar_detail_capture"


def test_qunar_read_only_lodging_detail_rejects_contract_and_lineage_drift() -> None:
    exact_query, driver, details, page_url = _qunar_read_only_detail_case()

    def normalize_with(
        *,
        driver_value: dict[str, object] | None = None,
        details_value: dict[str, object] | None = None,
        url: str = page_url,
    ) -> tuple[QuoteNormalizationStatus, QuoteNormalizationCode, str | None]:
        mutated_details = json.loads(json.dumps(details_value or details))
        mutated_details["driver"] = json.loads(json.dumps(driver_value or driver))
        mutated_details["page_url"] = url
        candidate = browser_quote(
            BrowserVertical.LODGING,
            provider=BrowserProvider.QUNAR,
            amount="673",
            search_query=exact_query,
            page_url_override=page_url,
            title_override=str(mutated_details["property_name"]),
            details_update=mutated_details,
        )
        if url != page_url:
            candidate = _reseal_model_copy(
                candidate.model_copy(update={"page_url": url}),
                details_update={},
            )
        result = BrowserQuoteNormalizer().normalize(candidate, exact_query)
        assert result.issues
        return result.status, result.issues[0].code, result.issues[0].field

    unsafe_urls = (
        page_url.replace("https://", "http://"),
        page_url.replace("hotel.qunar.com", "evil.example"),
        page_url.replace("i-ka_maafushi", "i-hulhumale"),
        page_url.replace("dt-2075", "dt-9999"),
        page_url.replace("/?#", "/?tracking=1#"),
        page_url.replace("fromDate=2026-08-23", "fromDate=2026-08-24"),
        page_url.replace("q=", "q=beach"),
        page_url.replace("showMap=0", "showMap=1"),
        f"{page_url}&tracking=1",
    )
    for unsafe_url in unsafe_urls:
        status, code, field = normalize_with(url=unsafe_url)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
        assert field == "driver.qunar_detail_capture"

    for field, value in (
        ("source", "visible_untrusted_link"),
        ("contract_scope", "generic_detail_url"),
        ("clicked_booking", True),
        ("same_controlled_tab", False),
        ("property_id", "2055"),
        ("seed_selection_policy", "fixed-first-two-v1"),
        ("seed_selection_offset", 0),
        ("target_property_ids", ["2112", "2055"]),
        ("list_inventory_receipt_sha256", "not-a-sha"),
        ("list_inventory_receipt_sha256", "f" * 64),
        ("inventory_observation_state", "unverified_pending"),
        ("inventory_observation_count", 1),
        ("inventory_observation_duration_ms", 1_999),
    ):
        damaged_driver = json.loads(json.dumps(driver))
        damaged_driver["qunar_detail_capture"][field] = value
        status, code, issue_field = normalize_with(driver_value=damaged_driver)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
        assert issue_field == "driver.qunar_detail_capture"

    for mutation in (
        "missing_chain",
        "reordered",
        "short_interval",
        "query_mismatch",
        "tab_mismatch",
        "legacy_fallback_summary",
        "wrong_fallback_seed",
        "fallback_does_not_cover_quote",
    ):
        damaged_driver = json.loads(json.dumps(driver))
        capture = damaged_driver["qunar_detail_capture"]
        receipt = capture["list_inventory_receipt"]
        chain = receipt["observation_chain"]
        if mutation == "missing_chain":
            receipt.pop("observation_chain")
        elif mutation == "reordered":
            chain["observations"].reverse()
        elif mutation == "short_interval":
            chain["observed_interval_ms"] = 1_999
        elif mutation == "query_mismatch":
            second_observation = chain["observations"][1]
            second_observation["receipt"]["confirmed_query"]["destination"] = "Malé"
            second_observation["receipt_sha256"] = lodging_inventory_receipt_sha256(
                second_observation["receipt"]
            )
        elif mutation == "tab_mismatch":
            chain["observations"][1]["lineage"]["tab_lineage_sha256"] = "9" * 64
        elif mutation == "legacy_fallback_summary":
            chain["detail_fallback"] = {
                "contract_version": "tripchord-qunar-detail-fallback-summary-v1",
                "attempted": True,
                "target_limit": 2,
                "target_property_ids": ["2112", "2055"],
                "observed_results": [
                    {
                        "property_id": "2112",
                        "state": "succeeded",
                        "verified_quote_count": 1,
                    },
                    {
                        "property_id": "2055",
                        "state": "failed",
                        "verified_quote_count": 0,
                    },
                ],
                "verified_quote_count": 1,
            }
        elif mutation == "wrong_fallback_seed":
            chain["detail_fallback"].update(
                {
                    "seed_selection_offset": 0,
                    "target_property_ids": ["2112", "2055"],
                    "observed_results": [
                        {
                            "property_id": "2112",
                            "state": "succeeded",
                            "verified_quote_count": 1,
                        },
                        {
                            "property_id": "2055",
                            "state": "failed",
                            "verified_quote_count": 0,
                        },
                    ],
                }
            )
        else:
            chain["detail_fallback"]["observed_results"][0][
                "verified_quote_count"
            ] = 0
            chain["detail_fallback"]["verified_quote_count"] = 0
        capture["list_inventory_receipt_sha256"] = lodging_inventory_receipt_sha256(
            receipt
        )
        status, code, issue_field = normalize_with(driver_value=damaged_driver)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
        assert issue_field == "driver.qunar_detail_capture"

    for pending_field, pending_value in (
        ("inventory_observation_count", 2),
        ("inventory_observation_duration_ms", 24_999),
        ("inventory_observation_duration_ms", 120_001),
    ):
        damaged_driver = json.loads(json.dumps(driver))
        damaged_driver["qunar_detail_capture"].update(
            {
                "inventory_observation_state": "bounded_provider_pending",
                "inventory_observation_count": 1,
                "inventory_observation_duration_ms": 28_391,
                pending_field: pending_value,
            }
        )
        status, code, issue_field = normalize_with(driver_value=damaged_driver)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
        assert issue_field == "driver.qunar_detail_capture"

    lineage_mutations = (
        ("result_query_readback_confirmed", False),
        ("result_query_readback_scope", "generic_result_readback"),
    )
    for field, value in lineage_mutations:
        damaged_driver = json.loads(json.dumps(driver))
        damaged_driver[field] = value
        status, _, _ = normalize_with(driver_value=damaged_driver)
        assert status == QuoteNormalizationStatus.REJECTED

    for field, value in (
        ("provider_destination_id", "i-hulhumale"),
        ("result_path", "/city/i-hulhumale"),
        ("destination_text", "胡鲁马累"),
        ("start_date_text", "2026-08-24"),
        ("occupancy_text", "每间人数 1成人 0儿童"),
        ("room_scope", "unknown_room_surface"),
    ):
        damaged_driver = json.loads(json.dumps(driver))
        damaged_driver["result_query_readback_evidence"][field] = value
        status, code, issue_field = normalize_with(driver_value=damaged_driver)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
        assert issue_field == "driver.result_query_readback_evidence"

    detail_mutations = (
        ("extraction", "visible_dom_generic_detail"),
        ("city_slug", "i-hulhumale"),
        ("hotel_seq", "i-ka_maafushi_9999"),
        ("property_id", "9999"),
        ("property_name", "Different Hotel"),
        ("location_evidence", "Maafushi, Maldives"),
        ("location_evidence", "Kaafu Atoll, Maldives"),
        ("location_evidence", "Maafushi, Kaafu Atoll, near Hulhumale"),
        ("location_evidence", "马富施，卡夫环礁，靠近班度士"),
        ("check_in", "2026-08-24"),
        ("adults", 1),
        ("rooms", 2),
        ("clicked_booking", True),
        ("price_basis_source", "visible_generic_price"),
        ("price_finality", "starting_or_estimated"),
    )
    for detail_field, detail_value in detail_mutations:
        damaged_details = json.loads(json.dumps(details))
        damaged_details[detail_field] = detail_value
        status, code, issue_field = normalize_with(details_value=damaged_details)
        assert status == QuoteNormalizationStatus.REJECTED
        assert code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
        assert issue_field == "details"


def test_ctrip_audited_property_seed_detail_is_exact_and_read_only() -> None:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    exact_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "expected_lodging_place_key": "maafushi",
                "expected_package_area": "destination_island",
                "segment": "full",
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
            },
        }
    )
    safe_snapshot = exact_query.model_dump(mode="json")
    safe_snapshot["options"] = {
        "expected_lodging_place_key": "maafushi",
        "expected_package_area": "destination_island",
        "segment": "full",
    }
    driver = _browser_context(
        BrowserVertical.LODGING,
        BrowserProvider.CTRIP,
        exact_query,
    )["driver"]
    assert isinstance(driver, dict)
    driver.update(
        {
            "mode": "audited_property_seed_detail_fallback",
            "confirmation_scope": "confirmed_visible_seed_detail",
            "detail_capture": {
                "source": "public_audited_property_id",
                "hotel_id": "131576087",
                "clicked_booking": False,
            },
        }
    )
    page_url = (
        "https://hotels.ctrip.com/hotels/detail/"
        "?cityEnName=Maafushi&cityId=35851&hotelId=131576087"
        "&checkIn=2026-08-23&checkOut=2026-08-30&adult=2"
        "&children=0&crn=1&ages=&curr=CNY&barcurr=CNY"
    )

    def quote_with(
        receipt: dict[str, object],
        *,
        area_text: str = "Maafushi",
        observed_place: str = "maafushi",
        place_matches: bool = True,
    ) -> BrowserQuote:
        return browser_quote(
            BrowserVertical.LODGING,
            provider=BrowserProvider.CTRIP,
            search_query=exact_query,
            page_url_override=page_url,
            details_update={
                "query": safe_snapshot,
                "driver": receipt,
                "destination": "Maafushi",
                "area_text": area_text,
                "area_matches_expected": place_matches,
                "expected_lodging_place_key": "maafushi",
                "observed_lodging_place_key": observed_place,
                "lodging_place_matches_expected": place_matches,
            },
        )

    accepted = BrowserQuoteNormalizer().normalize(quote_with(driver), exact_query)
    assert accepted.status == QuoteNormalizationStatus.USABLE

    unsafe_mutations = (
        ("hotel_id", "29935473"),
        ("source", "untrusted_property_seed"),
        ("clicked_booking", True),
    )
    for field, value in unsafe_mutations:
        damaged = json.loads(json.dumps(driver))
        damaged["detail_capture"][field] = value
        rejected = BrowserQuoteNormalizer().normalize(
            quote_with(damaged),
            exact_query,
        )
        assert rejected.status == QuoteNormalizationStatus.REJECTED
        assert rejected.issues[0].code == QuoteNormalizationCode.UNSAFE_BROWSER_ACTION
        assert rejected.issues[0].field == "driver.detail_capture"

    wrong_query = json.loads(json.dumps(driver))
    wrong_query["readback_query"]["start_date"] = "2026-08-24"
    query_rejected = BrowserQuoteNormalizer().normalize(
        quote_with(wrong_query),
        exact_query,
    )
    assert query_rejected.status == QuoteNormalizationStatus.REJECTED
    assert query_rejected.issues[0].field == "driver.readback_query.start_date"

    place_rejected = BrowserQuoteNormalizer().normalize(
        quote_with(
            driver,
            area_text="Hulhumalé",
            observed_place="hulhumale",
            place_matches=False,
        ),
        exact_query,
    )
    assert place_rejected.status == QuoteNormalizationStatus.REJECTED


def test_tongcheng_audited_city_id_readback_is_exactly_place_bound() -> None:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    exact_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "expected_lodging_place_key": "maafushi",
                "expected_package_area": "destination_island",
                "segment": "full",
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
            },
        }
    )
    driver = _browser_context(
        BrowserVertical.LODGING,
        BrowserProvider.TONGCHENG,
        exact_query,
    )["driver"]
    assert isinstance(driver, dict)
    driver["readback_query"] = {
        "destination": "audited-city-id:110018575",
        "start_date": "2026-08-23",
        "end_date": "2026-08-30",
        "adults": 2,
        "rooms": 1,
    }
    driver["destination_confirmation_scope"] = (
        "prefrozen_overseas_city_id_with_audited_party_url"
    )
    driver["lodging_search_strategy"] = {
        "provider_destination": "马富施",
        "provider_destination_id": "110018575",
        "keyword": None,
        "evidence_scope": (
            "provider_audited_exact_overseas_city_id_then_place_revalidation"
        ),
    }
    exact = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.TONGCHENG,
        search_query=exact_query,
        details_update={
            "driver": driver,
            "destination": "Maafushi",
            "area_text": "马富施",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )
    accepted = BrowserQuoteNormalizer().normalize(exact, exact_query)
    assert accepted.status == QuoteNormalizationStatus.USABLE

    wrong_driver = json.loads(json.dumps(driver))
    wrong_driver["readback_query"]["destination"] = "audited-city-id:110018578"
    wrong = browser_quote(
        BrowserVertical.LODGING,
        provider=BrowserProvider.TONGCHENG,
        search_query=exact_query,
        details_update={
            "driver": wrong_driver,
            "destination": "Maafushi",
            "area_text": "马富施",
            "expected_lodging_place_key": "maafushi",
            "observed_lodging_place_key": "maafushi",
            "lodging_place_matches_expected": True,
        },
    )
    rejected = BrowserQuoteNormalizer().normalize(wrong, exact_query)
    assert rejected.status == QuoteNormalizationStatus.REJECTED
    assert rejected.issues[0].field == "driver.readback_query.destination"


def test_lodging_per_night_and_transfer_are_normalized_without_llm_arithmetic() -> None:
    evidence = "单程 airport → destination_island，45分钟，需提前预约，含税每人 CNY 50"
    transfer = {
        "currency": "CNY",
        "taxes_included": True,
        "tax_evidence": evidence,
        "price_basis": "per_person",
        "price_scope": "one_way",
        "amount": "50",
        "price_evidence": evidence,
        "price_contract_key": "fixture-transfer-one-way",
        "purchase_scope": "hotel_bound",
        "purchase_scope_evidence": evidence,
        "origin_area": "airport",
        "destination_area": "destination_island",
        "direction_evidence": evidence,
        "schedule_mode": "exact_departure",
        "service_date": "2026-08-23",
        "duration_minutes": 45,
        "schedule_evidence": evidence,
        "depart_at": "2026-08-23T21:00:00+05:00",
        "arrive_at": "2026-08-23T21:45:00+05:00",
        "operates_24_hours": False,
        "requires_reservation": True,
        "availability": "available",
        "evidence_text": evidence,
        "detail_url": "https://hotels.ctrip.com/hotels/detail/fixture",
        "evidence_sha256": "c" * 64,
    }
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            amount="673",
            details_update={
                "check_in": "2026-08-24",
                "check_out": "2026-08-29",
                "transfers": [transfer],
            },
        ),
        query(),
    )

    assert result.status == QuoteNormalizationStatus.USABLE
    assert isinstance(result.quote, NormalizedLodgingQuote)
    assert result.quote.area == PackageArea.DESTINATION_ISLAND
    assert result.quote.night_count == 5
    assert result.quote.total_for_party_cents == 336_500
    assert len(result.transfers) == 1
    assert result.transfers[0].total_for_party_cents == 10_000
    assert result.issues == ()


def test_explicit_round_trip_window_contract_preserves_two_directions_and_one_price() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={"transfers": _round_trip_window_transfers()},
        ),
        query(),
    )

    assert result.usable
    assert len(result.transfers) == 2
    outbound, inbound = result.transfers
    assert (outbound.origin_area, outbound.destination_area) == (
        PackageArea.AIRPORT,
        PackageArea.AIRPORT_ISLAND,
    )
    assert (inbound.origin_area, inbound.destination_area) == (
        PackageArea.AIRPORT_ISLAND,
        PackageArea.AIRPORT,
    )
    assert outbound.price_contract_id == inbound.price_contract_id
    assert outbound.total_for_party_cents == inbound.total_for_party_cents == 10_800
    assert outbound.operates_24_hours
    assert outbound.has_feasible_departure(
        not_before=datetime.fromisoformat("2026-08-23T22:30:00+05:00")
    )


def test_transfer_place_binding_rejects_noncanonical_frozen_place_area_pair() -> None:
    mismatched = query().model_copy(
        update={
            "options": {
                **query().options,
                "expected_lodging_place_key": "hulhumale",
                "expected_package_area": "destination_island",
            }
        }
    )

    assert BrowserQuoteNormalizer._transfer_place_keys(
        mismatched,
        PackageArea.AIRPORT,
        PackageArea.DESTINATION_ISLAND,
    ) == (None, None)

    canonical = mismatched.model_copy(
        update={
            "options": {
                **mismatched.options,
                "expected_package_area": "airport_island",
            }
        }
    )
    assert BrowserQuoteNormalizer._transfer_place_keys(
        canonical,
        PackageArea.AIRPORT,
        PackageArea.AIRPORT_ISLAND,
    ) == (PackagePlaceKey.VELANA_AIRPORT, PackagePlaceKey.HULHUMALE)


def test_round_trip_price_contract_is_scoped_to_parent_lodging_and_search_segment() -> None:
    first_query = query().model_copy(
        update={
            "start_date": date(2026, 8, 23),
            "end_date": date(2026, 8, 24),
            "options": {"segment": "first"},
        }
    )
    last_query = query().model_copy(
        update={
            "start_date": date(2026, 8, 29),
            "end_date": date(2026, 8, 30),
            "options": {"segment": "last"},
        }
    )
    first = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "check_in": "2026-08-23",
                "check_out": "2026-08-24",
                "transfers": _round_trip_window_transfers(
                    "2026-08-23",
                    "2026-08-24",
                ),
            },
            search_query=first_query,
        ),
        first_query,
    )
    last = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "check_in": "2026-08-29",
                "check_out": "2026-08-30",
                "transfers": _round_trip_window_transfers(
                    "2026-08-29",
                    "2026-08-30",
                ),
            },
            search_query=last_query,
        ),
        last_query,
    )

    assert first.usable and last.usable
    assert first.quote is not None and last.quote is not None
    assert {item.price_contract_id for item in first.transfers}.isdisjoint(
        {item.price_contract_id for item in last.transfers}
    )
    assert {item.bound_lodging_id for item in first.transfers} == {first.quote.id}
    assert {item.bound_lodging_id for item in last.transfers} == {last.quote.id}


def test_incomplete_transfer_contracts_are_typed_rejections_and_never_zero_filled() -> None:
    base = _round_trip_window_transfers()[0]
    cases = (
        {**base, "taxes_included": None, "tax_evidence": "税费以酒店确认为准"},
        {**base, "amount": None, "price_evidence": "价格以酒店确认为准"},
        {
            **base,
            "schedule_mode": None,
            "service_window_start_at": None,
            "service_window_end_at": None,
            "schedule_evidence": "接送时间以酒店确认为准",
        },
        {
            **base,
            "purchase_scope": "public_independent",
            "purchase_scope_evidence": "酒店提供接驳，详情以酒店确认为准",
        },
    )

    for partial in cases:
        result = BrowserQuoteNormalizer().normalize(
            browser_quote(
                BrowserVertical.LODGING,
                details_update={"transfers": [partial]},
            ),
            query(),
        )
        assert result.usable
        assert result.transfers == ()
        assert len(result.issues) == 1
        assert result.issues[0].code == QuoteNormalizationCode.INVALID_TRANSFER


def test_transfer_mention_without_contract_is_typed_rejection() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "transfer_text": "酒店提供机场接送，详情以酒店确认为准",
                "transfers": [],
                "transfer_detail_status": "missing_explicit_contract",
            },
        ),
        query(),
    )

    assert result.usable
    assert result.transfers == ()
    assert result.issues[0].code == QuoteNormalizationCode.INVALID_TRANSFER
    assert result.issues[0].field == "transfers"


def test_total_stay_is_not_multiplied_by_nights_or_rooms() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            amount="3365.00",
            basis=QuotePriceBasis.TOTAL_STAY,
            details_update={
                "check_in": "2026-08-24",
                "check_out": "2026-08-29",
            },
        ),
        query(),
    )

    assert isinstance(result.quote, NormalizedLodgingQuote)
    assert result.quote.total_for_party_cents == 336_500


def test_incomplete_or_ambiguous_prices_are_typed_rejections() -> None:
    normalizer = BrowserQuoteNormalizer()
    cases = (
        (
            browser_quote(BrowserVertical.FLIGHT, taxes_included=None),
            QuoteNormalizationCode.TAXES_INCOMPLETE,
        ),
        (
            browser_quote(
                BrowserVertical.FLIGHT,
                basis=QuotePriceBasis.UNKNOWN,
            ),
            QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
        ),
        (
            browser_quote(BrowserVertical.FLIGHT, amount="4692.001"),
            QuoteNormalizationCode.NON_INTEGRAL_CENTS,
        ),
        (
            _reseal_model_copy(
                browser_quote(BrowserVertical.FLIGHT),
                details_update={"outbound_departure_at": None},
            ),
            QuoteNormalizationCode.MISSING_FIELD,
        ),
    )

    for quote, expected in cases:
        result = normalizer.normalize(quote, query())
        assert result.status == QuoteNormalizationStatus.REJECTED
        assert result.quote is None
        assert result.issues[0].code == expected


def test_unknown_baggage_and_breakfast_remain_unknown_instead_of_false_defaults() -> None:
    flight_result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.FLIGHT,
            details_update={
                "baggage_text": "行李额以详情页为准",
                "checked_baggage_per_adult_kg": None,
            },
        ),
        query(),
    )
    lodging_result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "breakfast_text": "早餐详情以页面为准",
                "breakfast_included": None,
            },
        ),
        query(),
    )

    assert isinstance(flight_result.quote, NormalizedFlightQuote)
    assert flight_result.quote.checked_baggage_per_adult_kg is None
    assert isinstance(lodging_result.quote, NormalizedLodgingQuote)
    assert lodging_result.quote.breakfast_included is None


def test_explicit_checked_baggage_value_is_preserved() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.FLIGHT,
            details_update={
                "baggage_text": "每位成人免费托运行李 23kg",
                "checked_baggage_per_adult_kg": 23,
            },
        ),
        query(),
    )

    assert isinstance(result.quote, NormalizedFlightQuote)
    assert result.quote.checked_baggage_per_adult_kg == 23


def test_unknown_or_mismatched_lodging_area_is_typed_rejection() -> None:
    normalizer = BrowserQuoteNormalizer()
    unknown = normalizer.normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "area": None,
                "area_source": None,
                "area_text": "位置以酒店最终确认为准",
            },
        ),
        query(),
    )
    airport_query = query().model_copy(
        update={
            "options": {
                "segment": "first",
                "expected_package_area": "airport_island",
            }
        }
    )
    mismatched = normalizer.normalize(
        browser_quote(
            BrowserVertical.LODGING,
            search_query=airport_query,
        ),
        airport_query,
    )

    assert unknown.status == QuoteNormalizationStatus.REJECTED
    assert unknown.issues[0].code == QuoteNormalizationCode.MISSING_FIELD
    assert unknown.issues[0].field == "area"
    assert mismatched.status == QuoteNormalizationStatus.REJECTED
    assert mismatched.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH
    assert mismatched.issues[0].field == "area"


def test_confirmed_exact_search_area_requires_matching_visible_driver_evidence() -> None:
    exact_query = query().model_copy(
        update={
            "destination": "South Ari Atoll",
            "options": {
                "segment": "middle",
                "expected_package_area": "destination_island",
            },
        }
    )
    exact_quote = browser_quote(
        BrowserVertical.LODGING,
        details_update={
            "destination": "South Ari Atoll",
            "area": "destination_island",
            "area_text": "South Ari Atoll",
            "area_source": "confirmed_exact_search_area",
            "driver": {
                "mode": "visible_form",
                "triggered": True,
                "provider": "ctrip",
                "vertical": "lodging",
                "confirmed_query": {
                    "destination": "South Ari Atoll",
                    "start_date": "2026-08-23",
                    "end_date": "2026-08-30",
                    "adults": 2,
                    "rooms": 1,
                },
                "readback_query": {
                    "destination": "South Ari Atoll",
                    "start_date": "2026-08-23",
                    "end_date": "2026-08-30",
                    "adults": 2,
                    "rooms": 1,
                },
                "confirmation_scope": "confirmed_visible_search",
            },
        },
        search_query=exact_query,
    )
    usable = BrowserQuoteNormalizer().normalize(exact_quote, exact_query)
    unconfirmed = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "destination": "South Ari Atoll",
                "area": "destination_island",
                "area_text": "South Ari Atoll",
                "area_source": "confirmed_exact_search_area",
                "driver": {
                    "mode": "visible_form",
                    "triggered": True,
                    "provider": "ctrip",
                    "vertical": "lodging",
                    "confirmed_query": {
                        "destination": "马累",
                        "start_date": "2026-08-23",
                        "end_date": "2026-08-30",
                        "adults": 2,
                        "rooms": 1,
                    },
                    "readback_query": {
                        "destination": "马累",
                        "start_date": "2026-08-23",
                        "end_date": "2026-08-30",
                        "adults": 2,
                        "rooms": 1,
                    },
                    "confirmation_scope": "confirmed_visible_search",
                },
            },
            search_query=exact_query,
        ),
        exact_query,
    )

    assert isinstance(usable.quote, NormalizedLodgingQuote)
    assert usable.quote.area == PackageArea.DESTINATION_ISLAND
    assert unconfirmed.status == QuoteNormalizationStatus.REJECTED
    assert unconfirmed.issues[0].code == QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH


def test_trusted_profile_and_confirmed_search_area_set_lodging_place_key() -> None:
    profile = system_stay_area_search_profile("MLE")
    assert profile is not None
    exact_query = query().model_copy(
        update={
            "destination": "Maafushi",
            "destination_code": None,
            "options": {
                "gateway_destination": "MLE",
                "stay_area_search_profile": profile.model_dump(mode="json"),
                "segment": "middle",
                "expected_package_area": "destination_island",
                "expected_lodging_place_key": "maafushi",
            },
        }
    )
    normalized = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "destination": "Maafushi",
                "area": "destination_island",
                "area_text": "Maafushi",
                "area_source": "confirmed_exact_search_area",
            },
            search_query=exact_query,
        ),
        exact_query,
    )

    assert normalized.status == QuoteNormalizationStatus.USABLE
    assert isinstance(normalized.quote, NormalizedLodgingQuote)
    assert normalized.quote.place_key == PackagePlaceKey.MAAFUSHI


def test_lodging_place_key_is_not_guessed_from_visible_free_text() -> None:
    normalized = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "area": "destination_island",
                "area_text": "Maafushi",
                "area_source": "visible_label",
            },
        ),
        query(),
    )

    assert normalized.status == QuoteNormalizationStatus.USABLE
    assert isinstance(normalized.quote, NormalizedLodgingQuote)
    assert normalized.quote.place_key is None


def test_invalid_optional_transfer_is_omitted_but_primary_lodging_remains_usable() -> None:
    result = BrowserQuoteNormalizer().normalize(
        browser_quote(
            BrowserVertical.LODGING,
            details_update={
                "transfers": [
                    {
                        "currency": "CNY",
                        "taxes_included": False,
                        "price_basis": "total_party",
                        "amount": "100",
                        "origin_area": "airport",
                        "destination_area": "destination_island",
                        "depart_at": "2026-08-23T21:00:00+05:00",
                        "arrive_at": "2026-08-23T21:45:00+05:00",
                    }
                ]
            },
        ),
        query(),
    )

    assert result.usable
    assert result.transfers == ()
    assert result.issues[0].code == QuoteNormalizationCode.INVALID_TRANSFER
