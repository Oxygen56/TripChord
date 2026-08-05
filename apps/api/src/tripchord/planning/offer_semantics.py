from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from enum import StrEnum

from pydantic import Field, JsonValue

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    NormalizedFlightQuote,
    NormalizedLodgingQuote,
    PackageQuote,
    TransferOption,
)


class OfferSemanticChange(StrEnum):
    NO_CHANGE = "no_change"
    OBSERVATION_REFRESHED = "observation_refreshed"
    PRICE_CHANGED = "price_changed"
    AVAILABILITY_CHANGED = "availability_changed"
    TERMS_CHANGED = "terms_changed"
    DIFFERENT_PRODUCT = "different_product"
    IDENTITY_AMBIGUOUS = "identity_ambiguous"


class OfferIdentitySource(StrEnum):
    PROVIDER_OFFICIAL_ID = "provider_official_id"
    HYBRID = "hybrid"
    SEMANTIC_FINGERPRINT = "semantic_fingerprint"


class OfferIdentityConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OfferIdentity(DomainModel):
    """Stable identity independent of DOM ids, price, freshness, and evidence receipts."""

    product_key: str = Field(min_length=64, max_length=64)
    offer_key: str = Field(min_length=64, max_length=64)
    product_payload: dict[str, JsonValue]
    terms_payload: dict[str, JsonValue]
    product_source: OfferIdentitySource
    offer_source: OfferIdentitySource
    product_confidence: OfferIdentityConfidence
    offer_confidence: OfferIdentityConfidence
    product_ambiguous: bool
    offer_ambiguous: bool
    ambiguity_reasons: tuple[str, ...] = ()
    official_product_id: str | None = None
    official_offer_id: str | None = None


class OfferSemanticDiff(DomainModel):
    change: OfferSemanticChange
    same_product: bool
    same_offer: bool
    different_product_confirmed: bool
    identity_ambiguous: bool
    price_changed: bool
    old_offer_key: str = Field(min_length=64, max_length=64)
    new_offer_key: str = Field(min_length=64, max_length=64)
    old_total_for_party_cents: int = Field(ge=0)
    new_total_for_party_cents: int = Field(ge=0)
    currency: str | None = None
    changed_fields: tuple[str, ...] = ()


def stable_offer_identity(quote: PackageQuote) -> OfferIdentity:
    (
        product,
        product_source,
        product_confidence,
        product_ambiguous,
        product_reasons,
        official_product_id,
    ) = _product_identity(quote)
    terms = _terms_payload(quote)
    official_offer_id = _official_offer_id(quote)
    if official_offer_id is not None:
        offer_source = OfferIdentitySource.PROVIDER_OFFICIAL_ID
        offer_confidence = OfferIdentityConfidence.HIGH
        offer_ambiguous = False
        offer_reasons: tuple[str, ...] = ()
    elif isinstance(quote, TransferOption):
        # A transfer contract has a typed route, service date/window, purchase
        # scope and price guarantee.  Its browser-generated price_contract_id is
        # evidence-versioned, so it is intentionally not treated as an official
        # stable offer id.
        offer_source = OfferIdentitySource.SEMANTIC_FINGERPRINT
        offer_confidence = OfferIdentityConfidence.MEDIUM
        offer_ambiguous = False
        offer_reasons = ()
    elif _semantic_offer_is_complete(quote, product_ambiguous=product_ambiguous):
        offer_source = OfferIdentitySource.SEMANTIC_FINGERPRINT
        offer_confidence = OfferIdentityConfidence.MEDIUM
        offer_ambiguous = False
        offer_reasons = ()
    else:
        offer_source = OfferIdentitySource.SEMANTIC_FINGERPRINT
        offer_confidence = OfferIdentityConfidence.LOW
        offer_ambiguous = True
        offer_reasons = ("provider_offer_id_missing",)
    offer_payload: dict[str, JsonValue] = {
        "product": product,
        "terms": terms,
        "official_offer_id": official_offer_id,
    }
    return OfferIdentity(
        product_key=_digest(product),
        offer_key=_digest(offer_payload),
        product_payload=product,
        terms_payload=terms,
        product_source=product_source,
        offer_source=offer_source,
        product_confidence=product_confidence,
        offer_confidence=offer_confidence,
        product_ambiguous=product_ambiguous,
        offer_ambiguous=offer_ambiguous,
        ambiguity_reasons=(*product_reasons, *offer_reasons),
        official_product_id=official_product_id,
        official_offer_id=official_offer_id,
    )


def semantic_offer_diff(before: PackageQuote, after: PackageQuote) -> OfferSemanticDiff:
    old_identity = stable_offer_identity(before)
    new_identity = stable_offer_identity(after)
    product_keys_equal = old_identity.product_key == new_identity.product_key
    product_identity_ambiguous = _product_comparison_is_ambiguous(
        old_identity,
        new_identity,
        keys_equal=product_keys_equal,
    )
    same_product = product_keys_equal and not product_identity_ambiguous
    different_product_confirmed = (
        not product_keys_equal and not product_identity_ambiguous
    )
    offer_keys_equal = old_identity.offer_key == new_identity.offer_key
    offer_identity_ambiguous = (
        same_product
        and offer_keys_equal
        and (old_identity.offer_ambiguous or new_identity.offer_ambiguous)
    )
    same_offer = same_product and offer_keys_equal and not offer_identity_ambiguous
    identity_ambiguous = product_identity_ambiguous or offer_identity_ambiguous
    same_currency = before.currency == after.currency
    price_changed = (
        same_offer
        and same_currency
        and before.total_for_party_cents != after.total_for_party_cents
    )
    changed_fields: list[str] = []
    if before.currency != after.currency:
        changed_fields.append("currency")
    if before.total_for_party_cents != after.total_for_party_cents:
        changed_fields.append("total_for_party_cents")
    if before.availability != after.availability:
        changed_fields.append("availability")
    if before.taxes_and_fees_included != after.taxes_and_fees_included:
        changed_fields.append("taxes_and_fees_included")
    if before.captured_at != after.captured_at:
        changed_fields.append("captured_at")
    if before.expires_at != after.expires_at:
        changed_fields.append("expires_at")
    if before.evidence_refs != after.evidence_refs:
        changed_fields.append("evidence_refs")
    if before.id != after.id:
        changed_fields.append("transient_id")
    if old_identity.product_key != new_identity.product_key:
        changed_fields.append("product_identity")
    if old_identity.terms_payload != new_identity.terms_payload:
        changed_fields.append("offer_terms")
    if old_identity.official_offer_id != new_identity.official_offer_id:
        changed_fields.append("provider_offer_id")

    if identity_ambiguous:
        change = OfferSemanticChange.IDENTITY_AMBIGUOUS
    elif different_product_confirmed:
        change = OfferSemanticChange.DIFFERENT_PRODUCT
    elif not same_offer or not same_currency:
        change = OfferSemanticChange.TERMS_CHANGED
    elif before.availability != after.availability:
        change = OfferSemanticChange.AVAILABILITY_CHANGED
    elif price_changed:
        change = OfferSemanticChange.PRICE_CHANGED
    elif any(
        field in changed_fields
        for field in ("captured_at", "expires_at", "evidence_refs", "transient_id")
    ):
        change = OfferSemanticChange.OBSERVATION_REFRESHED
    else:
        change = OfferSemanticChange.NO_CHANGE

    return OfferSemanticDiff(
        change=change,
        same_product=same_product,
        same_offer=same_offer,
        different_product_confirmed=different_product_confirmed,
        identity_ambiguous=identity_ambiguous,
        price_changed=price_changed,
        old_offer_key=old_identity.offer_key,
        new_offer_key=new_identity.offer_key,
        old_total_for_party_cents=before.total_for_party_cents,
        new_total_for_party_cents=after.total_for_party_cents,
        currency=before.currency if same_currency else None,
        changed_fields=tuple(changed_fields),
    )


def same_stable_offer(left: PackageQuote, right: PackageQuote) -> bool:
    return semantic_offer_diff(left, right).same_offer


def _product_identity(
    quote: PackageQuote,
) -> tuple[
    dict[str, JsonValue],
    OfferIdentitySource,
    OfferIdentityConfidence,
    bool,
    tuple[str, ...],
    str | None,
]:
    base: dict[str, JsonValue] = {
        "provider": _text(quote.provider),
        "quote_type": type(quote).__name__,
    }
    if isinstance(quote, NormalizedFlightQuote):
        official_product_id = _clean_identifier(
            quote.provider_itinerary_id
        ) or _clean_identifier(quote.provider_offer_id)
        product = {
            **base,
            "official_product_id": _identifier(official_product_id),
            "origin": _text(quote.origin),
            "destination": _text(quote.destination),
            "adults": quote.adults,
            "outbound_depart_at": _timestamp(quote.outbound_depart_at),
            "outbound_arrive_at": _timestamp(quote.outbound_arrive_at),
            "return_depart_at": _timestamp(quote.return_depart_at),
            "return_arrive_at": _timestamp(quote.return_arrive_at),
            "outbound_flight_numbers": _text_tuple(quote.outbound_flight_numbers),
            "return_flight_numbers": _text_tuple(quote.return_flight_numbers),
        }
        if official_product_id is not None:
            return (
                product,
                OfferIdentitySource.PROVIDER_OFFICIAL_ID,
                OfferIdentityConfidence.HIGH,
                False,
                (),
                official_product_id,
            )
        if quote.outbound_flight_numbers and quote.return_flight_numbers:
            return (
                product,
                OfferIdentitySource.SEMANTIC_FINGERPRINT,
                OfferIdentityConfidence.MEDIUM,
                False,
                (),
                None,
            )
        return (
            product,
            OfferIdentitySource.SEMANTIC_FINGERPRINT,
            OfferIdentityConfidence.LOW,
            True,
            ("flight_numbers_missing",),
            None,
        )
    if isinstance(quote, NormalizedLodgingQuote):
        lodging_official_product_id: str | None = None
        property_id = _clean_identifier(quote.provider_property_id)
        room_id = _clean_identifier(quote.provider_room_id)
        offer_id = _clean_identifier(quote.provider_offer_id)
        rate_plan_id = _clean_identifier(quote.provider_rate_plan_id)
        if property_id and room_id:
            lodging_official_product_id = (
                f"property:{property_id}|room:{room_id}"
            )
        elif offer_id:
            lodging_official_product_id = f"offer:{offer_id}"
        elif property_id and rate_plan_id:
            lodging_official_product_id = (
                f"property:{property_id}|rate:{rate_plan_id}"
            )
        product = {
            **base,
            "official_product_id": _identifier(lodging_official_product_id),
            # Human-readable fields remain in fallback fingerprints but are
            # excluded when official property+room identity is available.
            "property_name": (
                None
                if lodging_official_product_id is not None
                else _text(quote.property_name)
            ),
            "provider_property_id": property_id,
            "provider_room_id": room_id,
            "room_name": (
                None if quote.provider_room_id is not None else _optional_text(quote.room_name)
            ),
            "bed_type": (
                None if quote.provider_room_id is not None else _optional_text(quote.bed_type)
            ),
            "area": quote.area.value,
            "place_key": quote.place_key.value if quote.place_key is not None else None,
            "check_in": quote.check_in.isoformat(),
            "check_out": quote.check_out.isoformat(),
            "adults": quote.adults,
            "rooms": quote.rooms,
        }
        if lodging_official_product_id is not None:
            return (
                product,
                OfferIdentitySource.PROVIDER_OFFICIAL_ID,
                OfferIdentityConfidence.HIGH,
                False,
                (),
                lodging_official_product_id,
            )
        reasons: list[str] = []
        if quote.room_name is None:
            reasons.append("room_name_missing")
        source = (
            OfferIdentitySource.HYBRID
            if quote.provider_property_id is not None
            else OfferIdentitySource.SEMANTIC_FINGERPRINT
        )
        return (
            product,
            source,
            OfferIdentityConfidence.MEDIUM if quote.room_name else OfferIdentityConfidence.LOW,
            quote.room_name is None,
            tuple(reasons),
            None,
        )
    if isinstance(quote, TransferOption):
        product = {
            **base,
            "origin_area": quote.origin_area.value,
            "destination_area": quote.destination_area.value,
            "origin_place_key": (
                quote.origin_place_key.value if quote.origin_place_key is not None else None
            ),
            "destination_place_key": (
                quote.destination_place_key.value
                if quote.destination_place_key is not None
                else None
            ),
            "service_date": quote.service_date.isoformat(),
            "schedule_mode": quote.schedule_mode.value,
            "duration_minutes": quote.duration_minutes,
            "depart_at": _timestamp(quote.depart_at),
            "arrive_at": _timestamp(quote.arrive_at),
            "service_window_start_at": _timestamp(quote.service_window_start_at),
            "service_window_end_at": _timestamp(quote.service_window_end_at),
            "adults": quote.adults,
        }
        return (
            product,
            OfferIdentitySource.SEMANTIC_FINGERPRINT,
            OfferIdentityConfidence.MEDIUM,
            False,
            (),
            None,
        )
    raise TypeError(f"unsupported package quote: {type(quote).__name__}")


def _terms_payload(quote: PackageQuote) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {
        "currency": quote.currency,
        "taxes_and_fees_included": quote.taxes_and_fees_included,
    }
    if isinstance(quote, NormalizedFlightQuote):
        return {
            **base,
            "checked_baggage_per_adult_kg": quote.checked_baggage_per_adult_kg,
            "party_availability_confirmed": quote.party_availability_confirmed,
            "cabin_class": _optional_text(quote.cabin_class),
            "fare_basis_codes": _text_tuple(quote.fare_basis_codes),
            "fare_rule_summary": _optional_text(quote.fare_rule_summary),
        }
    if isinstance(quote, NormalizedLodgingQuote):
        return {
            **base,
            "breakfast_included": quote.breakfast_included,
            "provider_rate_plan_id": _identifier(quote.provider_rate_plan_id),
            "cancellation_policy": _optional_text(quote.cancellation_policy),
            "payment_policy": _optional_text(quote.payment_policy),
        }
    if isinstance(quote, TransferOption):
        return {
            **base,
            "operates_24_hours": quote.operates_24_hours,
            "requires_reservation": quote.requires_reservation,
            "price_scope": quote.price_scope.value,
            "purchase_scope": quote.purchase_scope.value,
            "price_guarantee": quote.price_guarantee.value,
            "bound_lodging_id": quote.bound_lodging_id,
        }
    raise TypeError(f"unsupported package quote: {type(quote).__name__}")


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _optional_text(value: str | None) -> str | None:
    return _text(value) if value is not None else None


def _identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return unicodedata.normalize("NFKC", value).strip()


def _clean_identifier(value: str | None) -> str | None:
    normalized = _identifier(value)
    return normalized or None


def _text_tuple(values: tuple[str, ...]) -> list[JsonValue]:
    return [_text(value) for value in values]


def _official_offer_id(quote: PackageQuote) -> str | None:
    offer_id = _clean_identifier(quote.provider_offer_id)
    if offer_id is not None:
        return offer_id
    if isinstance(quote, NormalizedLodgingQuote):
        return _clean_identifier(quote.provider_rate_plan_id)
    return None


def _semantic_offer_is_complete(
    quote: PackageQuote,
    *,
    product_ambiguous: bool,
) -> bool:
    if product_ambiguous:
        return False
    if isinstance(quote, NormalizedFlightQuote):
        return bool(
            quote.cabin_class
            and (quote.fare_basis_codes or quote.fare_rule_summary)
        )
    if isinstance(quote, NormalizedLodgingQuote):
        # Payment mode is included whenever visible, but some providers do not
        # expose it on the result surface.  Room + cancellation + meal scope is
        # therefore a medium-confidence (not high-confidence) semantic offer.
        return quote.cancellation_policy is not None and quote.breakfast_included is not None
    return False


def _product_comparison_is_ambiguous(
    before: OfferIdentity,
    after: OfferIdentity,
    *,
    keys_equal: bool,
) -> bool:
    if keys_equal:
        return before.product_ambiguous or after.product_ambiguous
    if (
        before.official_product_id is not None
        and after.official_product_id is not None
    ):
        return False
    if (
        before.product_payload.get("provider") != after.product_payload.get("provider")
        or before.product_payload.get("quote_type")
        != after.product_payload.get("quote_type")
    ):
        return False
    # A semantic fingerprint with missing official fields can change because a
    # label was reformatted.  It is not safe to call that a confirmed different
    # product, so SOLD_OUT must not silently consume it as a replacement.
    return before.product_ambiguous or after.product_ambiguous


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _digest(payload: dict[str, JsonValue]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "OfferIdentity",
    "OfferIdentityConfidence",
    "OfferIdentitySource",
    "OfferSemanticChange",
    "OfferSemanticDiff",
    "same_stable_offer",
    "semantic_offer_diff",
    "stable_offer_identity",
]
