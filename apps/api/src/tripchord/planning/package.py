from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Self
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import DomainModel
from tripchord.domain.preferences import PreferenceMode


class PackageArea(StrEnum):
    AIRPORT = "airport"
    AIRPORT_ISLAND = "airport_island"
    DESTINATION_ISLAND = "destination_island"


class PackagePlaceKey(StrEnum):
    VELANA_AIRPORT = "velana_airport"
    HULHUMALE = "hulhumale"
    MAAFUSHI = "maafushi"


class QuoteAvailability(StrEnum):
    AVAILABLE = "available"
    SOLD_OUT = "sold_out"
    COMPARISON_ONLY = "comparison_only"


class PackageCandidateKind(StrEnum):
    CONTINUOUS_ISLAND = "continuous_island"
    CONTINUOUS_AIRPORT_ISLAND = "continuous_airport_island"
    SPLIT_AIRPORT_ISLAND = "split_airport_island"


class LodgingQualityTier(StrEnum):
    SEA_VIEW = "sea_view"
    BALCONY = "balcony"
    DELUXE = "deluxe"
    STANDARD = "standard"
    BASIC = "basic"


class LodgingLocationConvenience(StrEnum):
    """Evidence-backed assessment; a place or property name is never sufficient."""

    CONFIRMED_NOT_REMOTE = "confirmed_not_remote"
    UNKNOWN = "unknown"


class TransferScheduleMode(StrEnum):
    EXACT_DEPARTURE = "exact_departure"
    SERVICE_WINDOW = "service_window"


class TransferPriceScope(StrEnum):
    ONE_WAY = "one_way"
    ROUND_TRIP = "round_trip"


class TransferPurchaseScope(StrEnum):
    HOTEL_BOUND = "hotel_bound"
    PUBLIC_INDEPENDENT = "public_independent"


class TransferPriceGuarantee(StrEnum):
    ALL_IN_CONFIRMED = "all_in_confirmed"
    PUBLISHED_BASE_FARE = "published_base_fare"


class PackageViolationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class PackageViolationCode(StrEnum):
    DATE_MISMATCH = "date_mismatch"
    PARTY_MISMATCH = "party_mismatch"
    PARTY_AVAILABILITY_UNCONFIRMED = "party_availability_unconfirmed"
    TRANSFER_AVAILABILITY_UNCONFIRMED = "transfer_availability_unconfirmed"
    CURRENCY_MISMATCH = "currency_mismatch"
    TOTAL_MISMATCH = "total_mismatch"
    TAXES_INCOMPLETE = "taxes_incomplete"
    STALE_QUOTE = "stale_quote"
    SOLD_OUT = "sold_out"
    MISSING_EVIDENCE = "missing_evidence"
    QUOTE_CAPTURE_SKEW = "quote_capture_skew"
    LODGING_STRUCTURE_MISMATCH = "lodging_structure_mismatch"
    LODGING_NIGHT_COVERAGE = "lodging_night_coverage"
    TRANSFER_PRICE_CONTRACT_INVALID = "transfer_price_contract_invalid"
    TRANSFER_CHAIN_INCOMPLETE = "transfer_chain_incomplete"
    TRANSFER_BINDING_MISMATCH = "transfer_binding_mismatch"
    TRANSFER_PLACE_MISMATCH = "transfer_place_mismatch"
    TRANSFER_CONNECTION_INFEASIBLE = "transfer_connection_infeasible"
    LATE_ARRIVAL_BOAT_RISK = "late_arrival_boat_risk"
    EARLY_DEPARTURE_BUFFER = "early_departure_buffer"
    PUBLISHED_BASE_FARE_NOT_ALL_IN = "published_base_fare_not_all_in"
    BUDGET_NOT_FULLY_VERIFIED = "budget_not_fully_verified"
    BAGGAGE_PREFERENCE = "baggage_preference"
    CONNECTION_PREFERENCE = "connection_preference"
    BREAKFAST_PREFERENCE = "breakfast_preference"
    LODGING_QUALITY_PREFERENCE = "lodging_quality_preference"
    LODGING_LOCATION_PREFERENCE = "lodging_location_preference"
    BUDGET_EXCEEDED = "budget_exceeded"


class PackageDecisionState(StrEnum):
    ACCEPT = "accept"
    REJECT_AND_REPLAN = "reject_and_replan"
    HUMAN_BLOCK = "human_block"

    @property
    def chinese_label(self) -> str:
        return {
            self.ACCEPT: "接受",
            self.REJECT_AND_REPLAN: "拒绝并重新规划",
            self.HUMAN_BLOCK: "阻塞并转人工",
        }[self]


class PackageVerificationPhase(StrEnum):
    INITIAL = "initial"
    REVERIFICATION = "reverification"
    EVENT_REVERIFICATION = "event_reverification"


class PackagePreferenceApplicationState(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    HARD_CONSTRAINT = "hard_constraint"
    NOT_REQUESTED = "not_requested"


class PackageEventKind(StrEnum):
    PRICE_CHANGED = "price_changed"
    SOLD_OUT = "sold_out"


class NormalizedQuote(DomainModel):
    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    total_for_party_cents: int | None = Field(default=None, ge=0)
    taxes_and_fees_included: bool | None
    captured_at: datetime
    expires_at: datetime
    availability: QuoteAvailability = QuoteAvailability.AVAILABLE
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    # Provider-owned identifiers are deliberately separate from ``id``.  ``id``
    # identifies one captured observation and can change whenever visible DOM or
    # evidence changes; a provider offer id, when actually exposed, is allowed to
    # participate in cross-observation identity.
    provider_offer_id: str | None = Field(default=None, min_length=1, max_length=500)
    reference_total_cents: int | None = Field(default=None, ge=0)
    reference_currency: str | None = Field(default=None, min_length=3, max_length=3)
    reference_rate_source: str | None = Field(default=None, min_length=1)
    reference_rate_date: date | None = None
    reference_usd_to_cny: Decimal | None = Field(default=None, gt=0)
    reference_rate_response_sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    reference_rate_captured_at: datetime | None = None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("captured_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("quote timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_freshness_window(self) -> Self:
        if self.expires_at <= self.captured_at:
            raise ValueError("quote expires_at must be after captured_at")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.captured_at <= reference < self.expires_at


class NormalizedFlightSegment(DomainModel):
    """One observed flight segment, including its airport-level identity."""

    flight_number: str = Field(min_length=2, max_length=12)
    departure_airport_code: str = Field(min_length=3, max_length=3)
    arrival_airport_code: str = Field(min_length=3, max_length=3)
    departure_at: datetime
    arrival_at: datetime

    @field_validator("departure_airport_code", "arrival_airport_code")
    @classmethod
    def normalize_airport_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("flight segment airport must be a three-letter IATA code")
        return normalized

    @field_validator("flight_number")
    @classmethod
    def normalize_flight_number(cls, value: str) -> str:
        normalized = value.strip().upper().replace(" ", "")
        if not re.fullmatch(r"[A-Z0-9]{2,12}", normalized):
            raise ValueError("flight segment number must be an explicit provider identifier")
        return normalized

    @field_validator("departure_at", "arrival_at")
    @classmethod
    def require_segment_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("flight segment timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_segment_interval(self) -> Self:
        if self.arrival_at <= self.departure_at:
            raise ValueError("flight segment arrival must be after departure")
        return self


class FlightGroundTransferContract(DomainModel):
    """Explicit contract for a connection that changes airports."""

    from_airport_code: str = Field(min_length=3, max_length=3)
    to_airport_code: str = Field(min_length=3, max_length=3)
    mode: str = Field(min_length=1, max_length=80)
    minimum_buffer_minutes: int = Field(ge=1, le=2_880)
    actual_buffer_minutes: int = Field(ge=0, le=2_880)
    baggage_recheck_required: bool
    through_ticket_protected: bool
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @field_validator("from_airport_code", "to_airport_code")
    @classmethod
    def normalize_transfer_airport_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise ValueError("ground transfer airport must be a three-letter IATA code")
        return normalized

    @model_validator(mode="after")
    def validate_ground_transfer(self) -> Self:
        if self.from_airport_code == self.to_airport_code:
            raise ValueError("ground transfer contract must connect different airports")
        if self.actual_buffer_minutes < self.minimum_buffer_minutes:
            raise ValueError("ground transfer buffer is below the required minimum")
        if not self.mode.strip():
            raise ValueError("ground transfer mode is required")
        return self


class NormalizedFlightQuote(NormalizedQuote):
    total_for_party_cents: int | None = Field(default=None, ge=0)
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    party_availability_confirmed: bool = True
    # A visible per-adult fare may describe the route and availability without
    # proving the requested party total.  Keep that amount for comparison, but
    # never let it enter a package total unless this flag is true.
    party_total_known: bool = True
    price_basis: str = "total_party"
    # Preserve a visible provider amount for comparison without representing it
    # as a party total when the provider contract did not prove one.
    display_amount_cents: int | None = Field(default=None, ge=0)
    outbound_depart_at: datetime
    outbound_arrive_at: datetime
    return_depart_at: datetime
    return_arrive_at: datetime
    checked_baggage_per_adult_kg: int | None = Field(default=None, ge=0, le=100)
    provider_itinerary_id: str | None = Field(default=None, min_length=1, max_length=500)
    outbound_flight_numbers: tuple[str, ...] = ()
    return_flight_numbers: tuple[str, ...] = ()
    outbound_segments: tuple[NormalizedFlightSegment, ...] = ()
    return_segments: tuple[NormalizedFlightSegment, ...] = ()
    outbound_ground_transfers: tuple[FlightGroundTransferContract, ...] = ()
    return_ground_transfers: tuple[FlightGroundTransferContract, ...] = ()
    origin_airport_code: str | None = Field(default=None, min_length=3, max_length=3)
    destination_airport_code: str | None = Field(default=None, min_length=3, max_length=3)
    carrier_summary: str | None = Field(default=None, min_length=1, max_length=1000)
    cabin_class: str | None = Field(default=None, min_length=1, max_length=100)
    fare_basis_codes: tuple[str, ...] = ()
    fare_rule_summary: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator(
        "outbound_depart_at",
        "outbound_arrive_at",
        "return_depart_at",
        "return_arrive_at",
    )
    @classmethod
    def require_flight_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("flight timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_flight_intervals(self) -> Self:
        if self.outbound_arrive_at <= self.outbound_depart_at:
            raise ValueError("outbound arrival must be after departure")
        if self.return_arrive_at <= self.return_depart_at:
            raise ValueError("return arrival must be after departure")
        if self.return_depart_at <= self.outbound_arrive_at:
            raise ValueError("return departure must be after outbound arrival")
        return self

    @property
    def has_publishable_execution_contract(self) -> bool:
        """Whether this quote is safe to put into a user-facing package.

        A visible comparison price is not a purchasable flight identity.  The
        package layer therefore requires a proven party total and a complete
        segment/airport contract before it can select or rank the quote.
        """

        if (
            not self.party_total_known
            or self.total_for_party_cents is None
            or self.total_for_party_cents <= 0
        ):
            return False
        if self.availability != QuoteAvailability.AVAILABLE:
            return False
        if not self.origin_airport_code or not self.destination_airport_code:
            return False
        summary_only_same_product = (
            not self.outbound_segments
            and not self.return_segments
            and not self.outbound_ground_transfers
            and not self.return_ground_transfers
            and bool(self.outbound_flight_numbers)
            and bool(self.return_flight_numbers)
            and any(
                reference.startswith("flight-party-comparison:sha256:")
                for reference in self.evidence_refs
            )
            and "flight-segments:summary-only-not-expanded" in self.evidence_refs
        )
        if summary_only_same_product:
            # Some provider cards expose a stable complete round-trip product,
            # all flight numbers, exact endpoints and four whole-journey times
            # without expanding each connection.  A server-owned 1/N receipt
            # may rank that product, but its evidence marker keeps the missing
            # segment detail visible; no intermediate airport or time is
            # invented here.
            return True
        for (
            numbers,
            segments,
            ground_transfers,
            start,
            end,
            expected_departure,
            expected_arrival,
        ) in (
            (
                self.outbound_flight_numbers,
                self.outbound_segments,
                self.outbound_ground_transfers,
                self.outbound_depart_at,
                self.outbound_arrive_at,
                self.origin_airport_code,
                self.destination_airport_code,
            ),
            (
                self.return_flight_numbers,
                self.return_segments,
                self.return_ground_transfers,
                self.return_depart_at,
                self.return_arrive_at,
                self.destination_airport_code,
                self.origin_airport_code,
            ),
        ):
            if not numbers or len(numbers) != len(segments):
                return False
            if not segments or segments[0].departure_airport_code != expected_departure:
                return False
            if segments[-1].arrival_airport_code != expected_arrival:
                return False
            if segments[0].departure_at != start or segments[-1].arrival_at != end:
                return False
            if tuple(segment.flight_number for segment in segments) != tuple(numbers):
                return False
            for previous, current in pairwise(segments):
                if previous.arrival_airport_code != current.departure_airport_code:
                    matching_transfers = tuple(
                        item
                        for item in ground_transfers
                        if item.from_airport_code == previous.arrival_airport_code
                        and item.to_airport_code == current.departure_airport_code
                    )
                    if len(matching_transfers) != 1:
                        return False
                    transfer = matching_transfers[0]
                    if (
                        not transfer.through_ticket_protected
                        or transfer.actual_buffer_minutes < transfer.minimum_buffer_minutes
                        or transfer.actual_buffer_minutes
                        != int((current.departure_at - previous.arrival_at).total_seconds() // 60)
                    ):
                        return False
                elif ground_transfers:
                    return False
                if current.departure_at < previous.arrival_at:
                    return False
        return True


class NormalizedLodgingQuote(NormalizedQuote):
    total_for_party_cents: int = Field(ge=0)
    property_name: str = Field(min_length=1)
    area: PackageArea
    check_in: date
    check_out: date
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    rooms: int = Field(ge=1, le=8)
    breakfast_included: bool | None = None
    place_key: PackagePlaceKey | None = None
    provider_property_id: str | None = Field(default=None, min_length=1, max_length=500)
    provider_room_id: str | None = Field(default=None, min_length=1, max_length=500)
    provider_rate_plan_id: str | None = Field(default=None, min_length=1, max_length=500)
    room_name: str | None = Field(default=None, min_length=1, max_length=1000)
    bed_type: str | None = Field(default=None, min_length=1, max_length=500)
    cancellation_policy: str | None = Field(default=None, min_length=1, max_length=4000)
    payment_policy: str | None = Field(default=None, min_length=1, max_length=1000)
    location_address: str | None = Field(default=None, min_length=1, max_length=1000)
    nearby_location_evidence: tuple[str, ...] = ()
    location_convenience: LodgingLocationConvenience = LodgingLocationConvenience.UNKNOWN

    @model_validator(mode="after")
    def validate_stay(self) -> Self:
        if self.check_out <= self.check_in:
            raise ValueError("lodging checkout must be after checkin")
        if self.area == PackageArea.AIRPORT:
            raise ValueError("lodging cannot use the airport terminal as its area")
        if self.place_key == PackagePlaceKey.HULHUMALE and self.area != PackageArea.AIRPORT_ISLAND:
            raise ValueError("Hulhumalé lodging must use airport_island area")
        if (
            self.place_key == PackagePlaceKey.MAAFUSHI
            and self.area != PackageArea.DESTINATION_ISLAND
        ):
            raise ValueError("Maafushi lodging must use destination_island area")
        if self.place_key == PackagePlaceKey.VELANA_AIRPORT:
            raise ValueError("lodging cannot use Velana Airport as its place")
        if len(self.nearby_location_evidence) > 8:
            raise ValueError("lodging nearby-location evidence is limited to eight items")
        if any(not item.strip() or len(item) > 1000 for item in self.nearby_location_evidence):
            raise ValueError("lodging nearby-location evidence must be bounded non-empty text")
        if (
            self.location_convenience == LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
            and not lodging_non_remote_evidence_confirmed(
                self.location_address,
                self.nearby_location_evidence,
            )
        ):
            raise ValueError(
                "confirmed non-remote lodging requires an explicit address and visible "
                "proximity to a service, commercial, or transport facility"
            )
        return self

    @property
    def night_count(self) -> int:
        return (self.check_out - self.check_in).days


_BASIC_LODGING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("windowless", re.compile(r"无窗|windowless|\bno[ -]?window\b", re.IGNORECASE)),
    ("basic", re.compile(r"基础(?:房|客房|房型)?|\bbasic(?: room)?\b", re.IGNORECASE)),
    ("economy", re.compile(r"经济(?:房|客房|房型)?|特价房|\beconomy room\b", re.IGNORECASE)),
    (
        "shared_or_dormitory",
        re.compile(r"床位|宿舍|公共卫浴|\bdormitory\b|\bshared bathroom\b", re.IGNORECASE),
    ),
)

_EXPLICIT_LODGING_ADDRESS_PATTERN = re.compile(
    r"\b(?:road|street|avenue|lane|drive|boulevard|highway|magu|hingun)\b|"
    r"(?:路|街|道|大道|巷|弄|号)",
    re.IGNORECASE,
)
_NEARBY_PROXIMITY_PATTERN = re.compile(
    r"(?:^|[\s,.;·])(?:近|靠近|邻近|附近|near(?:by)?|close\s+to|next\s+to|"
    r"walking\s+distance|on[- ]?site)",
    re.IGNORECASE,
)
_NEARBY_SERVICE_PATTERN = re.compile(
    r"潜水|水上(?:活动|运动)|餐厅|咖啡|商店|市场|超市|商场|码头|港口|渡轮|"
    r"公交|车站|医院|诊所|药房|银行|"
    r"\b(?:dive|diving|water\s+sports?|restaurant|caf[eé]|shop|market|supermarket|"
    r"mall|ferry|terminal|jetty|harbo(?:u)?r|bus\s+stop|station|hospital|clinic|"
    r"pharmacy|bank|atm)\b",
    re.IGNORECASE,
)


def lodging_non_remote_evidence_confirmed(
    address: str | None,
    nearby_evidence: tuple[str, ...],
) -> bool:
    """Require explicit address plus visible proximity to real-world services.

    Exact island/place binding is intentionally absent: it proves the requested
    search area, not whether a property is remote within that area.  Beaches and
    other natural landmarks alone likewise do not satisfy this contract.
    """

    if address is None or not _EXPLICIT_LODGING_ADDRESS_PATTERN.search(address):
        return False
    return any(
        _NEARBY_PROXIMITY_PATTERN.search(item) and _NEARBY_SERVICE_PATTERN.search(item)
        for item in nearby_evidence
    )


def lodging_basic_markers(lodging: NormalizedLodgingQuote) -> tuple[str, ...]:
    """Return generic room-quality markers; provider names are never special-cased."""

    searchable = " ".join(
        value.strip() for value in (lodging.property_name, lodging.room_name or "") if value.strip()
    )
    return tuple(name for name, pattern in _BASIC_LODGING_PATTERNS if pattern.search(searchable))


def lodging_quality_tier(lodging: NormalizedLodgingQuote) -> LodgingQualityTier:
    if lodging_basic_markers(lodging):
        return LodgingQualityTier.BASIC
    searchable = f"{lodging.property_name} {lodging.room_name or ''}".casefold()
    if any(term in searchable for term in ("海景", "sea view", "seaview", "ocean view")):
        return LodgingQualityTier.SEA_VIEW
    if any(term in searchable for term in ("阳台", "balcony")):
        return LodgingQualityTier.BALCONY
    if any(
        term in searchable for term in ("超级豪华", "豪华", "高级", "deluxe", "superior", "premium")
    ):
        return LodgingQualityTier.DELUXE
    return LodgingQualityTier.STANDARD


def lodging_quality_rank(lodging: NormalizedLodgingQuote) -> int:
    return {
        LodgingQualityTier.SEA_VIEW: 0,
        LodgingQualityTier.BALCONY: 1,
        LodgingQualityTier.DELUXE: 2,
        LodgingQualityTier.STANDARD: 3,
        LodgingQualityTier.BASIC: 4,
    }[lodging_quality_tier(lodging)]


class TransferOption(NormalizedQuote):
    total_for_party_cents: int = Field(ge=0)
    origin_area: PackageArea
    destination_area: PackageArea
    origin_place_key: PackagePlaceKey | None = None
    destination_place_key: PackagePlaceKey | None = None
    adults: int = Field(ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    service_date: date
    schedule_mode: TransferScheduleMode
    duration_minutes: int = Field(gt=0, le=1440)
    depart_at: datetime | None = None
    arrive_at: datetime | None = None
    service_window_start_at: datetime | None = None
    service_window_end_at: datetime | None = None
    operates_24_hours: bool
    requires_reservation: bool | None = None
    price_scope: TransferPriceScope
    price_contract_id: str = Field(min_length=1)
    purchase_scope: TransferPurchaseScope
    price_guarantee: TransferPriceGuarantee = TransferPriceGuarantee.ALL_IN_CONFIRMED
    bound_lodging_id: str | None = None
    contract_evidence_text: str = Field(min_length=1)
    detail_url: str = Field(min_length=1)

    @field_validator(
        "depart_at",
        "arrive_at",
        "service_window_start_at",
        "service_window_end_at",
    )
    @classmethod
    def require_transfer_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("transfer timestamps must be timezone-aware")
        return value

    @field_validator("detail_url")
    @classmethod
    def require_https_detail_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("transfer detail_url must be a safe HTTPS URL")
        return value

    @model_validator(mode="after")
    def validate_transfer(self) -> Self:
        if self.origin_area == self.destination_area:
            raise ValueError("transfer origin and destination must differ")
        expected_areas = {
            PackagePlaceKey.VELANA_AIRPORT: PackageArea.AIRPORT,
            PackagePlaceKey.HULHUMALE: PackageArea.AIRPORT_ISLAND,
            PackagePlaceKey.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
        }
        if (
            self.origin_place_key is not None
            and expected_areas[self.origin_place_key] != self.origin_area
        ):
            raise ValueError("transfer origin place does not match its area")
        if (
            self.destination_place_key is not None
            and expected_areas[self.destination_place_key] != self.destination_area
        ):
            raise ValueError("transfer destination place does not match its area")
        if (
            self.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
            and self.taxes_and_fees_included is not True
        ):
            raise ValueError("all-in transfer price requires confirmed taxes and fees")
        if (
            self.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
            and self.taxes_and_fees_included is True
        ):
            raise ValueError("published base fare cannot claim confirmed taxes and fees")
        if self.purchase_scope == TransferPurchaseScope.HOTEL_BOUND and not self.bound_lodging_id:
            raise ValueError("hotel-bound transfer requires bound_lodging_id")
        if (
            self.purchase_scope == TransferPurchaseScope.PUBLIC_INDEPENDENT
            and self.bound_lodging_id is not None
        ):
            raise ValueError("public transfer cannot be bound to a lodging quote")
        duration = timedelta(minutes=self.duration_minutes)
        if self.schedule_mode == TransferScheduleMode.EXACT_DEPARTURE:
            if (
                self.depart_at is None
                or self.arrive_at is None
                or self.service_window_start_at is not None
                or self.service_window_end_at is not None
            ):
                raise ValueError(
                    "exact transfer requires depart_at/arrive_at and forbids service windows"
                )
            if self.operates_24_hours:
                raise ValueError("an exact departure cannot be marked as 24-hour service")
            if self.depart_at.date() != self.service_date:
                raise ValueError("exact transfer departure date must equal service_date")
            if self.arrive_at - self.depart_at != duration:
                raise ValueError(
                    "exact transfer duration must match its visible departure/arrival interval"
                )
        else:
            if (
                self.service_window_start_at is None
                or self.service_window_end_at is None
                or self.depart_at is not None
                or self.arrive_at is not None
            ):
                raise ValueError(
                    "window transfer requires service-window bounds and forbids exact timestamps"
                )
            if (
                self.service_window_start_at.date() != self.service_date
                or self.service_window_end_at.date() != self.service_date
            ):
                raise ValueError("transfer service-window bounds must use service_date")
            if self.service_window_end_at < self.service_window_start_at:
                raise ValueError("transfer service-window end must not precede its start")
            if self.operates_24_hours and (
                self.service_window_start_at.hour != 0
                or self.service_window_start_at.minute != 0
                or self.service_window_end_at.hour != 23
                or self.service_window_end_at.minute != 59
            ):
                raise ValueError("24-hour transfer evidence must expose a 00:00-23:59 window")
        return self

    @property
    def travel_date(self) -> date:
        return self.service_date

    @property
    def earliest_departure_at(self) -> datetime:
        if self.depart_at is not None:
            return self.depart_at
        assert self.service_window_start_at is not None
        return self.service_window_start_at

    @property
    def latest_departure_at(self) -> datetime:
        if self.depart_at is not None:
            return self.depart_at
        assert self.service_window_end_at is not None
        return self.service_window_end_at

    @property
    def earliest_arrival_at(self) -> datetime:
        if self.arrive_at is not None:
            return self.arrive_at
        return self.earliest_departure_at + timedelta(minutes=self.duration_minutes)

    @property
    def latest_arrival_at(self) -> datetime:
        if self.arrive_at is not None:
            return self.arrive_at
        return self.latest_departure_at + timedelta(minutes=self.duration_minutes)

    def has_feasible_departure(
        self,
        *,
        not_before: datetime | None = None,
        arrive_by: datetime | None = None,
    ) -> bool:
        earliest = self.earliest_departure_at
        latest = self.latest_departure_at
        if not_before is not None:
            earliest = max(earliest, not_before)
        if arrive_by is not None:
            latest = min(
                latest,
                arrive_by - timedelta(minutes=self.duration_minutes),
            )
        return earliest <= latest


def transfer_binding_error(
    transfer: TransferOption,
    lodgings: tuple[NormalizedLodgingQuote, ...],
) -> str | None:
    if transfer.purchase_scope == TransferPurchaseScope.PUBLIC_INDEPENDENT:
        return None
    lodging = next(
        (item for item in lodgings if item.id == transfer.bound_lodging_id),
        None,
    )
    if lodging is None:
        return "酒店专属接驳未绑定到整包中的住宿报价"
    arrives_at_bound_lodging = (
        transfer.destination_area == lodging.area and transfer.service_date == lodging.check_in
    )
    leaves_bound_lodging = (
        transfer.origin_area == lodging.area and transfer.service_date == lodging.check_out
    )
    if not (arrives_at_bound_lodging or leaves_bound_lodging):
        return "酒店专属接驳的方向或服务日期与绑定住宿不一致"
    return None


def transfer_place_error(
    intent: PackageIntent,
    transfer: TransferOption,
    lodgings: tuple[NormalizedLodgingQuote, ...],
) -> str | None:
    expected_by_area: dict[PackageArea, PackagePlaceKey] = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
    }
    for area in (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT_ISLAND):
        place_keys = {
            lodging.place_key
            for lodging in lodgings
            if lodging.area == area and lodging.place_key is not None
        }
        if len(place_keys) == 1:
            expected_by_area[area] = next(iter(place_keys))
    for area, place_key in (
        (transfer.origin_area, transfer.origin_place_key),
        (transfer.destination_area, transfer.destination_place_key),
    ):
        expected_place = expected_by_area.get(area)
        if expected_place is None:
            return "接驳端点无法绑定到整包中的确定地点"
        if place_key != expected_place:
            return "接驳端点地点与整包住宿或机场身份不一致"
    if transfer.price_guarantee != TransferPriceGuarantee.PUBLISHED_BASE_FARE:
        return None
    if intent.destination_place_key not in {None, PackagePlaceKey.MAAFUSHI}:
        return "iCom 公开班次只允许用于明确指定 Maafushi 的行程"
    destination_lodgings = tuple(
        lodging for lodging in lodgings if lodging.area == PackageArea.DESTINATION_ISLAND
    )
    if not destination_lodgings or any(
        lodging.place_key != PackagePlaceKey.MAAFUSHI for lodging in destination_lodgings
    ):
        return "iCom 公开班次不能绑定到未确认位于 Maafushi 的住宿"
    expected = {
        PackageArea.AIRPORT: PackagePlaceKey.VELANA_AIRPORT,
        PackageArea.DESTINATION_ISLAND: PackagePlaceKey.MAAFUSHI,
    }
    if transfer.origin_place_key != expected.get(
        transfer.origin_area
    ) or transfer.destination_place_key != expected.get(transfer.destination_area):
        return "iCom 公开班次的机场或 Maafushi 地点身份不完整"
    return None


type PackageQuote = NormalizedFlightQuote | NormalizedLodgingQuote | TransferOption


def transfer_contract_total_cents(
    transfers: tuple[TransferOption, ...],
    *,
    currency: str | None = None,
) -> int:
    contracts: dict[str, int] = {}
    for transfer in transfers:
        if transfer.price_guarantee != TransferPriceGuarantee.ALL_IN_CONFIRMED:
            continue
        if currency is not None and transfer.currency != currency:
            continue
        contracts.setdefault(
            transfer.price_contract_id,
            transfer.total_for_party_cents,
        )
    return sum(contracts.values())


class PackageIntent(DomainModel):
    trip_id: str = Field(min_length=1)
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    destination_place_key: PackagePlaceKey | None = None
    start_date: date
    end_date: date
    latest_arrival_date: date | None = None
    adults: int = Field(default=2, ge=1, le=20)
    children: int = Field(default=0, ge=0, le=20)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=10)
    rooms: int = Field(default=1, ge=1, le=8)
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
    minimum_transfer_connection_minutes: int = Field(default=30, ge=0, le=1440)
    maximum_quote_capture_skew_minutes: int = Field(default=20, ge=1, le=180)

    @field_validator("currency")
    @classmethod
    def normalize_intent_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        from tripchord.domain.trip import TravelParty

        TravelParty(
            adults=self.adults,
            children=self.children,
            children_ages=self.children_ages,
            infants=self.infants,
            rooms=self.rooms,
        )
        if self.end_date <= self.start_date:
            raise ValueError("package end_date must be after start_date")
        mode = self.breakfast_preference_mode
        weight = self.breakfast_preference_weight
        if mode is None:
            if weight is not None:
                raise ValueError("breakfast_preference_weight requires breakfast_preference_mode")
            return self
        if weight is None:
            raise ValueError("breakfast_preference_mode requires breakfast_preference_weight")
        canonical_weight = {
            PreferenceMode.REQUIRED: 1.0,
            PreferenceMode.FORBIDDEN: 1.0,
            PreferenceMode.INDIFFERENT: 0.0,
        }.get(mode)
        if canonical_weight is not None and weight != canonical_weight:
            raise ValueError(
                f"{mode.value} breakfast mode requires canonical weight {canonical_weight:g}"
            )
        hard_value = {
            PreferenceMode.REQUIRED: True,
            PreferenceMode.FORBIDDEN: False,
        }.get(mode)
        if hard_value is not None and self.require_breakfast is not hard_value:
            raise ValueError(f"{mode.value} breakfast mode conflicts with require_breakfast")
        if (
            mode in {PreferenceMode.WEIGHTED, PreferenceMode.INDIFFERENT}
            and self.require_breakfast is not None
        ):
            raise ValueError(f"{mode.value} breakfast mode must not create a hard constraint")
        return self

    @property
    def night_count(self) -> int:
        return (self.end_date - self.start_date).days


def lodging_reference_cny_if_comparable(
    lodging: NormalizedLodgingQuote,
    intent_currency: str,
) -> int | None:
    """Return a CNY comparison amount only with a complete FX evidence contract."""

    if lodging.taxes_and_fees_included is not True:
        return None

    if lodging.currency == intent_currency:
        return lodging.total_for_party_cents if lodging.total_for_party_cents > 0 else None
    response_sha = lodging.reference_rate_response_sha256
    if (
        lodging.reference_total_cents is None
        or lodging.reference_total_cents <= 0
        or lodging.reference_currency != "CNY"
        or not lodging.reference_rate_source
        or lodging.reference_rate_date is None
        or lodging.reference_usd_to_cny is None
        or lodging.reference_usd_to_cny <= 0
        or response_sha is None
        or len(response_sha) != 64
        or any(char not in "0123456789abcdef" for char in response_sha)
        or lodging.reference_rate_captured_at is None
    ):
        return None
    return lodging.reference_total_cents


def lodging_is_comparison_eligible(
    lodging: NormalizedLodgingQuote,
    intent: PackageIntent,
    *,
    allow_reference_currency: bool = False,
) -> bool:
    """Return whether a quote may support a same-hard-contract comparison claim.

    Candidate generation deliberately keeps some repairable mismatches (for
    example breakfast) so the repair audit can replace them.  Comparison
    coverage is stricter: only a quote already satisfying every final lodging
    contract may count toward a lowest-qualified-price claim.
    """

    return (
        lodging.availability == QuoteAvailability.AVAILABLE
        and (
            lodging.currency == intent.currency
            or (
                allow_reference_currency
                and lodging_reference_cny_if_comparable(lodging, intent.currency) is not None
            )
        )
        and lodging.taxes_and_fees_included is True
        and lodging.adults == intent.adults
        and lodging.children == intent.children
        and lodging.children_ages == intent.children_ages
        and lodging.infants == intent.infants
        and lodging.rooms == intent.rooms
        and (
            intent.require_breakfast is None
            or lodging.breakfast_included is intent.require_breakfast
        )
        and (not intent.require_non_basic_lodging or not lodging_basic_markers(lodging))
        and (
            not intent.require_non_remote_lodging
            or (
                lodging.location_convenience == LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
                and lodging_non_remote_evidence_confirmed(
                    lodging.location_address,
                    lodging.nearby_location_evidence,
                )
            )
        )
    )


def lodging_is_segment_comparison_eligible(
    lodging: NormalizedLodgingQuote,
    intent: PackageIntent,
    *,
    area: PackageArea,
    check_in: date,
    check_out: date,
    exact_place_key: PackagePlaceKey | None = None,
    allow_reference_currency: bool = False,
) -> bool:
    """Match one quote to the exact segment and final hard comparison contract."""

    if not lodging_is_comparison_eligible(
        lodging, intent, allow_reference_currency=allow_reference_currency
    ):
        return False
    if lodging.area != area or lodging.check_in != check_in or lodging.check_out != check_out:
        return False
    if exact_place_key is not None:
        return lodging.place_key == exact_place_key
    return (
        intent.destination_place_key is None
        or (
            intent.destination_place_key == PackagePlaceKey.MAAFUSHI
            and area != PackageArea.DESTINATION_ISLAND
        )
        or (
            intent.destination_place_key == PackagePlaceKey.HULHUMALE
            and area != PackageArea.AIRPORT_ISLAND
        )
        or lodging.place_key == intent.destination_place_key
    )


class PackageInventory(DomainModel):
    flights: tuple[NormalizedFlightQuote, ...] = ()
    lodgings: tuple[NormalizedLodgingQuote, ...] = ()
    transfers: tuple[TransferOption, ...] = ()


_PACKAGE_CANDIDATE_POLICY_VERSION = "package-candidate-beam-v4"
_PACKAGE_CANDIDATE_SELECTION_POLICY_VERSION = "provider-flight-kind-reservation-v1"


class PackageCandidateGenerationAudit(DomainModel):
    """Proof that live candidate construction stayed inside an explicit envelope.

    The raw count is only a structural upper bound before transfer feasibility;
    it is deliberately not presented as a count of valid itineraries.  Live
    generation is a deterministic bounded prescreen, never a full-enumeration
    claim.
    """

    policy_version: str = _PACKAGE_CANDIDATE_POLICY_VERSION
    selection_policy_version: str = _PACKAGE_CANDIDATE_SELECTION_POLICY_VERSION
    raw_inventory_counts: dict[str, int]
    prescreened_inventory_counts: dict[str, int]
    raw_inventory_ids_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )
    prescreened_inventory_ids_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )
    raw_structural_candidate_upper_bound: int = Field(ge=0)
    prescreened_structural_candidate_upper_bound: int = Field(ge=0)
    generation_candidate_cap: int = Field(ge=1)
    transfer_beam_width: int = Field(ge=1)
    transfer_limit_per_contract_bucket: int = Field(ge=1)
    structurally_joined_candidate_count: int = Field(
        ge=0,
        description=(
            "Candidates built after the bounded structure scan and before final beam selection."
        ),
    )
    generated_candidate_count: int = Field(ge=0)
    generated_candidate_ids: tuple[str, ...]
    rejection_reasons: tuple[str, ...] = ()
    input_prescreen_pruned: bool
    generation_stopped_at_cap: bool = Field(
        description=(
            "Compatibility field: in v3 this means final beam selection was truncated at cap."
        )
    )
    prescreen_structure_scan_completed: bool = Field(
        description="Whether every bounded prescreen structure was visited."
    )
    transfer_combinations_exhaustively_enumerated: bool = False
    full_enumeration_claimed: bool = False
    omitted_scope: str = Field(min_length=1)
    generation_proof_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def forbid_full_enumeration_claim(self) -> Self:
        expected_inventory_keys = {"flights", "lodgings", "transfers"}
        if (
            set(self.raw_inventory_counts) != expected_inventory_keys
            or set(self.prescreened_inventory_counts) != expected_inventory_keys
        ):
            raise ValueError("candidate generation inventory counts require exact typed keys")
        if any(value < 0 for value in self.raw_inventory_counts.values()) or any(
            value < 0 for value in self.prescreened_inventory_counts.values()
        ):
            raise ValueError("candidate generation inventory counts cannot be negative")
        if any(
            self.prescreened_inventory_counts[key] > self.raw_inventory_counts[key]
            for key in expected_inventory_keys
        ):
            raise ValueError("prescreened inventory counts cannot exceed raw counts")
        if self.full_enumeration_claimed or self.transfer_combinations_exhaustively_enumerated:
            raise ValueError("bounded live generation cannot claim exhaustive enumeration")
        if self.policy_version != _PACKAGE_CANDIDATE_POLICY_VERSION:
            raise ValueError("candidate generation audit requires the current policy version")
        if self.selection_policy_version != _PACKAGE_CANDIDATE_SELECTION_POLICY_VERSION:
            raise ValueError("candidate generation audit requires the current selection policy")
        if self.generated_candidate_count > self.generation_candidate_cap:
            raise ValueError("generated candidates exceed the audited cap")
        if self.generated_candidate_count > self.structurally_joined_candidate_count:
            raise ValueError("generated candidates exceed the structurally joined pool")
        if (
            self.structurally_joined_candidate_count
            > self.prescreened_structural_candidate_upper_bound
        ):
            raise ValueError("structurally joined candidates exceed the prescreened upper bound")
        expected_generated_count = min(
            self.structurally_joined_candidate_count,
            self.generation_candidate_cap,
        )
        if self.generated_candidate_count != expected_generated_count:
            raise ValueError("generated candidate count conflicts with the audited selection cap")
        if self.generated_candidate_count != len(self.generated_candidate_ids):
            raise ValueError("generated candidate count must match the audited ids")
        if len(set(self.generated_candidate_ids)) != len(self.generated_candidate_ids):
            raise ValueError("audited generated candidate ids must be unique")
        if self.generated_candidate_count == 0 and not self.rejection_reasons:
            raise ValueError("empty candidate generation requires deterministic rejection reasons")
        if self.generated_candidate_count > 0 and self.rejection_reasons:
            raise ValueError(
                "successful candidate generation cannot carry global rejection reasons"
            )
        if (
            self.prescreened_structural_candidate_upper_bound
            > self.raw_structural_candidate_upper_bound
        ):
            raise ValueError("prescreened structural bound cannot exceed raw bound")
        if self.input_prescreen_pruned == (
            self.raw_inventory_counts == self.prescreened_inventory_counts
        ):
            raise ValueError("input prescreen flag conflicts with inventory counts")
        expected_truncated = (
            self.structurally_joined_candidate_count > self.generation_candidate_cap
        )
        if self.generation_stopped_at_cap != expected_truncated:
            raise ValueError("candidate selection truncation conflicts with the joined pool")
        if not self.prescreen_structure_scan_completed:
            raise ValueError("v3 generation must scan every bounded prescreen structure")
        return self


class TravelPackageCandidate(DomainModel):
    id: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    parent_candidate_id: str | None = None
    kind: PackageCandidateKind
    flight: NormalizedFlightQuote
    lodgings: tuple[NormalizedLodgingQuote, ...] = Field(min_length=1)
    transfers: tuple[TransferOption, ...]
    declared_total_cents: int = Field(ge=0)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    applied_event_ids: tuple[str, ...] = ()

    @field_validator("currency")
    @classmethod
    def normalize_candidate_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_component_ids(self) -> Self:
        ids = self.component_ids
        if len(ids) != len(set(ids)):
            raise ValueError("package component ids must be unique")

        # A package total is meaningful only when every component describes
        # the same traveller shape.  Adult-only legacy quotes remain valid
        # because the new fields default to zero.
        party = (self.flight.adults, self.flight.children, self.flight.infants)
        for quote in self.lodgings:
            if (quote.adults, quote.children, quote.infants) != party:
                raise ValueError("package components must use one shared traveller shape")
            if quote.children_ages != self.flight.children_ages:
                raise ValueError("package components must use one shared child-age shape")
        for transfer in self.transfers:
            if (transfer.adults, transfer.children, transfer.infants) != party:
                raise ValueError("package components must use one shared traveller shape")
            if transfer.children_ages != self.flight.children_ages:
                raise ValueError("package components must use one shared child-age shape")

        start_date = self.flight.outbound_arrive_at.date()
        end_date = self.flight.return_depart_at.date()
        if self.kind == PackageCandidateKind.CONTINUOUS_ISLAND:
            expected_lodgings = Counter({(PackageArea.DESTINATION_ISLAND, start_date, end_date): 1})
        elif self.kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND:
            expected_lodgings = Counter({(PackageArea.AIRPORT_ISLAND, start_date, end_date): 1})
        else:
            first_checkout = start_date + timedelta(days=1)
            last_checkin = end_date - timedelta(days=1)
            expected_lodgings = Counter(
                {
                    (PackageArea.AIRPORT_ISLAND, start_date, first_checkout): 1,
                    (
                        PackageArea.DESTINATION_ISLAND,
                        first_checkout,
                        last_checkin,
                    ): 1,
                    (PackageArea.AIRPORT_ISLAND, last_checkin, end_date): 1,
                }
            )
        actual_lodgings = Counter(
            (lodging.area, lodging.check_in, lodging.check_out) for lodging in self.lodgings
        )
        if actual_lodgings != expected_lodgings:
            raise ValueError("package lodging segments do not match candidate kind")

        contracts: dict[str, list[TransferOption]] = {}
        for transfer in self.transfers:
            contracts.setdefault(transfer.price_contract_id, []).append(transfer)
        for group in contracts.values():
            first = group[0]
            for transfer in group[1:]:
                if (
                    first.price_scope != transfer.price_scope
                    or first.purchase_scope != transfer.purchase_scope
                    or first.price_guarantee != transfer.price_guarantee
                    or first.bound_lodging_id != transfer.bound_lodging_id
                    or first.provider != transfer.provider
                    or first.currency != transfer.currency
                    or first.total_for_party_cents != transfer.total_for_party_cents
                    or first.taxes_and_fees_included != transfer.taxes_and_fees_included
                    or first.adults != transfer.adults
                ):
                    raise ValueError("shared transfer price contracts must have identical terms")
            if len(group) == 1:
                continue
            if len(group) != 2 or any(
                transfer.price_scope != TransferPriceScope.ROUND_TRIP for transfer in group
            ):
                raise ValueError(
                    "a shared round-trip transfer contract must cover exactly two legs"
                )
            first, returning = group
            if (
                first.origin_area != returning.destination_area
                or first.destination_area != returning.origin_area
                or first.origin_place_key != returning.destination_place_key
                or first.destination_place_key != returning.origin_place_key
            ):
                raise ValueError("shared round-trip transfer contracts require reciprocal legs")
        return self

    @property
    def component_ids(self) -> tuple[str, ...]:
        return (
            self.flight.id,
            *(lodging.id for lodging in self.lodgings),
            *(transfer.id for transfer in self.transfers),
        )

    @property
    def computed_total_cents(self) -> int:
        flight_total = (
            self.flight.total_for_party_cents
            if self.flight.party_total_known and self.flight.total_for_party_cents is not None
            else 0
        )
        return (
            flight_total
            + sum(
                lodging.total_for_party_cents
                for lodging in self.lodgings
                if lodging.currency == self.currency
            )
            + transfer_contract_total_cents(
                self.transfers,
                currency=self.currency,
            )
        )

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        refs: list[str] = []
        for quote in (self.flight, *self.lodgings, *self.transfers):
            for ref in quote.evidence_refs:
                if ref not in refs:
                    refs.append(ref)
        return tuple(refs)


class PackageCandidateGenerationResult(DomainModel):
    candidates: tuple[TravelPackageCandidate, ...]
    audit: PackageCandidateGenerationAudit


class PackageSupplementalPublishedFare(DomainModel):
    currency: str = Field(min_length=3, max_length=3)
    adults: int = Field(ge=1, le=20)
    total_for_party_cents: int = Field(ge=0)
    price_contract_ids: tuple[str, ...] = Field(min_length=1)
    transfer_ids: tuple[str, ...] = Field(min_length=1)
    price_guarantee: TransferPriceGuarantee = TransferPriceGuarantee.PUBLISHED_BASE_FARE
    taxes_and_fees_included: bool | None = None

    @field_validator("currency")
    @classmethod
    def normalize_supplemental_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_published_base_fare(self) -> Self:
        if self.price_guarantee != TransferPriceGuarantee.PUBLISHED_BASE_FARE:
            raise ValueError("supplemental fare must remain a published base fare")
        if self.taxes_and_fees_included is not None:
            raise ValueError("supplemental published fare tax inclusion must remain unknown")
        return self


class PackageForeignCurrencySubtotal(DomainModel):
    currency: str = Field(min_length=3, max_length=3)
    adults: int = Field(ge=1, le=20)
    total_for_party_cents: int = Field(ge=0)
    component_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class PackageBudgetBreakdown(DomainModel):
    currency: str
    adults: int
    flight_cents: int = Field(ge=0)
    lodging_cents: int = Field(ge=0)
    transfer_cents: int = Field(ge=0)
    total_cents: int = Field(ge=0)
    confirmed_subtotal_cents: int = Field(ge=0)
    flight_total_known: bool = True
    supplemental_published_base_fares: tuple[PackageSupplementalPublishedFare, ...] = ()
    foreign_currency_subtotals: tuple[PackageForeignCurrencySubtotal, ...] = ()
    budget_compliance_fully_verified: bool = True
    is_all_in_total: bool = True
    formula: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_budget_truth_flags(self) -> Self:
        if self.confirmed_subtotal_cents != self.total_cents:
            raise ValueError("legacy total_cents must equal confirmed_subtotal_cents")
        if self.budget_compliance_fully_verified != self.is_all_in_total:
            raise ValueError("budget truth flags conflict with supplemental fares")
        if self.supplemental_published_base_fares and self.is_all_in_total:
            raise ValueError("supplemental fares forbid an all-in budget claim")
        if self.foreign_currency_subtotals and self.is_all_in_total:
            raise ValueError("foreign currency subtotals forbid an all-in budget claim")
        return self


class DecisionOnlyPackageCandidate(DomainModel):
    """A complete comparison candidate that can never become executable."""

    candidate: TravelPackageCandidate
    budget: PackageBudgetBreakdown
    execution_eligible: bool = False
    decision_boundary: str = Field(min_length=1)

    @model_validator(mode="after")
    def enforce_decision_only(self) -> Self:
        if self.execution_eligible:
            raise ValueError("decision-only candidates cannot be execution eligible")
        if self.candidate.flight.availability != QuoteAvailability.COMPARISON_ONLY:
            raise ValueError("decision-only candidate requires a comparison-only flight")
        return self


class DecisionOnlyCandidateSet(DomainModel):
    """Authoritative set of complete, non-executable comparison candidates."""

    candidates: tuple[DecisionOnlyPackageCandidate, ...] = Field(min_length=1)
    selected_candidate_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        ids = tuple(item.candidate.id for item in self.candidates)
        if len(ids) != len(set(ids)):
            raise ValueError("decision-only candidate IDs must be unique")
        if len({item.candidate.flight.id for item in self.candidates}) != 1:
            raise ValueError("decision-only candidates must share one flight")
        if self.selected_candidate_id not in ids:
            raise ValueError("selected decision-only candidate is not in the set")
        if any(item.execution_eligible for item in self.candidates):
            raise ValueError("decision-only candidate set cannot contain executable items")
        return self


class PackageViolation(DomainModel):
    code: PackageViolationCode
    severity: PackageViolationSeverity
    message: str = Field(min_length=1)
    component_ids: tuple[str, ...] = ()
    details: dict[str, str | int | bool] = Field(default_factory=dict)


def package_date_violations(
    intent: PackageIntent,
    candidate: TravelPackageCandidate,
) -> tuple[PackageViolation, ...]:
    """Apply the one authoritative date contract to every candidate class."""

    departure_matches = candidate.flight.outbound_depart_at.date() == intent.start_date
    return_departure_matches = candidate.flight.return_depart_at.date() == intent.end_date
    if departure_matches and return_departure_matches:
        if (
            intent.latest_arrival_date is None
            or candidate.flight.return_arrive_at.date() <= intent.latest_arrival_date
        ):
            return ()
        return (
            PackageViolation(
                code=PackageViolationCode.DATE_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="返程实际到达日期晚于用户要求的回杭边界",
                component_ids=(candidate.flight.id,),
            ),
        )
    return (
        PackageViolation(
            code=PackageViolationCode.DATE_MISMATCH,
            severity=PackageViolationSeverity.ERROR,
            message="航班日期与用户旅行日期不一致",
            component_ids=(candidate.flight.id,),
        ),
    )


class PackageDiff(DomainModel):
    before_candidate_id: str
    after_candidate_id: str
    removed_component_ids: tuple[str, ...] = ()
    added_component_ids: tuple[str, ...] = ()
    changed_component_ids: tuple[str, ...] = ()
    preserved_component_ids: tuple[str, ...] = ()
    preservation_ratio: Decimal = Field(ge=0, le=1)

    @property
    def changed(self) -> bool:
        return bool(
            self.removed_component_ids or self.added_component_ids or self.changed_component_ids
        )


class PackageEvent(DomainModel):
    id: str = Field(min_length=1)
    kind: PackageEventKind
    target_component_id: str = Field(min_length=1)
    replacement_component_id: str = Field(min_length=1)


class PackageDecision(DomainModel):
    state: PackageDecisionState
    summary: str = Field(min_length=1)
    violation_codes: tuple[PackageViolationCode, ...] = ()
    evidence_refs: tuple[str, ...] = ()


class PackagePreferenceApplication(DomainModel):
    key: str = "hotel_breakfast"
    mode: PreferenceMode | None = None
    weight: float | None = Field(default=None, ge=0, le=1)
    state: PackagePreferenceApplicationState
    reason: str = Field(min_length=1)
    comparable_candidate_count: int = Field(default=0, ge=0)
    selected_candidate_id: str = Field(min_length=1)
    selected_breakfast_coverage: Decimal | None = Field(default=None, ge=0, le=1)
    selected_breakfast_evidence_complete: bool | None = None


class PackageRepairPlanStrategy(StrEnum):
    NO_ACTION = "no_action"
    LOCAL_REPAIR = "local_repair"
    EXPAND_CANDIDATE_POOL = "expand_candidate_pool"
    GLOBAL_REPLAN = "global_replan"
    HUMAN_BLOCK = "human_block"


class PackageRepairPlanStep(DomainModel):
    order: int = Field(ge=1)
    action: str = Field(min_length=1)
    component_ids: tuple[str, ...] = ()
    dependency_component_ids: tuple[str, ...] = ()
    success_invariant: str = Field(min_length=1)


class PackageStructuredRepairPlan(DomainModel):
    strategy: PackageRepairPlanStrategy
    target_component_ids: tuple[str, ...] = ()
    cascade_component_ids: tuple[str, ...] = ()
    preserve_component_ids: tuple[str, ...] = ()
    candidate_pool_expansion_required: bool = False
    requested_candidate_count: int = Field(default=0, ge=0)
    steps: tuple[PackageRepairPlanStep, ...] = ()
    fallback_strategy: PackageRepairPlanStrategy | None = None
    rationale: str = Field(min_length=1)


class PackageRepairOutcome(DomainModel):
    candidate: TravelPackageCandidate | None
    diff: PackageDiff | None
    message: str = Field(min_length=1)
    repair_plan: PackageStructuredRepairPlan | None = None


class PackagePlannerHandoff(DomainModel):
    candidates: tuple[TravelPackageCandidate, ...] = ()
    selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        candidate_ids = tuple(candidate.id for candidate in self.candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("planner handoff candidate ids must be unique")
        if not self.candidates:
            if self.selected_candidate_id is not None:
                raise ValueError("empty planner handoff cannot select a candidate")
            return self
        if self.selected_candidate_id is None:
            raise ValueError("non-empty planner handoff must select a candidate")
        if self.selected_candidate_id not in candidate_ids:
            raise ValueError("planner selection must reference a handed-off candidate")
        return self

    @property
    def selected_candidate(self) -> TravelPackageCandidate | None:
        if self.selected_candidate_id is None:
            return None
        return next(
            candidate for candidate in self.candidates if candidate.id == self.selected_candidate_id
        )


class PackageVerificationHandoff(DomainModel):
    phase: PackageVerificationPhase
    candidate_id: str = Field(min_length=1)
    candidate_version: int = Field(ge=1)
    component_ids: tuple[str, ...] = Field(min_length=1)
    violations: tuple[PackageViolation, ...]
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def require_verified_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("verification handoff timestamp must be timezone-aware")
        return value

    @property
    def errors(self) -> tuple[PackageViolation, ...]:
        return tuple(
            violation
            for violation in self.violations
            if violation.severity == PackageViolationSeverity.ERROR
        )

    @classmethod
    def from_candidate(
        cls,
        *,
        phase: PackageVerificationPhase,
        candidate: TravelPackageCandidate,
        violations: tuple[PackageViolation, ...],
        verified_at: datetime,
    ) -> PackageVerificationHandoff:
        return cls(
            phase=phase,
            candidate_id=candidate.id,
            candidate_version=candidate.version,
            component_ids=candidate.component_ids,
            violations=violations,
            verified_at=verified_at,
        )

    def matches(self, candidate: TravelPackageCandidate) -> bool:
        return (
            self.candidate_id == candidate.id
            and self.candidate_version == candidate.version
            and self.component_ids == candidate.component_ids
        )


class PackageRepairHandoff(DomainModel):
    """Auditable Repair receipt with separate hard- and soft-risk triggers.

    ``attempted`` remains tied one-to-one to deterministic Verifier errors.
    ``agent_strategy_applied`` records a material switch proposed for a Critic
    soft risk after the executor has checked the frozen candidate identity and
    run a deterministic pre-verification.  Keeping the flags separate prevents
    an LLM soft-risk proposal from fabricating a hard rejection reason.
    """

    rejected_candidate_id: str = Field(min_length=1)
    rejection_error_codes: tuple[PackageViolationCode, ...] = ()
    attempted: bool
    agent_strategy_applied: bool = False
    outcome: PackageRepairOutcome

    @model_validator(mode="after")
    def validate_attempt(self) -> Self:
        if self.attempted and not self.rejection_error_codes:
            raise ValueError("repair attempt requires verifier rejection reasons")
        if not self.attempted and self.rejection_error_codes:
            raise ValueError("no-op repair cannot carry rejection reasons")
        candidate = self.outcome.candidate
        changed_by_repair = self.attempted or self.agent_strategy_applied
        if changed_by_repair and candidate is not None:
            if candidate.id == self.rejected_candidate_id:
                raise ValueError("repair cannot silently reuse a rejected candidate")
            if self.outcome.diff is None or not self.outcome.diff.changed:
                raise ValueError("repair candidate must include a material package diff")
        if self.agent_strategy_applied and candidate is None:
            raise ValueError("applied Agent repair strategy requires a candidate")
        if not changed_by_repair and (
            candidate is None or candidate.id != self.rejected_candidate_id
        ):
            raise ValueError("no-op repair must preserve the verified candidate")
        if not changed_by_repair and self.outcome.diff is not None:
            raise ValueError("no-op repair cannot claim a package diff")
        return self


class PackagePlanningHandoff(DomainModel):
    planner: PackagePlannerHandoff
    initial_verification: PackageVerificationHandoff
    repair: PackageRepairHandoff
    reverification: PackageVerificationHandoff | None

    @model_validator(mode="after")
    def validate_chain(self) -> Self:
        initial = self.planner.selected_candidate
        if initial is None:
            raise ValueError("planning handoff requires a selected candidate")
        if self.initial_verification.phase != PackageVerificationPhase.INITIAL:
            raise ValueError("initial verification handoff has the wrong phase")
        if not self.initial_verification.matches(initial):
            raise ValueError("initial verifier did not verify the planner selection")
        if self.repair.rejected_candidate_id != initial.id:
            raise ValueError("repair handoff does not reference the verified candidate")
        expected_codes = tuple(violation.code for violation in self.initial_verification.errors)
        if self.repair.rejection_error_codes != expected_codes:
            raise ValueError("repair reasons must exactly match verifier hard errors")
        if bool(expected_codes) != self.repair.attempted:
            raise ValueError("repair attempt must follow the verifier rejection state")
        repaired = self.repair.outcome.candidate
        if repaired is None:
            if self.reverification is not None:
                raise ValueError("missing repair candidate cannot be reverified")
            return self
        if self.reverification is None:
            raise ValueError("master cannot receive an unverified repair candidate")
        if self.reverification.phase != PackageVerificationPhase.REVERIFICATION:
            raise ValueError("repair candidate must use the reverification phase")
        if not self.reverification.matches(repaired):
            raise ValueError("reverifier did not verify the repair output")
        return self


class PackageEventRepairHandoff(DomainModel):
    event: PackageEvent
    current_candidate_id: str = Field(min_length=1)
    current_candidate_version: int = Field(ge=1)
    current_component_ids: tuple[str, ...] = Field(min_length=1)
    outcome: PackageRepairOutcome

    @model_validator(mode="after")
    def validate_event_repair(self) -> Self:
        if self.event.target_component_id not in self.current_component_ids:
            raise ValueError("event repair target is not in the current package")
        repaired = self.outcome.candidate
        if repaired is None:
            if self.outcome.diff is not None:
                raise ValueError("failed event repair cannot claim a package diff")
            return self
        if repaired.parent_candidate_id != self.current_candidate_id:
            raise ValueError("event repair must descend from the current package")
        if self.outcome.diff is None or not self.outcome.diff.changed:
            raise ValueError("event repair must expose the changed component diff")
        if self.outcome.diff.before_candidate_id != self.current_candidate_id:
            raise ValueError("event repair diff must start from the current package")
        return self


class PackageEventPlanningHandoff(DomainModel):
    repair: PackageEventRepairHandoff
    reverification: PackageVerificationHandoff | None

    @model_validator(mode="after")
    def validate_event_chain(self) -> Self:
        repaired = self.repair.outcome.candidate
        if repaired is None:
            if self.reverification is not None:
                raise ValueError("failed event repair cannot be reverified")
            return self
        if self.reverification is None:
            raise ValueError("event repair cannot reach master without ReVerifier")
        if self.reverification.phase != PackageVerificationPhase.EVENT_REVERIFICATION:
            raise ValueError("event repair must use event reverification phase")
        if not self.reverification.matches(repaired):
            raise ValueError("event ReVerifier did not verify the repair output")
        return self


class PackageRunResult(DomainModel):
    initial_candidate: TravelPackageCandidate
    final_candidate: TravelPackageCandidate
    decisions: tuple[PackageDecision, ...] = Field(min_length=1)
    final_decision: PackageDecision
    initial_violations: tuple[PackageViolation, ...]
    final_violations: tuple[PackageViolation, ...]
    diff: PackageDiff | None
    preservation_ratio: Decimal = Field(ge=0, le=1)
    budget: PackageBudgetBreakdown
    evidence_refs: tuple[str, ...]
    preference_applications: tuple[PackagePreferenceApplication, ...] = ()
    planning_handoff: PackagePlanningHandoff | None = None
    event_handoff: PackageEventPlanningHandoff | None = None


def _cny(cents: int) -> str:
    return f"¥{Decimal(cents) / Decimal(100):.2f}"


def _money(currency: str, cents: int) -> str:
    if currency == "CNY":
        return _cny(cents)
    return f"{currency} {Decimal(cents) / Decimal(100):.2f}"


def supplemental_published_base_fares(
    transfers: tuple[TransferOption, ...],
) -> tuple[PackageSupplementalPublishedFare, ...]:
    by_currency: dict[str, dict[str, TransferOption]] = {}
    transfer_ids_by_currency: dict[str, list[str]] = {}
    for transfer in transfers:
        if transfer.price_guarantee != TransferPriceGuarantee.PUBLISHED_BASE_FARE:
            continue
        by_currency.setdefault(transfer.currency, {}).setdefault(
            transfer.price_contract_id,
            transfer,
        )
        ids = transfer_ids_by_currency.setdefault(transfer.currency, [])
        if transfer.id not in ids:
            ids.append(transfer.id)
    return tuple(
        PackageSupplementalPublishedFare(
            currency=currency,
            adults=next(iter(contracts.values())).adults,
            total_for_party_cents=sum(
                transfer.total_for_party_cents for transfer in contracts.values()
            ),
            price_contract_ids=tuple(sorted(contracts)),
            transfer_ids=tuple(sorted(transfer_ids_by_currency[currency])),
        )
        for currency, contracts in sorted(by_currency.items())
    )


def published_base_fare_contract_count(
    transfers: tuple[TransferOption, ...],
) -> int:
    return len(
        {
            transfer.price_contract_id
            for transfer in transfers
            if transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
        }
    )


def package_budget(candidate: TravelPackageCandidate) -> PackageBudgetBreakdown:
    flight_known = candidate.flight.party_total_known
    flight = (
        candidate.flight.total_for_party_cents
        if flight_known and candidate.flight.total_for_party_cents is not None
        else 0
    )
    lodging = sum(
        item.total_for_party_cents
        for item in candidate.lodgings
        if item.currency == candidate.currency
    )
    transfer = transfer_contract_total_cents(
        candidate.transfers,
        currency=candidate.currency,
    )
    total = flight + lodging + transfer
    party_label = (
        f"{candidate.flight.adults}名成人"
        if candidate.flight.children == 0 and candidate.flight.infants == 0
        else f"{candidate.flight.adults}名成人、{candidate.flight.children}名儿童、"
        f"{candidate.flight.infants}名婴儿"
    )
    supplemental = supplemental_published_base_fares(candidate.transfers)
    foreign_groups: dict[str, list[NormalizedLodgingQuote]] = {}
    for item in candidate.lodgings:
        if item.currency != candidate.currency:
            foreign_groups.setdefault(item.currency, []).append(item)
    foreign_subtotals = tuple(
        PackageForeignCurrencySubtotal(
            currency=currency,
            adults=items[0].adults,
            total_for_party_cents=sum(item.total_for_party_cents for item in items),
            component_ids=tuple(item.id for item in items),
        )
        for currency, items in sorted(foreign_groups.items())
    )
    all_in_total = (
        flight_known
        and not supplemental
        and all(
            quote.currency == candidate.currency and quote.taxes_and_fees_included is True
            for quote in (candidate.flight, *candidate.lodgings, *candidate.transfers)
        )
        and not foreign_subtotals
    )
    if flight_known:
        formula = (
            f"航班 {_money(candidate.currency, flight)} + "
            f"住宿 {_money(candidate.currency, lodging)} + "
            f"接驳 {_money(candidate.currency, transfer)} = "
            f"{_money(candidate.currency, total)}（{party_label}）"
        )
    else:
        displayed_flight_amount = candidate.flight.display_amount_cents or 0
        formula = (
            f"航班搜索上下文显示价 {_money(candidate.flight.currency, displayed_flight_amount)}"
            f"（本次旅客总价未由同一产品的完整人数对照证明，未计入）；"
            f"住宿 {_money(candidate.currency, lodging)} + "
            f"接驳 {_money(candidate.currency, transfer)}"
            f" = {_money(candidate.currency, total)} 已确认小计"
        )
    if supplemental:
        if flight_known:
            formula = (
                f"航班 {_money(candidate.currency, flight)} + "
                f"住宿 {_money(candidate.currency, lodging)} + "
                f"全包接驳 {_money(candidate.currency, transfer)} = "
                f"{_money(candidate.currency, total)} 已确认小计（{party_label}）"
            )
        else:
            displayed_flight = _money(
                candidate.flight.currency,
                candidate.flight.display_amount_cents or 0,
            )
            formula = (
                f"航班观察价 {displayed_flight}"
                f"（本次旅客总价未知，未计入）；住宿 {_money(candidate.currency, lodging)} + "
                f"全包接驳 {_money(candidate.currency, transfer)} = "
                f"{_money(candidate.currency, total)} 已确认小计"
            )
        supplemental_formula = "；".join(
            (
                f"另有公开基础价 {_money(item.currency, item.total_for_party_cents)}"
                f"（{item.adults}名成人，按 {len(item.price_contract_ids)} 个合同去重）"
            )
            for item in supplemental
        )
        formula = (
            f"{formula}；{supplemental_formula}，税费未知，未换汇且未计入 "
            f"{candidate.currency} 已确认小计"
        )
    if foreign_subtotals:
        foreign_formula = "；".join(
            f"另有住宿 {item.currency} {Decimal(item.total_for_party_cents) / Decimal(100):.2f}"
            f"（{item.adults}名成人，未换汇未计入 {candidate.currency} 小计）"
            for item in foreign_subtotals
        )
        formula = f"{formula}；{foreign_formula}"
    return PackageBudgetBreakdown(
        currency=candidate.currency,
        adults=candidate.flight.adults,
        flight_cents=flight,
        lodging_cents=lodging,
        transfer_cents=transfer,
        total_cents=total,
        confirmed_subtotal_cents=total,
        flight_total_known=flight_known,
        supplemental_published_base_fares=supplemental,
        foreign_currency_subtotals=foreign_subtotals,
        budget_compliance_fully_verified=all_in_total,
        is_all_in_total=all_in_total,
        formula=formula,
    )


def diff_packages(
    before: TravelPackageCandidate,
    after: TravelPackageCandidate,
) -> PackageDiff:
    before_ids = set(before.component_ids)
    after_ids = set(after.component_ids)
    before_quotes = {
        quote.id: quote for quote in (before.flight, *before.lodgings, *before.transfers)
    }
    after_quotes = {quote.id: quote for quote in (after.flight, *after.lodgings, *after.transfers)}
    changed = tuple(
        item_id
        for item_id in before.component_ids
        if item_id in after_quotes and before_quotes[item_id] != after_quotes[item_id]
    )
    preserved = tuple(item_id for item_id in before.component_ids if item_id in after_ids)
    ratio = (
        Decimal(len(preserved)) / Decimal(len(before.component_ids))
        if before.component_ids
        else Decimal(1)
    )
    return PackageDiff(
        before_candidate_id=before.id,
        after_candidate_id=after.id,
        removed_component_ids=tuple(
            item_id for item_id in before.component_ids if item_id not in after_ids
        ),
        added_component_ids=tuple(
            item_id for item_id in after.component_ids if item_id not in before_ids
        ),
        changed_component_ids=changed,
        preserved_component_ids=preserved,
        preservation_ratio=ratio,
    )


def _transfer_connection_limits(
    intent: PackageIntent,
    flight: NormalizedFlightQuote,
    kind: PackageCandidateKind,
    transfer: TransferOption,
) -> tuple[datetime | None, datetime | None]:
    not_before: datetime | None = None
    arrive_by: datetime | None = None
    actual_departure_date = flight.outbound_arrive_at.date()
    actual_return_date = flight.return_depart_at.date()
    if (
        transfer.origin_area == PackageArea.AIRPORT
        and transfer.travel_date == actual_departure_date
    ):
        required_buffer = (
            intent.minimum_arrival_to_boat_minutes
            if kind == PackageCandidateKind.CONTINUOUS_ISLAND
            and transfer.destination_area == PackageArea.DESTINATION_ISLAND
            else 0
        )
        not_before = flight.outbound_arrive_at + timedelta(minutes=required_buffer)
    if (
        transfer.destination_area == PackageArea.AIRPORT
        and transfer.travel_date == actual_return_date
    ):
        arrive_by = flight.return_depart_at - timedelta(
            minutes=intent.minimum_airport_buffer_minutes
        )
    return not_before, arrive_by


def _required_transfer_legs(
    intent: PackageIntent,
    kind: PackageCandidateKind,
    *,
    flight: NormalizedFlightQuote | None = None,
) -> tuple[tuple[PackageArea, PackageArea, date], ...]:
    stay_start = flight.outbound_arrive_at.date() if flight is not None else intent.start_date
    stay_end = flight.return_depart_at.date() if flight is not None else intent.end_date
    if kind == PackageCandidateKind.CONTINUOUS_ISLAND:
        return (
            (PackageArea.AIRPORT, PackageArea.DESTINATION_ISLAND, stay_start),
            (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT, stay_end),
        )
    if kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND:
        return (
            (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, stay_start),
            (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, stay_end),
        )
    first_checkout = stay_start + timedelta(days=1)
    last_checkin = stay_end - timedelta(days=1)
    return (
        (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, stay_start),
        (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, first_checkout),
        (PackageArea.AIRPORT, PackageArea.DESTINATION_ISLAND, first_checkout),
        (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT, last_checkin),
        (PackageArea.AIRPORT, PackageArea.AIRPORT_ISLAND, last_checkin),
        (PackageArea.AIRPORT_ISLAND, PackageArea.AIRPORT, stay_end),
    )


def _split_connection_pairs(
    intent: PackageIntent,
    transfers: tuple[TransferOption, ...],
    *,
    flight: NormalizedFlightQuote | None = None,
) -> tuple[tuple[TransferOption, TransferOption], ...]:
    required = _required_transfer_legs(
        intent,
        PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
        flight=flight,
    )
    by_leg = {
        (transfer.origin_area, transfer.destination_area, transfer.travel_date): transfer
        for transfer in transfers
    }
    try:
        outbound_to_airport = by_leg[required[1]]
        outbound_from_airport = by_leg[required[2]]
        inbound_to_airport = by_leg[required[3]]
        inbound_from_airport = by_leg[required[4]]
    except KeyError:
        return ()
    return (
        (outbound_to_airport, outbound_from_airport),
        (inbound_to_airport, inbound_from_airport),
    )


def _transfers_can_connect(
    first: TransferOption,
    second: TransferOption,
    *,
    minimum_connection_minutes: int,
) -> bool:
    return (
        first.travel_date == second.travel_date
        and first.destination_area == second.origin_area
        and first.earliest_arrival_at + timedelta(minutes=minimum_connection_minutes)
        <= second.latest_departure_at
    )


def _effective_breakfast_preference(
    intent: PackageIntent,
) -> tuple[PreferenceMode | None, float | None]:
    if intent.breakfast_preference_mode is not None:
        return (
            intent.breakfast_preference_mode,
            intent.breakfast_preference_weight,
        )
    if intent.require_breakfast is True:
        return PreferenceMode.REQUIRED, 1.0
    if intent.require_breakfast is False:
        return PreferenceMode.FORBIDDEN, 1.0
    return None, None


def _breakfast_coverage(
    candidate: TravelPackageCandidate,
) -> tuple[Decimal, bool]:
    total_nights = sum(lodging.night_count for lodging in candidate.lodgings)
    if total_nights <= 0:
        return Decimal(0), False
    confirmed_nights = sum(
        lodging.night_count for lodging in candidate.lodgings if lodging.breakfast_included is True
    )
    evidence_complete = all(
        lodging.breakfast_included is not None for lodging in candidate.lodgings
    )
    return Decimal(confirmed_nights) / Decimal(total_nights), evidence_complete


def breakfast_preference_application(
    intent: PackageIntent,
    candidates: tuple[TravelPackageCandidate, ...],
    selected: TravelPackageCandidate,
) -> PackagePreferenceApplication:
    mode, weight = _effective_breakfast_preference(intent)
    selected_coverage, selected_complete = _breakfast_coverage(selected)
    unique_candidates: dict[tuple[str, ...], TravelPackageCandidate] = {
        candidate.component_ids: candidate for candidate in candidates
    }
    unique_candidates.setdefault(selected.component_ids, selected)
    candidate_pool = tuple(unique_candidates.values())
    selected_tier = published_base_fare_contract_count(selected.transfers)
    comparable = tuple(
        candidate
        for candidate in candidate_pool
        if published_base_fare_contract_count(candidate.transfers) == selected_tier
    )
    common = {
        "mode": mode,
        "weight": weight,
        "comparable_candidate_count": len(comparable),
        "selected_candidate_id": selected.id,
        "selected_breakfast_coverage": selected_coverage,
        "selected_breakfast_evidence_complete": selected_complete,
    }
    if mode in {PreferenceMode.REQUIRED, PreferenceMode.FORBIDDEN}:
        expected = "必须确认包含早餐" if mode == PreferenceMode.REQUIRED else "必须确认不含早餐"
        return PackagePreferenceApplication(
            state=PackagePreferenceApplicationState.HARD_CONSTRAINT,
            reason=(
                f"早餐偏好按硬约束执行：{expected}；未知状态不会被视为满足，最终仍须通过 Verifier。"
            ),
            **common,
        )
    if mode in {None, PreferenceMode.INDIFFERENT}:
        return PackagePreferenceApplication(
            state=PackagePreferenceApplicationState.NOT_REQUESTED,
            reason=(
                "用户未声明早餐偏好，不参与排序。"
                if mode is None
                else "用户明确对早餐不作要求，规范权重为 0，不参与排序。"
            ),
            **common,
        )
    assert weight is not None
    if weight == 0:
        return PackagePreferenceApplication(
            state=PackagePreferenceApplicationState.NOT_APPLIED,
            reason="早餐为加权偏好，但用户权重为 0，候选保持证据层级与价格排序。",
            **common,
        )
    if len(comparable) < 2:
        return PackagePreferenceApplication(
            state=PackagePreferenceApplicationState.NOT_APPLIED,
            reason=(
                "同一报价证据层只有一个可比候选，无法应用早餐权重进行相对排序；没有伪造偏好增益。"
            ),
            **common,
        )
    return PackagePreferenceApplication(
        state=PackagePreferenceApplicationState.APPLIED,
        reason=(
            "已在同一报价证据层的可比候选中，用用户 0–1 权重融合价格效用与"
            "已确认早餐覆盖率；早餐未知夜晚获得 0 偏好奖励，但未被推断为不含早餐。"
        ),
        **common,
    )


class PackagePlanner:
    LIVE_CANDIDATE_CAP = 256
    LIVE_FLIGHT_LIMIT = 12
    LIVE_LODGING_LIMIT_PER_SEGMENT = 8
    LIVE_TRANSFER_BEAM_WIDTH = 64
    LIVE_TRANSFER_LIMIT_PER_CONTRACT_BUCKET = 8

    def build_decision_only_candidate(
        self,
        intent: PackageIntent,
        flight: NormalizedFlightQuote,
        inventory: PackageInventory,
        *,
        transfer_provider: str,
    ) -> DecisionOnlyPackageCandidate | None:
        """Join a sealed comparison flight with normal package rules."""

        if not (
            flight.provider == "qunar"
            and flight.currency == intent.currency
            and flight.party_total_known
            and flight.total_for_party_cents is not None
            and flight.total_for_party_cents > 0
            and flight.price_basis == "comparison_only"
            and flight.availability == QuoteAvailability.COMPARISON_ONLY
            and not flight.party_availability_confirmed
            and any(
                reference.startswith("flight-party-comparison:sha256:")
                for reference in flight.evidence_refs
            )
        ):
            return None
        variants = self.build_decision_only_candidates(
            intent, flight, inventory, transfer_provider=transfer_provider
        )
        if not variants:
            return None
        return min(
            variants,
            key=lambda item: (
                sum(lodging_quality_rank(lodging) for lodging in item.candidate.lodgings),
                item.budget.confirmed_subtotal_cents,
                item.candidate.declared_total_cents,
                item.candidate.id,
            ),
        )

    def build_decision_only_candidates(
        self,
        intent: PackageIntent,
        flight: NormalizedFlightQuote,
        inventory: PackageInventory,
        *,
        transfer_provider: str,
    ) -> tuple[DecisionOnlyPackageCandidate, ...]:
        """Build all bounded lodging variants for decision-only comparison."""
        variants = tuple(
            candidate
            for candidate in (
                *self._continuous_candidates(intent, flight, inventory),
                *self._split_candidates(intent, flight, inventory),
            )
            if not package_date_violations(intent, candidate)
            and candidate.transfers
            and all(
                item.provider == transfer_provider
                for item in candidate.transfers
            )
        )
        representatives: dict[str, TravelPackageCandidate] = {}
        for candidate in variants:
            lodging_identity = "|".join(
                re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", item.property_name.casefold()).strip()
                for item in candidate.lodgings
            )
            provider_key = (
                f"{candidate.lodgings[0].provider}|{lodging_identity}|{candidate.kind.value}"
            )
            current = representatives.get(provider_key)
            if current is None or (
                package_budget(candidate).confirmed_subtotal_cents,
                candidate.id,
            ) < (
                package_budget(current).confirmed_subtotal_cents,
                current.id,
            ):
                representatives[provider_key] = candidate
        return tuple(
            DecisionOnlyPackageCandidate(
                candidate=candidate,
                budget=package_budget(candidate),
                decision_boundary=(
                    "仅用于当前已封存比较价的行程决策；不代表余位、可订性、成交或库存锁定。"
                ),
            )
            for candidate in representatives.values()
        )

    def generate(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> tuple[TravelPackageCandidate, ...]:
        """Return the safe default beam; callers needing proof use ``generate_bounded``.

        There is intentionally no public unbounded Cartesian generation path.
        Small fixtures below all limits produce the same candidates, while
        unexpectedly large provider inventories are deterministically bounded.
        """

        return self.generate_bounded(intent, inventory).candidates

    def generate_bounded(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
        *,
        candidate_cap: int = LIVE_CANDIDATE_CAP,
    ) -> PackageCandidateGenerationResult:
        """Generate a deterministic live beam and an explicit non-exhaustive proof."""

        if candidate_cap < 1 or candidate_cap > 2_000:
            raise ValueError("candidate_cap must be between one and two thousand")
        raw_upper = self._structural_candidate_upper_bound(intent, inventory)
        prescreened = self._prescreen_live_inventory(intent, inventory)
        prescreened_upper = self._structural_candidate_upper_bound(intent, prescreened)
        ranked_joined = self.rank_candidates(
            intent,
            self._generate_candidates(
                intent,
                prescreened,
            ),
        )
        stopped_at_cap = len(ranked_joined) > candidate_cap
        generated = self._select_diverse_candidate_beam(
            ranked_joined,
            limit=candidate_cap,
        )
        rejection_reasons = self._generation_rejection_reasons(
            intent,
            prescreened,
            generated,
        )
        raw_counts = self._inventory_counts(inventory)
        prescreened_counts = self._inventory_counts(prescreened)
        raw_inventory_ids_sha256 = self._inventory_ids_sha256(inventory)
        prescreened_inventory_ids_sha256 = self._inventory_ids_sha256(prescreened)
        input_pruned = raw_counts != prescreened_counts
        proof_payload = {
            "policy_version": _PACKAGE_CANDIDATE_POLICY_VERSION,
            "selection_policy_version": _PACKAGE_CANDIDATE_SELECTION_POLICY_VERSION,
            "raw_inventory_counts": raw_counts,
            "prescreened_inventory_counts": prescreened_counts,
            "raw_inventory_ids_sha256": raw_inventory_ids_sha256,
            "prescreened_inventory_ids_sha256": prescreened_inventory_ids_sha256,
            "raw_structural_candidate_upper_bound": raw_upper,
            "prescreened_structural_candidate_upper_bound": prescreened_upper,
            "generation_candidate_cap": candidate_cap,
            "transfer_beam_width": self.LIVE_TRANSFER_BEAM_WIDTH,
            "transfer_limit_per_contract_bucket": (self.LIVE_TRANSFER_LIMIT_PER_CONTRACT_BUCKET),
            "structurally_joined_candidate_count": len(ranked_joined),
            "generated_candidate_ids": [item.id for item in generated],
            "rejection_reasons": list(rejection_reasons),
            "generation_stopped_at_cap": stopped_at_cap,
            "prescreen_structure_scan_completed": True,
            "transfer_combinations_exhaustively_enumerated": False,
            "full_enumeration_claimed": False,
        }
        proof = hashlib.sha256(
            json.dumps(
                proof_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        audit = PackageCandidateGenerationAudit(
            policy_version=_PACKAGE_CANDIDATE_POLICY_VERSION,
            selection_policy_version=_PACKAGE_CANDIDATE_SELECTION_POLICY_VERSION,
            raw_inventory_counts=raw_counts,
            prescreened_inventory_counts=prescreened_counts,
            raw_inventory_ids_sha256=raw_inventory_ids_sha256,
            prescreened_inventory_ids_sha256=prescreened_inventory_ids_sha256,
            raw_structural_candidate_upper_bound=raw_upper,
            prescreened_structural_candidate_upper_bound=prescreened_upper,
            generation_candidate_cap=candidate_cap,
            transfer_beam_width=self.LIVE_TRANSFER_BEAM_WIDTH,
            transfer_limit_per_contract_bucket=(self.LIVE_TRANSFER_LIMIT_PER_CONTRACT_BUCKET),
            structurally_joined_candidate_count=len(ranked_joined),
            generated_candidate_count=len(generated),
            generated_candidate_ids=tuple(item.id for item in generated),
            rejection_reasons=rejection_reasons,
            input_prescreen_pruned=input_pruned,
            generation_stopped_at_cap=stopped_at_cap,
            prescreen_structure_scan_completed=True,
            omitted_scope=(
                "输入报价先按分段、provider 与权益作确定性 beam 预筛；"
                "接驳组合仅保留固定宽度 beam；在完整扫描有界预筛结构后，"
                "按 provider、航班与方案类型的新覆盖优先保留多样性，再按原排序输出。"
                "candidate_cap 只截断最终候选池，不会让先扫描的航班独占额度。"
                "未进入预筛或 beam 的组合没有被验证，即使结构扫描完成也不代表"
                "穷举过接驳组合，不得声称候选池覆盖全部有效组合。"
            ),
            generation_proof_sha256=proof,
        )
        return PackageCandidateGenerationResult(candidates=generated, audit=audit)

    def _generation_rejection_reasons(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
        generated: tuple[TravelPackageCandidate, ...],
    ) -> tuple[str, ...]:
        """Explain an empty hard-contract join without weakening any contract.

        Structural upper bounds deliberately ignore transfers, so a positive
        upper bound can still produce zero valid packages.  This audit mirrors
        the Planner's exact flight/lodging filters and records transfer legs
        for which the prescreened inventory contains no semantically compatible
        option.  It never treats a terminal search receipt as a quote and never
        converts a published foreign-currency base fare.
        """

        if generated:
            return ()

        reasons: list[str] = []
        flights = tuple(
            flight
            for flight in inventory.flights
            if flight.availability == QuoteAvailability.AVAILABLE
            and flight.origin == intent.origin
            and flight.destination == intent.destination
            and flight.outbound_depart_at.date() == intent.start_date
            and flight.return_depart_at.date() == intent.end_date
            and flight.adults == intent.adults
            and flight.children == intent.children
            and flight.infants == intent.infants
            and flight.currency == intent.currency
        )
        if not flights:
            reasons.append("flight:no_exact_available_round_trip_quote")

        def lodging_count(
            area: PackageArea,
            check_in: date,
            check_out: date,
        ) -> int:
            return sum(
                self._matching_lodging(
                    lodging,
                    intent,
                    area,
                    check_in,
                    check_out,
                )
                for lodging in inventory.lodgings
            )

        def compatible_transfer_count(
            origin: PackageArea,
            destination: PackageArea,
            service_date: date,
        ) -> int:
            return sum(
                transfer.availability == QuoteAvailability.AVAILABLE
                and transfer.origin_area == origin
                and transfer.destination_area == destination
                and transfer.service_date == service_date
                and transfer.adults == intent.adults
                and transfer.children == intent.children
                and transfer.infants == intent.infants
                and (
                    (
                        transfer.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
                        and transfer.currency == intent.currency
                    )
                    or transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                )
                for transfer in inventory.transfers
            )

        def audit_branch(
            kind: PackageCandidateKind,
            lodging_segments: tuple[tuple[PackageArea, date, date], ...],
        ) -> None:
            branch_reasons_before = len(reasons)
            for area, check_in, check_out in lodging_segments:
                if lodging_count(area, check_in, check_out) == 0:
                    reasons.append(
                        f"{kind.value}:lodging:{area.value}:"
                        f"{check_in.isoformat()}:{check_out.isoformat()}:"
                        "no_exact_normalized_quote"
                    )
            for origin, destination, service_date in _required_transfer_legs(intent, kind):
                if compatible_transfer_count(origin, destination, service_date) == 0:
                    reasons.append(
                        f"{kind.value}:transfer:{origin.value}:{destination.value}:"
                        f"{service_date.isoformat()}:no_compatible_hard_contract"
                    )
            if len(reasons) == branch_reasons_before and flights:
                reasons.append(f"{kind.value}:no_candidate_after_lodging_transfer_binding_join")

        continuous_branches: tuple[tuple[PackageArea, PackageCandidateKind], ...]
        if intent.destination_place_key == PackagePlaceKey.MAAFUSHI:
            continuous_branches = (
                (
                    PackageArea.DESTINATION_ISLAND,
                    PackageCandidateKind.CONTINUOUS_ISLAND,
                ),
            )
        elif intent.destination_place_key == PackagePlaceKey.HULHUMALE:
            continuous_branches = (
                (
                    PackageArea.AIRPORT_ISLAND,
                    PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
                ),
            )
        else:
            continuous_branches = (
                (
                    PackageArea.DESTINATION_ISLAND,
                    PackageCandidateKind.CONTINUOUS_ISLAND,
                ),
                (
                    PackageArea.AIRPORT_ISLAND,
                    PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
                ),
            )
        if flights:
            for area, kind in continuous_branches:
                audit_branch(
                    kind,
                    ((area, intent.start_date, intent.end_date),),
                )
            if intent.night_count >= 3:
                audit_branch(
                    PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
                    (
                        (
                            PackageArea.AIRPORT_ISLAND,
                            intent.start_date,
                            intent.start_date + timedelta(days=1),
                        ),
                        (
                            PackageArea.DESTINATION_ISLAND,
                            intent.start_date + timedelta(days=1),
                            intent.end_date - timedelta(days=1),
                        ),
                        (
                            PackageArea.AIRPORT_ISLAND,
                            intent.end_date - timedelta(days=1),
                            intent.end_date,
                        ),
                    ),
                )
        reasons.append("global:no_candidate_after_hard_contract_join")
        return tuple(dict.fromkeys(reasons))

    def _generate_candidates(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> tuple[TravelPackageCandidate, ...]:
        candidates: list[TravelPackageCandidate] = []
        flights = tuple(
            flight
            for flight in inventory.flights
            if flight.availability == QuoteAvailability.AVAILABLE
            and flight.origin == intent.origin
            and flight.destination == intent.destination
            and flight.outbound_depart_at.date() == intent.start_date
            and flight.return_depart_at.date() == intent.end_date
            and flight.adults == intent.adults
            and flight.children == intent.children
            and flight.infants == intent.infants
            and flight.currency == intent.currency
        )
        for flight in flights:
            candidates.extend(
                self._continuous_candidates(
                    intent,
                    flight,
                    inventory,
                )
            )
            candidates.extend(
                self._split_candidates(
                    intent,
                    flight,
                    inventory,
                )
            )
        return tuple(candidates)

    def _select_diverse_candidate_beam(
        self,
        ranked: tuple[TravelPackageCandidate, ...],
        *,
        limit: int,
    ) -> tuple[TravelPackageCandidate, ...]:
        """Reserve scarce beam slots without changing the deterministic winner.

        The input is already ranked by the package objective.  Greedy coverage
        gives priority to a previously unseen flight provider, then an unseen
        flight itinerary, then an unseen package kind.  Original rank resolves
        every tie.  Once no remaining candidate adds coverage, the rest of the
        beam is filled in original rank order.  Returning the selected set in
        original rank order keeps the globally best candidate first.
        """

        if len(ranked) <= limit:
            return ranked
        rank_by_id = {candidate.id: index for index, candidate in enumerate(ranked)}
        remaining = list(ranked)
        selected: dict[str, TravelPackageCandidate] = {}
        seen_providers: set[str] = set()
        seen_flights: set[str] = set()
        seen_kinds: set[PackageCandidateKind] = set()

        while remaining and len(selected) < limit:
            coverage_candidates = tuple(
                candidate
                for candidate in remaining
                if candidate.flight.provider not in seen_providers
                or candidate.flight.id not in seen_flights
                or candidate.kind not in seen_kinds
            )
            if not coverage_candidates:
                break
            chosen = min(
                coverage_candidates,
                key=lambda candidate: (
                    candidate.flight.provider in seen_providers,
                    candidate.flight.id in seen_flights,
                    candidate.kind in seen_kinds,
                    rank_by_id[candidate.id],
                ),
            )
            selected[chosen.id] = chosen
            seen_providers.add(chosen.flight.provider)
            seen_flights.add(chosen.flight.id)
            seen_kinds.add(chosen.kind)
            remaining.remove(chosen)

        for candidate in ranked:
            if len(selected) >= limit:
                break
            selected.setdefault(candidate.id, candidate)
        return tuple(sorted(selected.values(), key=lambda item: rank_by_id[item.id]))

    def _prescreen_live_inventory(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> PackageInventory:
        # A provider display amount with no same-product 1/N-adult proof is
        # observation-only.  It must not reach ranking or package arithmetic,
        # even when the rest of its route evidence looks complete.
        party_total_flights = tuple(
            flight
            for flight in inventory.flights
            if flight.party_total_known is True
            and (
                flight.price_basis == "total_party"
                or (
                    flight.price_basis == "per_person"
                    and any(
                        reference.startswith("flight-party-comparison:sha256:")
                        for reference in flight.evidence_refs
                    )
                )
            )
        )
        flights = self._take_diverse_flights(
            party_total_flights,
            self.LIVE_FLIGHT_LIMIT,
        )
        lodging_groups: dict[
            tuple[PackageArea, date, date, int, int, int, int, str],
            list[NormalizedLodgingQuote],
        ] = {}
        for lodging in inventory.lodgings:
            # Quality and location are fail-closed before candidate generation:
            # neither can be made true by a deterministic package repair.  Other
            # hard requirements remain visible to the verifier/repair chain, so
            # an otherwise compatible quote can still be replaced rather than
            # silently disappearing from the candidate audit.
            if intent.require_non_basic_lodging and lodging_basic_markers(lodging):
                continue
            if intent.require_non_remote_lodging and (
                lodging.location_convenience != LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
                or not lodging_non_remote_evidence_confirmed(
                    lodging.location_address,
                    lodging.nearby_location_evidence,
                )
            ):
                continue
            lodging_key = (
                lodging.area,
                lodging.check_in,
                lodging.check_out,
                lodging.adults,
                lodging.children,
                lodging.infants,
                lodging.rooms,
                lodging.currency,
            )
            lodging_groups.setdefault(lodging_key, []).append(lodging)
        lodgings = tuple(
            item
            for lodging_key in sorted(
                lodging_groups,
                key=lambda item: tuple(str(part) for part in item),
            )
            for item in self._take_diverse_lodgings(
                tuple(lodging_groups[lodging_key]),
                self.LIVE_LODGING_LIMIT_PER_SEGMENT,
            )
        )
        transfer_groups: dict[
            tuple[
                PackageArea,
                PackageArea,
                date,
                str | None,
                TransferPurchaseScope,
                TransferPriceGuarantee,
                int,
                int,
                int,
                str,
                str,
                TransferPriceScope,
                str,
                bool | None,
                bool | None,
                str,
            ],
            list[TransferOption],
        ] = {}
        for transfer in inventory.transfers:
            if transfer.depart_at is not None:
                schedule_key = transfer.depart_at.isoformat()
            else:
                assert transfer.service_window_start_at is not None
                assert transfer.service_window_end_at is not None
                schedule_key = (
                    f"{transfer.service_window_start_at.isoformat()}|"
                    f"{transfer.service_window_end_at.isoformat()}"
                )
            transfer_key = (
                transfer.origin_area,
                transfer.destination_area,
                transfer.service_date,
                transfer.bound_lodging_id,
                transfer.purchase_scope,
                transfer.price_guarantee,
                transfer.adults,
                transfer.children,
                transfer.infants,
                transfer.currency,
                transfer.provider,
                transfer.price_scope,
                transfer.price_contract_id,
                transfer.requires_reservation,
                transfer.taxes_and_fees_included,
                schedule_key,
            )
            transfer_groups.setdefault(transfer_key, []).append(transfer)
        # Collapse only duplicate observations of the same provider contract,
        # schedule and rights.  Cross-provider and one-way/round-trip choices
        # must survive so the downstream diversity beam can compare them.
        schedule_representatives = tuple(
            min(
                group,
                key=lambda item: (
                    item.total_for_party_cents,
                    item.provider,
                    item.id,
                ),
            )
            for _, group in sorted(
                transfer_groups.items(),
                key=lambda item: tuple(str(part) for part in item[0]),
            )
        )
        transfer_contract_groups: dict[
            tuple[
                PackageArea,
                PackageArea,
                date,
                str | None,
                TransferPurchaseScope,
                TransferPriceGuarantee,
                TransferPriceScope,
                int,
                int,
                int,
                str,
            ],
            list[TransferOption],
        ] = {}
        for transfer in schedule_representatives:
            contract_key = (
                transfer.origin_area,
                transfer.destination_area,
                transfer.service_date,
                transfer.bound_lodging_id,
                transfer.purchase_scope,
                transfer.price_guarantee,
                transfer.price_scope,
                transfer.adults,
                transfer.children,
                transfer.infants,
                transfer.currency,
            )
            transfer_contract_groups.setdefault(contract_key, []).append(transfer)
        transfers = tuple(
            item
            for contract_key in sorted(
                transfer_contract_groups,
                key=lambda value: tuple(str(part) for part in value),
            )
            for item in self._take_diverse_transfers(
                tuple(transfer_contract_groups[contract_key]),
                self.LIVE_TRANSFER_LIMIT_PER_CONTRACT_BUCKET,
            )
        )
        return PackageInventory(
            flights=flights,
            lodgings=lodgings,
            transfers=transfers,
        )

    def _take_diverse_flights(
        self,
        flights: tuple[NormalizedFlightQuote, ...],
        limit: int,
    ) -> tuple[NormalizedFlightQuote, ...]:
        ordered = tuple(sorted(flights, key=lambda item: (item.total_for_party_cents, item.id)))
        selected: dict[str, NormalizedFlightQuote] = {}
        feature_seen: set[tuple[str, str]] = set()
        for flight in ordered:
            features = (
                ("provider", flight.provider),
                ("cabin", flight.cabin_class or "unknown"),
                ("fare_rules", "present" if flight.fare_rule_summary else "missing"),
                (
                    "baggage",
                    "known" if flight.checked_baggage_per_adult_kg is not None else "unknown",
                ),
            )
            if any(feature not in feature_seen for feature in features):
                selected[flight.id] = flight
                feature_seen.update(features)
            if len(selected) >= limit:
                break
        for flight in ordered:
            if len(selected) >= limit:
                break
            selected.setdefault(flight.id, flight)
        return tuple(
            sorted(selected.values(), key=lambda item: (item.total_for_party_cents, item.id))
        )

    def _take_diverse_lodgings(
        self,
        lodgings: tuple[NormalizedLodgingQuote, ...],
        limit: int,
    ) -> tuple[NormalizedLodgingQuote, ...]:
        ordered = tuple(sorted(lodgings, key=lambda item: (item.total_for_party_cents, item.id)))
        selected: dict[str, NormalizedLodgingQuote] = {}
        feature_seen: set[tuple[str, str]] = set()
        for lodging in ordered:
            features = (
                ("provider", lodging.provider),
                ("breakfast", str(lodging.breakfast_included)),
                (
                    "cancellation",
                    "present" if lodging.cancellation_policy else "missing",
                ),
                ("payment", "present" if lodging.payment_policy else "missing"),
                (
                    "official_room_identity",
                    "present" if lodging.provider_room_id else "missing",
                ),
            )
            if any(feature not in feature_seen for feature in features):
                selected[lodging.id] = lodging
                feature_seen.update(features)
            if len(selected) >= limit:
                break
        for lodging in ordered:
            if len(selected) >= limit:
                break
            selected.setdefault(lodging.id, lodging)
        return tuple(
            sorted(selected.values(), key=lambda item: (item.total_for_party_cents, item.id))
        )

    def _take_diverse_transfers(
        self,
        transfers: tuple[TransferOption, ...],
        limit: int,
    ) -> tuple[TransferOption, ...]:
        price_order = tuple(
            sorted(transfers, key=lambda item: (item.total_for_party_cents, item.id))
        )
        schedule_order = tuple(
            sorted(
                transfers,
                key=lambda item: (
                    item.earliest_departure_at,
                    item.latest_departure_at,
                    item.id,
                ),
            )
        )
        selected: dict[str, TransferOption] = {}

        def add(item: TransferOption) -> None:
            if len(selected) < limit or item.id in selected:
                selected.setdefault(item.id, item)

        if price_order:
            add(price_order[0])
            add(schedule_order[0])
            add(schedule_order[len(schedule_order) // 2])
            add(schedule_order[-1])
        for provider in sorted({item.provider for item in transfers}):
            add(next(item for item in price_order if item.provider == provider))
        for reservation_state in (False, True, None):
            matching = tuple(
                item for item in price_order if item.requires_reservation is reservation_state
            )
            if matching:
                add(matching[0])
        for item in price_order:
            add(item)
        return tuple(
            sorted(
                selected.values(),
                key=lambda item: (
                    item.earliest_departure_at,
                    item.total_for_party_cents,
                    item.id,
                ),
            )
        )

    def _structural_candidate_upper_bound(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
    ) -> int:
        flights = tuple(
            flight
            for flight in inventory.flights
            if flight.availability == QuoteAvailability.AVAILABLE
            and flight.origin == intent.origin
            and flight.destination == intent.destination
            and flight.outbound_depart_at.date() == intent.start_date
            and flight.return_depart_at.date() == intent.end_date
            and flight.adults == intent.adults
            and flight.children == intent.children
            and flight.infants == intent.infants
            and flight.currency == intent.currency
        )
        continuous_areas = (
            (PackageArea.DESTINATION_ISLAND,)
            if intent.destination_place_key == PackagePlaceKey.MAAFUSHI
            else (PackageArea.AIRPORT_ISLAND,)
            if intent.destination_place_key == PackagePlaceKey.HULHUMALE
            else (PackageArea.DESTINATION_ISLAND, PackageArea.AIRPORT_ISLAND)
        )

        def count(area: PackageArea, start: date, end: date) -> int:
            return sum(
                self._matching_lodging(lodging, intent, area, start, end)
                for lodging in inventory.lodgings
            )

        upper_bound = 0
        for flight in flights:
            stay_start = flight.outbound_arrive_at.date()
            stay_end = flight.return_depart_at.date()
            continuous = sum(count(area, stay_start, stay_end) for area in continuous_areas)
            split = 0
            if (stay_end - stay_start).days >= 3:
                split = (
                    count(
                        PackageArea.AIRPORT_ISLAND,
                        stay_start,
                        stay_start + timedelta(days=1),
                    )
                    * count(
                        PackageArea.DESTINATION_ISLAND,
                        stay_start + timedelta(days=1),
                        stay_end - timedelta(days=1),
                    )
                    * count(
                        PackageArea.AIRPORT_ISLAND,
                        stay_end - timedelta(days=1),
                        stay_end,
                    )
                )
            upper_bound += continuous + split
        # The bounded generator retains one additional provider-constrained
        # Maafushi/iCom transfer variant whenever the inventory contains the
        # official public-transfer contract.  Include that finite alternative
        # in the structural envelope so the audit cannot undercount the join.
        if any(transfer.provider == "icom-public-transfer" for transfer in inventory.transfers):
            upper_bound *= 2
        return upper_bound

    def _inventory_counts(self, inventory: PackageInventory) -> dict[str, int]:
        return {
            "flights": len(inventory.flights),
            "lodgings": len(inventory.lodgings),
            "transfers": len(inventory.transfers),
        }

    def _inventory_ids_sha256(self, inventory: PackageInventory) -> str:
        payload = {
            "flights": sorted(item.id for item in inventory.flights),
            "lodgings": sorted(item.id for item in inventory.lodgings),
            "transfers": sorted(item.id for item in inventory.transfers),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def rank_candidates(
        self,
        intent: PackageIntent,
        candidates: tuple[TravelPackageCandidate, ...],
    ) -> tuple[TravelPackageCandidate, ...]:
        def rank_key(item: TravelPackageCandidate) -> tuple[int, int, int, int, int, str]:
            budget = package_budget(item)
            # A complete CNY party total is the only directly comparable primary
            # price.  Unknown taxes, foreign subtotals, and partial party totals
            # must never outrank it merely because they have more contracts.
            complete_cny = int(budget.is_all_in_total and item.currency == "CNY")
            return (
                -complete_cny,
                published_base_fare_contract_count(item.transfers),
                item.declared_total_cents,
                sum(lodging_quality_rank(lodging) for lodging in item.lodgings),
                max(lodging_quality_rank(lodging) for lodging in item.lodgings),
                item.id,
            )

        baseline = tuple(sorted(candidates, key=rank_key))
        mode, weight = _effective_breakfast_preference(intent)
        if mode != PreferenceMode.WEIGHTED or weight is None or weight <= 0 or len(baseline) < 2:
            return baseline
        ranked: list[TravelPackageCandidate] = []
        # Keep the same ordering as the baseline: ``rank_key[0]`` is already
        # negative for the preferred complete-CNY tier.  Inverting it here
        # would make incomplete/foreign totals the first weighted tier.
        tiers = sorted({(rank_key(candidate)[0], rank_key(candidate)[1]) for candidate in baseline})
        preference_weight = Decimal(str(weight))
        price_weight = Decimal(1) - preference_weight
        for tier in tiers:
            comparable = tuple(
                candidate
                for candidate in baseline
                if (rank_key(candidate)[0], rank_key(candidate)[1]) == tier
            )
            if len(comparable) < 2:
                ranked.extend(comparable)
                continue
            minimum_price = min(candidate.declared_total_cents for candidate in comparable)
            maximum_price = max(candidate.declared_total_cents for candidate in comparable)
            price_span = maximum_price - minimum_price

            def utility(
                candidate: TravelPackageCandidate,
                *,
                maximum: int = maximum_price,
                span: int = price_span,
            ) -> Decimal:
                price_utility = (
                    Decimal(1)
                    if span == 0
                    else Decimal(maximum - candidate.declared_total_cents) / Decimal(span)
                )
                breakfast_utility, _ = _breakfast_coverage(candidate)
                return price_weight * price_utility + preference_weight * breakfast_utility

            ranked.extend(
                sorted(
                    comparable,
                    key=lambda item: (
                        -utility(item),
                        item.declared_total_cents,
                        item.id,
                    ),
                )
            )
        return tuple(ranked)

    def _continuous_candidates(
        self,
        intent: PackageIntent,
        flight: NormalizedFlightQuote,
        inventory: PackageInventory,
        *,
        limit: int | None = None,
    ) -> list[TravelPackageCandidate]:
        result: list[TravelPackageCandidate] = []
        branches: tuple[tuple[PackageArea, PackageCandidateKind], ...]
        if intent.destination_place_key == PackagePlaceKey.MAAFUSHI:
            branches = (
                (
                    PackageArea.DESTINATION_ISLAND,
                    PackageCandidateKind.CONTINUOUS_ISLAND,
                ),
            )
        elif intent.destination_place_key == PackagePlaceKey.HULHUMALE:
            branches = (
                (
                    PackageArea.AIRPORT_ISLAND,
                    PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
                ),
            )
        else:
            branches = (
                (
                    PackageArea.DESTINATION_ISLAND,
                    PackageCandidateKind.CONTINUOUS_ISLAND,
                ),
                (
                    PackageArea.AIRPORT_ISLAND,
                    PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
                ),
            )
        stay_start = flight.outbound_arrive_at.date()
        stay_end = flight.return_depart_at.date()
        for area, kind in branches:
            stays = (
                lodging
                for lodging in inventory.lodgings
                if self._matching_lodging(
                    lodging,
                    intent,
                    area,
                    stay_start,
                    stay_end,
                )
            )
            legs = _required_transfer_legs(intent, kind, flight=flight)
            for stay in stays:
                if limit is not None and len(result) >= limit:
                    return result
                selected_transfers = self._select_transfers(
                    intent,
                    inventory,
                    legs,
                    lodgings=(stay,),
                    flight=flight,
                    kind=kind,
                )
                selected_variants = [selected_transfers] if selected_transfers is not None else []
                # A frozen Maafushi plan has an exact iCom public-transfer
                # contract.  The ordinary score quite correctly prefers a
                # cheaper hotel-bound transfer, but that single winner would
                # disappear when live-v4 applies the frozen contract.  Keep
                # one provider-constrained variant in the bounded candidate
                # pool so the later frozen join can consume the real iCom
                # evidence without treating another provider as equivalent.
                if area == PackageArea.DESTINATION_ISLAND:
                    public_variant = self._select_transfers(
                        intent,
                        inventory,
                        legs,
                        lodgings=(stay,),
                        flight=flight,
                        kind=kind,
                        required_provider_by_leg=(
                            "icom-public-transfer",
                            "icom-public-transfer",
                        ),
                    )
                    if public_variant is not None and public_variant != selected_transfers:
                        selected_variants.append(public_variant)
                for transfers in selected_variants:
                    assert transfers is not None
                    result.append(
                        self._candidate(
                            intent,
                            kind,
                            flight,
                            (stay,),
                            transfers,
                        )
                    )
        return result

    def _split_candidates(
        self,
        intent: PackageIntent,
        flight: NormalizedFlightQuote,
        inventory: PackageInventory,
        *,
        limit: int | None = None,
    ) -> list[TravelPackageCandidate]:
        if intent.night_count < 3:
            return []
        stay_start = flight.outbound_arrive_at.date()
        stay_end = flight.return_depart_at.date()
        first_checkout = stay_start + timedelta(days=1)
        last_checkin = stay_end - timedelta(days=1)
        first_stays = tuple(
            lodging
            for lodging in inventory.lodgings
            if self._matching_lodging(
                lodging,
                intent,
                PackageArea.AIRPORT_ISLAND,
                stay_start,
                first_checkout,
            )
        )
        middle_stays = tuple(
            lodging
            for lodging in inventory.lodgings
            if self._matching_lodging(
                lodging,
                intent,
                PackageArea.DESTINATION_ISLAND,
                first_checkout,
                last_checkin,
            )
        )
        last_stays = tuple(
            lodging
            for lodging in inventory.lodgings
            if self._matching_lodging(
                lodging,
                intent,
                PackageArea.AIRPORT_ISLAND,
                last_checkin,
                stay_end,
            )
        )
        legs = _required_transfer_legs(
            intent,
            PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
            flight=flight,
        )
        result: list[TravelPackageCandidate] = []
        for first in first_stays:
            for middle in middle_stays:
                for last in last_stays:
                    if limit is not None and len(result) >= limit:
                        return result
                    lodgings = (first, middle, last)
                    selected_transfers = self._select_transfers(
                        intent,
                        inventory,
                        legs,
                        lodgings=lodgings,
                        flight=flight,
                        kind=PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
                    )
                    selected_variants = (
                        [selected_transfers] if selected_transfers is not None else []
                    )
                    public_variant = self._select_transfers(
                        intent,
                        inventory,
                        legs,
                        lodgings=lodgings,
                        flight=flight,
                        kind=PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
                        required_provider_by_leg=tuple(
                            (
                                "icom-public-transfer"
                                if PackageArea.DESTINATION_ISLAND in {origin, destination}
                                else None
                            )
                            for origin, destination, _ in legs
                        ),
                    )
                    if public_variant is not None and public_variant != selected_transfers:
                        selected_variants.append(public_variant)
                    for transfers in selected_variants:
                        assert transfers is not None
                        result.append(
                            self._candidate(
                                intent,
                                PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
                                flight,
                                lodgings,
                                transfers,
                            )
                        )
        return result

    def _matching_lodging(
        self,
        lodging: NormalizedLodgingQuote,
        intent: PackageIntent,
        area: PackageArea,
        check_in: date,
        check_out: date,
    ) -> bool:
        return (
            lodging.availability == QuoteAvailability.AVAILABLE
            and lodging.area == area
            and lodging.check_in == check_in
            and lodging.check_out == check_out
            and lodging.adults == intent.adults
            and lodging.children == intent.children
            and lodging.infants == intent.infants
            and lodging.rooms == intent.rooms
            and (not intent.require_non_basic_lodging or not lodging_basic_markers(lodging))
            and (
                not intent.require_non_remote_lodging
                or (
                    lodging.location_convenience == LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
                    and lodging_non_remote_evidence_confirmed(
                        lodging.location_address,
                        lodging.nearby_location_evidence,
                    )
                )
            )
            and (
                intent.destination_place_key is None
                or (
                    intent.destination_place_key == PackagePlaceKey.MAAFUSHI
                    and area != PackageArea.DESTINATION_ISLAND
                )
                or (
                    intent.destination_place_key == PackagePlaceKey.HULHUMALE
                    and area != PackageArea.AIRPORT_ISLAND
                )
                or lodging.place_key == intent.destination_place_key
            )
        )

    def _select_transfers(
        self,
        intent: PackageIntent,
        inventory: PackageInventory,
        legs: tuple[tuple[PackageArea, PackageArea, date], ...],
        *,
        lodgings: tuple[NormalizedLodgingQuote, ...],
        flight: NormalizedFlightQuote,
        kind: PackageCandidateKind,
        required_provider_by_leg: tuple[str | None, ...] | None = None,
    ) -> tuple[TransferOption, ...] | None:
        if required_provider_by_leg is not None and len(required_provider_by_leg) != len(legs):
            raise ValueError("required transfer providers must match transfer legs")
        matches_by_leg: list[tuple[TransferOption, ...]] = []
        for leg_index, (origin, destination, travel_date) in enumerate(legs):
            required_provider = (
                required_provider_by_leg[leg_index]
                if required_provider_by_leg is not None
                else None
            )
            matches = tuple(
                transfer
                for transfer in inventory.transfers
                if transfer.availability == QuoteAvailability.AVAILABLE
                and transfer.origin_area == origin
                and transfer.destination_area == destination
                and transfer.travel_date == travel_date
                and transfer.adults == intent.adults
                and transfer.children == intent.children
                and transfer.infants == intent.infants
                and (required_provider is None or transfer.provider == required_provider)
                and (
                    (
                        transfer.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
                        and transfer.currency == intent.currency
                    )
                    or transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                )
                and transfer_binding_error(transfer, lodgings) is None
                and transfer_place_error(intent, transfer, lodgings) is None
            )
            if not matches:
                return None
            matches_by_leg.append(matches)

        def schedule_preference_penalty(items: tuple[TransferOption, ...]) -> int:
            """Prefer the requested safe-window ferry observations when available.

            The current Maldives run should prefer the earliest safe 15:25
            airport->Maafushi observation and the 09:30 Maafushi->airport
            observation.  This tie-breaker only runs
            for the continuous Maafushi branch; it never turns an unsafe
            schedule into an acceptable one and falls back to the normal
            connection/price ordering when those observations are absent.
            """

            if kind != PackageCandidateKind.CONTINUOUS_ISLAND:
                return 0
            penalty = 0
            outbound_target = time(15, 25)
            inbound_target = time(9, 30)
            for item in items:
                if item.depart_at is None:
                    continue
                if (
                    item.origin_area == PackageArea.AIRPORT
                    and item.destination_area == PackageArea.DESTINATION_ISLAND
                    and item.travel_date == flight.outbound_arrive_at.date()
                ):
                    target = outbound_target
                elif (
                    item.origin_area == PackageArea.DESTINATION_ISLAND
                    and item.destination_area == PackageArea.AIRPORT
                    and item.travel_date == flight.return_depart_at.date()
                ):
                    target = inbound_target
                else:
                    continue
                penalty += abs(
                    (item.depart_at.hour * 60 + item.depart_at.minute)
                    - (target.hour * 60 + target.minute)
                )
            return penalty

        def score(items: tuple[TransferOption, ...]) -> tuple[int, int, int, int, tuple[str, ...]]:
            return (
                self._connection_penalty(intent, flight, kind, items),
                schedule_preference_penalty(items),
                len(
                    {
                        item.price_contract_id
                        for item in items
                        if item.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                    }
                ),
                transfer_contract_total_cents(items, currency=intent.currency),
                tuple(item.id for item in items),
            )

        beam: tuple[tuple[TransferOption, ...], ...] = ((),)
        for matches in matches_by_leg:
            expanded = tuple((*partial, option) for partial in beam for option in matches)
            beam = tuple(sorted(expanded, key=score)[: self.LIVE_TRANSFER_BEAM_WIDTH])
        return min(beam, key=score)

    def _connection_penalty(
        self,
        intent: PackageIntent,
        flight: NormalizedFlightQuote,
        kind: PackageCandidateKind,
        transfers: tuple[TransferOption, ...],
    ) -> int:
        penalty = 0
        for transfer in transfers:
            not_before, arrive_by = _transfer_connection_limits(
                intent,
                flight,
                kind,
                transfer,
            )
            if not transfer.has_feasible_departure(
                not_before=not_before,
                arrive_by=arrive_by,
            ):
                penalty += 1
        if kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND:
            penalty += sum(
                not _transfers_can_connect(
                    first,
                    second,
                    minimum_connection_minutes=intent.minimum_transfer_connection_minutes,
                )
                for first, second in _split_connection_pairs(intent, transfers, flight=flight)
            )
        return penalty

    def _candidate(
        self,
        intent: PackageIntent,
        kind: PackageCandidateKind,
        flight: NormalizedFlightQuote,
        lodgings: tuple[NormalizedLodgingQuote, ...],
        transfers: tuple[TransferOption, ...],
    ) -> TravelPackageCandidate:
        component_ids = (
            flight.id,
            *(item.id for item in lodgings),
            *(item.id for item in transfers),
        )
        digest = hashlib.sha256("|".join(component_ids).encode()).hexdigest()[:12]
        flight_total = (
            flight.total_for_party_cents
            if flight.party_total_known and flight.total_for_party_cents is not None
            else 0
        )
        total = (
            flight_total
            + sum(
                item.total_for_party_cents for item in lodgings if item.currency == intent.currency
            )
            + transfer_contract_total_cents(
                transfers,
                currency=intent.currency,
            )
        )
        return TravelPackageCandidate(
            id=f"{intent.trip_id}:package:{kind.value}:{digest}:v1",
            trip_id=intent.trip_id,
            kind=kind,
            flight=flight,
            lodgings=lodgings,
            transfers=transfers,
            declared_total_cents=total,
            currency=intent.currency,
        )


class PackageVerifier:
    def verify(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
        *,
        now: datetime | None = None,
    ) -> tuple[PackageViolation, ...]:
        reference = now or datetime.now(UTC)
        violations: list[PackageViolation] = []
        violations.extend(self._check_dates(intent, candidate))
        violations.extend(self._check_party(intent, candidate))
        violations.extend(self._check_party_availability(candidate))
        violations.extend(self._check_transfer_availability(candidate))
        violations.extend(self._check_currency(intent, candidate))
        violations.extend(self._check_lodging_structure(intent, candidate))
        violations.extend(self._check_transfer_price_contracts(candidate))
        violations.extend(self._check_total(candidate))
        violations.extend(self._check_quote_truth(candidate, reference))
        violations.extend(self._check_quote_capture_skew(intent, candidate))
        violations.extend(self._check_transfer_price_guarantees(intent, candidate))
        violations.extend(self._check_night_coverage(intent, candidate))
        violations.extend(self._check_transfer_bindings(candidate))
        violations.extend(self._check_transfer_places(intent, candidate))
        violations.extend(self._check_transfer_chain(intent, candidate))
        violations.extend(self._check_transfer_sequence(intent, candidate))
        violations.extend(self._check_connection_risk(intent, candidate))
        violations.extend(self._check_preferences(intent, candidate))
        violations.extend(self._check_budget(intent, candidate))
        return tuple(violations)

    def errors(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
        *,
        now: datetime | None = None,
    ) -> tuple[PackageViolation, ...]:
        return tuple(
            item
            for item in self.verify(intent, candidate, now=now)
            if item.severity == PackageViolationSeverity.ERROR
        )

    def _check_dates(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        return list(package_date_violations(intent, candidate))

    def _check_party(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        quotes: tuple[PackageQuote, ...] = (
            candidate.flight,
            *candidate.lodgings,
            *candidate.transfers,
        )
        expected_party = (intent.adults, intent.children, intent.infants)
        mismatches = [
            quote.id
            for quote in quotes
            if (quote.adults, quote.children, quote.infants) != expected_party
        ]
        mismatches.extend(
            quote.id for quote in quotes if quote.children_ages != intent.children_ages
        )
        mismatches.extend(
            lodging.id for lodging in candidate.lodgings if lodging.rooms != intent.rooms
        )
        if not mismatches:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.PARTY_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="报价并非针对完整出行人数和房间数",
                component_ids=tuple(dict.fromkeys(mismatches)),
                details={
                    "expected_adults": intent.adults,
                    "expected_children": intent.children,
                    "expected_infants": intent.infants,
                    "expected_rooms": intent.rooms,
                },
            )
        ]

    def _check_party_availability(
        self,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        if candidate.flight.party_availability_confirmed:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED,
                severity=PackageViolationSeverity.ERROR,
                message="航班报价未确认请求的完整出行人数仍有可用库存",
                component_ids=(candidate.flight.id,),
                details={
                    "requested_adults": candidate.flight.adults,
                    "requested_children": candidate.flight.children,
                    "requested_infants": candidate.flight.infants,
                    "party_availability_confirmed": False,
                },
            )
        ]

    def _check_transfer_availability(
        self,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        """A transfer with unknown service timing cannot pass final gate."""
        pending = tuple(
            transfer.id
            for transfer in candidate.transfers
            if transfer.availability != QuoteAvailability.AVAILABLE
        )
        if not pending:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_AVAILABILITY_UNCONFIRMED,
                severity=PackageViolationSeverity.ERROR,
                message="接驳已确认包含权益但具体服务时段未确认，不能进入最终可执行方案",
                component_ids=pending,
            )
        ]

    def _check_currency(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        hard_mismatches = [
            quote.id for quote in (candidate.flight,) if quote.currency != intent.currency
        ]
        hard_mismatches.extend(
            transfer.id
            for transfer in candidate.transfers
            if (
                transfer.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
                and transfer.currency != intent.currency
            )
        )
        if candidate.currency != intent.currency:
            hard_mismatches.append(candidate.id)
        foreign_lodgings = tuple(
            lodging.id for lodging in candidate.lodgings if lodging.currency != intent.currency
        )
        if hard_mismatches:
            return [
                PackageViolation(
                    code=PackageViolationCode.CURRENCY_MISMATCH,
                    severity=PackageViolationSeverity.ERROR,
                    message="整包中存在未经确定性换汇的航班或全包接驳币种",
                    component_ids=tuple(hard_mismatches),
                )
            ]
        if not foreign_lodgings:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.CURRENCY_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="住宿存在未换算外币，不能参与完整人民币总价最优或接受",
                component_ids=foreign_lodgings,
            )
        ]

    def _check_total(self, candidate: TravelPackageCandidate) -> list[PackageViolation]:
        if candidate.declared_total_cents == candidate.computed_total_cents:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TOTAL_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="声明的两人总价与逐项整数分求和不一致",
                component_ids=candidate.component_ids,
                details={
                    "declared_cents": candidate.declared_total_cents,
                    "computed_cents": candidate.computed_total_cents,
                },
            )
        ]

    def _check_quote_truth(
        self,
        candidate: TravelPackageCandidate,
        now: datetime,
    ) -> list[PackageViolation]:
        violations: list[PackageViolation] = []
        quotes: tuple[PackageQuote, ...] = (
            candidate.flight,
            *candidate.lodgings,
            *candidate.transfers,
        )
        incomplete = tuple(
            quote.id
            for quote in quotes
            if not quote.taxes_and_fees_included
            and (
                not isinstance(quote, TransferOption)
                or quote.price_guarantee == TransferPriceGuarantee.ALL_IN_CONFIRMED
            )
        )
        if incomplete:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.TAXES_INCOMPLETE,
                    severity=PackageViolationSeverity.ERROR,
                    message="存在未确认含税费的报价，不能进入整包总预算",
                    component_ids=incomplete,
                )
            )
        stale = tuple(quote.id for quote in quotes if not quote.is_fresh(now))
        if stale:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.STALE_QUOTE,
                    severity=PackageViolationSeverity.ERROR,
                    message="存在已过期或尚未生效的报价",
                    component_ids=stale,
                )
            )
        sold_out = tuple(
            quote.id for quote in quotes if quote.availability == QuoteAvailability.SOLD_OUT
        )
        if sold_out:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.SOLD_OUT,
                    severity=PackageViolationSeverity.ERROR,
                    message="整包引用了已售罄组件",
                    component_ids=sold_out,
                )
            )
        missing = tuple(quote.id for quote in quotes if not quote.evidence_refs)
        if missing:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.MISSING_EVIDENCE,
                    severity=PackageViolationSeverity.ERROR,
                    message="整包组件缺少报价证据引用",
                    component_ids=missing,
                )
            )
        if not candidate.flight.party_total_known:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.BUDGET_NOT_FULLY_VERIFIED,
                    severity=PackageViolationSeverity.WARNING,
                    message=(
                        "航班仅有每成人显示价；同一票价产品的 1/2 成人对照不足，"
                        "机票两人总价未计入整包总价"
                    ),
                    component_ids=(candidate.flight.id,),
                    details={
                        "party_total_known": False,
                        "price_basis": candidate.flight.price_basis,
                        "display_amount_cents": candidate.flight.display_amount_cents,
                    },
                )
            )
        return violations

    def _check_quote_capture_skew(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        """Reject packages assembled from observations captured too far apart.

        Individual TTL checks are insufficient for a cross-platform package: a
        just-captured hotel can otherwise be combined with a flight that was
        captured near the end of its own freshness window.  The skew gate keeps
        the final budget evidence temporally coherent without pretending that
        the components are atomically locked.
        """

        quotes: tuple[PackageQuote, ...] = (
            candidate.flight,
            *candidate.lodgings,
            *candidate.transfers,
        )
        oldest = min(item.captured_at for item in quotes)
        newest = max(item.captured_at for item in quotes)
        skew_seconds = int((newest - oldest).total_seconds())
        allowed_seconds = intent.maximum_quote_capture_skew_minutes * 60
        if skew_seconds <= allowed_seconds:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.QUOTE_CAPTURE_SKEW,
                severity=PackageViolationSeverity.ERROR,
                message="整包组件的抓取时间差过大，必须重新核价后才能比较总预算",
                component_ids=tuple(item.id for item in quotes),
                details={
                    "capture_skew_seconds": skew_seconds,
                    "maximum_capture_skew_seconds": allowed_seconds,
                },
            )
        ]

    def _check_transfer_price_guarantees(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        published = tuple(
            transfer
            for transfer in candidate.transfers
            if transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
        )
        if not published:
            return []
        supplemental = supplemental_published_base_fares(candidate.transfers)
        amounts = ", ".join(
            _money(item.currency, item.total_for_party_cents) for item in supplemental
        )
        violations = [
            PackageViolation(
                code=PackageViolationCode.PUBLISHED_BASE_FARE_NOT_ALL_IN,
                severity=PackageViolationSeverity.WARNING,
                message=(
                    f"接驳仅有公开基础价 {amounts}；税费未知，未换汇且未计入"
                    f" {candidate.currency} 已确认小计"
                ),
                component_ids=tuple(item.id for item in published),
                details={
                    "taxes_and_fees_confirmed": False,
                    "taxes_and_fees_status": "unknown",
                    "included_in_confirmed_subtotal": False,
                },
            )
        ]
        if intent.budget_cents is not None:
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.BUDGET_NOT_FULLY_VERIFIED,
                    severity=PackageViolationSeverity.WARNING,
                    message=(
                        "用户给出了总预算，但公开接驳基础价的税费和确定性换汇均未知，"
                        "只能核验同币种全包小计，不能声称整趟预算合规已被证明"
                    ),
                    component_ids=tuple(item.id for item in published),
                    details={
                        "confirmed_subtotal_cents": candidate.computed_total_cents,
                        "budget_cents": intent.budget_cents,
                        "budget_compliance_fully_verified": False,
                    },
                )
            )
        return violations

    def _check_night_coverage(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        stay_start = candidate.flight.outbound_arrive_at.date()
        stay_end = candidate.flight.return_depart_at.date()
        expected = {
            stay_start + timedelta(days=offset): 0 for offset in range((stay_end - stay_start).days)
        }
        outside = False
        for lodging in candidate.lodgings:
            night = lodging.check_in
            while night < lodging.check_out:
                if night not in expected:
                    outside = True
                else:
                    expected[night] += 1
                night += timedelta(days=1)
        invalid = tuple(night.isoformat() for night, count in expected.items() if count != 1)
        if not invalid and not outside:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.LODGING_NIGHT_COVERAGE,
                severity=PackageViolationSeverity.ERROR,
                message="住宿没有逐晚且仅一次覆盖完整行程",
                component_ids=tuple(item.id for item in candidate.lodgings),
                details={"invalid_nights": ",".join(invalid), "outside_trip": outside},
            )
        ]

    def _check_lodging_structure(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        stay_start = candidate.flight.outbound_arrive_at.date()
        stay_end = candidate.flight.return_depart_at.date()
        if candidate.kind == PackageCandidateKind.CONTINUOUS_ISLAND:
            expected = Counter({(PackageArea.DESTINATION_ISLAND, stay_start, stay_end): 1})
        elif candidate.kind == PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND:
            expected = Counter({(PackageArea.AIRPORT_ISLAND, stay_start, stay_end): 1})
        else:
            first_checkout = stay_start + timedelta(days=1)
            last_checkin = stay_end - timedelta(days=1)
            expected = Counter(
                {
                    (PackageArea.AIRPORT_ISLAND, stay_start, first_checkout): 1,
                    (
                        PackageArea.DESTINATION_ISLAND,
                        first_checkout,
                        last_checkin,
                    ): 1,
                    (PackageArea.AIRPORT_ISLAND, last_checkin, stay_end): 1,
                }
            )
        actual = Counter(
            (lodging.area, lodging.check_in, lodging.check_out) for lodging in candidate.lodgings
        )
        place_mismatch = tuple(
            lodging.id
            for lodging in candidate.lodgings
            if (
                intent.destination_place_key == PackagePlaceKey.MAAFUSHI
                and lodging.area == PackageArea.DESTINATION_ISLAND
                and lodging.place_key != PackagePlaceKey.MAAFUSHI
            )
            or (
                intent.destination_place_key == PackagePlaceKey.HULHUMALE
                and lodging.area == PackageArea.AIRPORT_ISLAND
                and lodging.place_key != PackagePlaceKey.HULHUMALE
            )
        )
        if actual == expected and not place_mismatch:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.LODGING_STRUCTURE_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="住宿区域与候选类型或首中末分段边界不一致",
                component_ids=place_mismatch or tuple(item.id for item in candidate.lodgings),
                details={
                    "expected_segment_count": sum(expected.values()),
                    "actual_segment_count": len(candidate.lodgings),
                },
            )
        ]

    def _check_transfer_price_contracts(
        self,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        by_contract: dict[str, list[TransferOption]] = {}
        for transfer in candidate.transfers:
            by_contract.setdefault(transfer.price_contract_id, []).append(transfer)
        invalid: set[str] = set()
        for group in by_contract.values():
            first = group[0]
            terms_match = all(
                first.price_scope == transfer.price_scope
                and first.purchase_scope == transfer.purchase_scope
                and first.price_guarantee == transfer.price_guarantee
                and first.bound_lodging_id == transfer.bound_lodging_id
                and first.provider == transfer.provider
                and first.currency == transfer.currency
                and first.total_for_party_cents == transfer.total_for_party_cents
                and first.taxes_and_fees_included == transfer.taxes_and_fees_included
                and first.adults == transfer.adults
                for transfer in group[1:]
            )
            if len(group) == 1:
                if not terms_match:
                    invalid.update(item.id for item in group)
                continue
            reciprocal = bool(
                len(group) == 2
                and all(transfer.price_scope == TransferPriceScope.ROUND_TRIP for transfer in group)
                and group[0].origin_area == group[1].destination_area
                and group[0].destination_area == group[1].origin_area
                and group[0].origin_place_key == group[1].destination_place_key
                and group[0].destination_place_key == group[1].origin_place_key
            )
            if not terms_match or not reciprocal:
                invalid.update(item.id for item in group)
        if not invalid:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_PRICE_CONTRACT_INVALID,
                severity=PackageViolationSeverity.ERROR,
                message="往返接驳价格合同只能由条款一致的互补去返程腿共享一次",
                component_ids=tuple(sorted(invalid)),
            )
        ]

    def _check_transfer_chain(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        required = _required_transfer_legs(intent, candidate.kind, flight=candidate.flight)
        available = Counter(
            (item.origin_area, item.destination_area, item.travel_date)
            for item in candidate.transfers
        )
        required_counts = Counter(required)
        missing = tuple(
            f"{origin.value}->{destination.value}@{travel_date.isoformat()}"
            for (origin, destination, travel_date), count in required_counts.items()
            if available[(origin, destination, travel_date)] < count
        )
        unexpected = tuple(
            f"{origin.value}->{destination.value}@{travel_date.isoformat()}"
            for (origin, destination, travel_date), count in available.items()
            if count != required_counts[(origin, destination, travel_date)]
        )
        if available == required_counts:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_CHAIN_INCOMPLETE,
                severity=PackageViolationSeverity.ERROR,
                message="航班与住宿之间的接驳链存在缺失、重复或额外腿",
                component_ids=tuple(item.id for item in candidate.transfers),
                details={
                    "missing_legs": ",".join(missing),
                    "unexpected_legs": ",".join(unexpected),
                },
            )
        ]

    def _check_transfer_sequence(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        if candidate.kind != PackageCandidateKind.SPLIT_AIRPORT_ISLAND:
            return []
        invalid = tuple(
            (first, second)
            for first, second in _split_connection_pairs(
                intent,
                candidate.transfers,
                flight=candidate.flight,
            )
            if not _transfers_can_connect(
                first,
                second,
                minimum_connection_minutes=intent.minimum_transfer_connection_minutes,
            )
        )
        if not invalid and len(candidate.transfers) == 6:
            return []
        component_ids = tuple(dict.fromkeys(item.id for pair in invalid for item in pair))
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_CONNECTION_INFEASIBLE,
                severity=PackageViolationSeverity.ERROR,
                message="分段住宿的同日接驳腿顺序错误或没有可衔接班次",
                component_ids=component_ids or tuple(item.id for item in candidate.transfers),
                details={
                    "required_transfer_count": 6,
                    "actual_transfer_count": len(candidate.transfers),
                },
            )
        ]

    def _check_transfer_bindings(
        self,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        invalid = tuple(
            (transfer.id, error)
            for transfer in candidate.transfers
            if (error := transfer_binding_error(transfer, candidate.lodgings)) is not None
        )
        if not invalid:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_BINDING_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="整包引用了不属于所选住宿或日期方向不匹配的酒店专属接驳",
                component_ids=tuple(item_id for item_id, _ in invalid),
                details={
                    "binding_errors": "; ".join(
                        f"{item_id}: {message}" for item_id, message in invalid
                    )
                },
            )
        ]

    def _check_transfer_places(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        invalid = tuple(
            (transfer.id, error)
            for transfer in candidate.transfers
            if (
                error := transfer_place_error(
                    intent,
                    transfer,
                    candidate.lodgings,
                )
            )
            is not None
        )
        if not invalid:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.TRANSFER_PLACE_MISMATCH,
                severity=PackageViolationSeverity.ERROR,
                message="公开接驳的精确地点与所选目的岛或住宿不一致",
                component_ids=tuple(item_id for item_id, _ in invalid),
                details={
                    "place_errors": "; ".join(
                        f"{item_id}: {message}" for item_id, message in invalid
                    )
                },
            )
        ]

    def _check_connection_risk(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        violations: list[PackageViolation] = []
        arrival_transfers = [
            item
            for item in candidate.transfers
            if item.origin_area == PackageArea.AIRPORT and item.travel_date == intent.start_date
        ]
        for transfer in arrival_transfers:
            not_before, _ = _transfer_connection_limits(
                intent,
                candidate.flight,
                candidate.kind,
                transfer,
            )
            if not_before is None or transfer.has_feasible_departure(not_before=not_before):
                continue
            latest_gap = int(
                (transfer.latest_departure_at - candidate.flight.outbound_arrive_at).total_seconds()
                // 60
            )
            required = int((not_before - candidate.flight.outbound_arrive_at).total_seconds() // 60)
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
                    severity=PackageViolationSeverity.ERROR,
                    message="落地后没有满足最小缓冲的明确接驳班次或服务窗口",
                    component_ids=(candidate.flight.id, transfer.id),
                    details={
                        "latest_available_gap_minutes": latest_gap,
                        "required_minutes": required,
                    },
                )
            )
        final_airport_transfers = [
            item
            for item in candidate.transfers
            if item.destination_area == PackageArea.AIRPORT and item.travel_date == intent.end_date
        ]
        for transfer in final_airport_transfers:
            _, arrive_by = _transfer_connection_limits(
                intent,
                candidate.flight,
                candidate.kind,
                transfer,
            )
            if arrive_by is None or transfer.has_feasible_departure(arrive_by=arrive_by):
                continue
            best_buffer = int(
                (candidate.flight.return_depart_at - transfer.earliest_arrival_at).total_seconds()
                // 60
            )
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.EARLY_DEPARTURE_BUFFER,
                    severity=PackageViolationSeverity.ERROR,
                    message="没有能在值机缓冲截止前到达机场的明确接驳班次或服务窗口",
                    component_ids=(transfer.id, candidate.flight.id),
                    details={
                        "best_available_buffer_minutes": best_buffer,
                        "required_minutes": intent.minimum_airport_buffer_minutes,
                    },
                )
            )
        return violations

    def _check_preferences(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        violations: list[PackageViolation] = []
        if intent.require_checked_baggage is True and (
            candidate.flight.checked_baggage_per_adult_kg is None
            or candidate.flight.checked_baggage_per_adult_kg <= 0
        ):
            baggage = candidate.flight.checked_baggage_per_adult_kg
            violations.append(
                PackageViolation(
                    code=PackageViolationCode.BAGGAGE_PREFERENCE,
                    severity=PackageViolationSeverity.ERROR,
                    message=(
                        "用户明确要求托运行李，但航班报价未明确托运行李额度"
                        if baggage is None
                        else "用户明确要求托运行李，但航班报价不含托运行李"
                    ),
                    component_ids=(candidate.flight.id,),
                )
            )
        if intent.allow_connections is False:
            outbound = candidate.flight.outbound_flight_numbers
            inbound = candidate.flight.return_flight_numbers
            if not outbound or not inbound:
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.CONNECTION_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message="用户明确拒绝中转，但报价未提供足以确认直飞的航班号证据",
                        component_ids=(candidate.flight.id,),
                        details={"flight_number_evidence_complete": False},
                    )
                )
            elif len(outbound) > 1 or len(inbound) > 1:
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.CONNECTION_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message="用户明确拒绝中转，但往返报价至少一程包含多个航段",
                        component_ids=(candidate.flight.id,),
                        details={
                            "outbound_segment_count": len(outbound),
                            "return_segment_count": len(inbound),
                        },
                    )
                )
        if intent.require_breakfast is True:
            missing = tuple(
                item.id for item in candidate.lodgings if item.breakfast_included is not True
            )
            if missing:
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.BREAKFAST_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message="用户明确要求早餐，但部分住宿未确认包含早餐",
                        component_ids=missing,
                    )
                )
        elif intent.require_breakfast is False:
            forbidden_or_unknown = tuple(
                item for item in candidate.lodgings if item.breakfast_included is not False
            )
            if forbidden_or_unknown:
                unknown_count = sum(
                    item.breakfast_included is None for item in forbidden_or_unknown
                )
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.BREAKFAST_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message=(
                            "用户明确禁止早餐，但部分住宿的早餐状态未知"
                            if unknown_count == len(forbidden_or_unknown)
                            else "用户明确禁止早餐，但部分住宿包含早餐或状态未知"
                        ),
                        component_ids=tuple(item.id for item in forbidden_or_unknown),
                        details={
                            "unknown_breakfast_quote_count": unknown_count,
                            "confirmed_breakfast_quote_count": sum(
                                item.breakfast_included is True for item in forbidden_or_unknown
                            ),
                        },
                    )
                )
        if intent.require_non_basic_lodging:
            basic_lodgings = tuple(
                (item, lodging_basic_markers(item))
                for item in candidate.lodgings
                if lodging_basic_markers(item)
            )
            if basic_lodgings:
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.LODGING_QUALITY_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message="用户明确要求住宿不能简陋，但候选包含无窗或基础型房间",
                        component_ids=tuple(item.id for item, _ in basic_lodgings),
                        details={
                            "basic_marker_count": sum(
                                len(markers) for _, markers in basic_lodgings
                            ),
                            "deterministic_hard_filter": True,
                        },
                    )
                )
        if intent.require_non_remote_lodging:
            unproven_lodgings = tuple(
                item
                for item in candidate.lodgings
                if (
                    item.location_convenience != LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
                    or not lodging_non_remote_evidence_confirmed(
                        item.location_address,
                        item.nearby_location_evidence,
                    )
                )
            )
            if unproven_lodgings:
                violations.append(
                    PackageViolation(
                        code=PackageViolationCode.LODGING_LOCATION_PREFERENCE,
                        severity=PackageViolationSeverity.ERROR,
                        message=(
                            "用户明确要求住宿不能偏僻，但部分住宿缺少来源明确地址及"
                            "邻近商业、服务或交通设施的页面证据"
                        ),
                        component_ids=tuple(item.id for item in unproven_lodgings),
                        details={
                            "unknown_location_quote_count": len(unproven_lodgings),
                            "place_name_alone_is_insufficient": True,
                        },
                    )
                )
        return violations

    def _check_budget(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
    ) -> list[PackageViolation]:
        known_same_currency_base_fare_cents = sum(
            contract.total_for_party_cents
            for contract in {
                transfer.price_contract_id: transfer
                for transfer in candidate.transfers
                if (
                    transfer.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
                    and transfer.currency == candidate.currency
                )
            }.values()
        )
        minimum_known_total_cents = (
            candidate.computed_total_cents + known_same_currency_base_fare_cents
        )
        if not candidate.flight.party_total_known:
            return []
        if intent.budget_cents is None or minimum_known_total_cents <= intent.budget_cents:
            return []
        return [
            PackageViolation(
                code=PackageViolationCode.BUDGET_EXCEEDED,
                severity=PackageViolationSeverity.ERROR,
                message="整包已确认小计与同币种公开基础价构成的最低已知总额超过用户预算",
                component_ids=candidate.component_ids,
                details={
                    "total_cents": candidate.computed_total_cents,
                    "known_same_currency_base_fare_cents": (known_same_currency_base_fare_cents),
                    "minimum_known_total_cents": minimum_known_total_cents,
                    "budget_cents": intent.budget_cents,
                },
            )
        ]


class PackageRepairer:
    _FRAGILITY_CODES = frozenset(
        {
            PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
            PackageViolationCode.EARLY_DEPARTURE_BUFFER,
        }
    )

    def __init__(
        self,
        planner: PackagePlanner | None = None,
        verifier: PackageVerifier | None = None,
    ) -> None:
        self._planner = planner or PackagePlanner()
        self._verifier = verifier or PackageVerifier()

    def repair_fragile_direct_island(
        self,
        intent: PackageIntent,
        rejected: TravelPackageCandidate,
        inventory: PackageInventory,
        *,
        now: datetime | None = None,
    ) -> PackageRepairOutcome:
        alternatives = [
            candidate
            for candidate in self._planner.generate(intent, inventory)
            if candidate.kind == PackageCandidateKind.SPLIT_AIRPORT_ISLAND
            and candidate.flight.id == rejected.flight.id
            and not self._verifier.errors(intent, candidate, now=now)
        ]
        if not alternatives:
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message="没有找到通过验证的机场岛首末晚分段住宿候选",
            )
        selected = self._planner.rank_candidates(
            intent,
            tuple(alternatives),
        )[0]
        repaired = selected.model_copy(
            update={
                "id": self._versioned_id(selected.id, rejected.version + 1),
                "version": rejected.version + 1,
                "parent_candidate_id": rejected.id,
            }
        )
        return PackageRepairOutcome(
            candidate=repaired,
            diff=diff_packages(rejected, repaired),
            message="已保留往返航班，改为机场岛首末晚加中段岛屿住宿",
        )

    def repair_with_valid_alternative(
        self,
        intent: PackageIntent,
        rejected: TravelPackageCandidate,
        inventory: PackageInventory,
        *,
        now: datetime | None = None,
    ) -> PackageRepairOutcome:
        alternatives = [
            candidate
            for candidate in self._planner.generate(intent, inventory)
            if candidate.component_ids != rejected.component_ids
            and not self._verifier.errors(intent, candidate, now=now)
        ]
        if not alternatives:
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message="其余候选中没有找到通过全部确定性约束的方案",
            )
        selected = self._planner.rank_candidates(
            intent,
            tuple(alternatives),
        )[0]
        repaired = selected.model_copy(
            update={
                "id": self._versioned_id(selected.id, rejected.version + 1),
                "version": rejected.version + 1,
                "parent_candidate_id": rejected.id,
            }
        )
        return PackageRepairOutcome(
            candidate=repaired,
            diff=diff_packages(rejected, repaired),
            message=("已在其余通过硬约束的候选中，按报价证据层级、用户软偏好与已确认小计完成重排"),
        )

    def repair_from_rejection(
        self,
        intent: PackageIntent,
        rejected: TravelPackageCandidate,
        candidates: tuple[TravelPackageCandidate, ...],
        violations: tuple[PackageViolation, ...],
    ) -> PackageRepairOutcome:
        """Produce a repair solely from the verifier's structured rejection.

        This path deliberately does not call ``PackageVerifier``.  The repair Agent
        may use the rejection codes and affected component ids to choose a new
        proposal, but a separate ReVerifier must decide whether that proposal is
        actually valid.
        """

        errors = tuple(
            violation
            for violation in violations
            if violation.severity == PackageViolationSeverity.ERROR
        )
        if not errors:
            return PackageRepairOutcome(
                candidate=rejected,
                diff=None,
                message="Verifier 未给出硬错误，Repair 保持初案并交由 ReVerifier 独立复核",
            )

        error_codes = frozenset(violation.code for violation in errors)
        alternatives = tuple(
            candidate
            for candidate in candidates
            if candidate.component_ids != rejected.component_ids
        )
        if error_codes <= self._FRAGILITY_CODES:
            alternatives = tuple(
                candidate
                for candidate in alternatives
                if candidate.kind
                in {
                    PackageCandidateKind.SPLIT_AIRPORT_ISLAND,
                    PackageCandidateKind.CONTINUOUS_AIRPORT_ISLAND,
                }
                and candidate.flight.id == rejected.flight.id
                and self._addresses_rejection(intent, candidate, errors)
            )
            repair_summary = (
                "按 Verifier 的接驳脆弱性拒绝原因，在所有预冻结机场岛候选中"
                "保留原航班并按确定性评分选择替代方案"
            )
        else:
            alternatives = tuple(
                candidate
                for candidate in alternatives
                if self._addresses_rejection(intent, candidate, errors)
            )
            repair_summary = (
                "按 Verifier 给出的硬约束组件与拒绝代码选择替代候选，"
                "未在 Repair 内部自行宣布验证通过"
            )
        if not alternatives:
            codes = "、".join(sorted(code.value for code in error_codes))
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message=f"没有候选能直接响应 Verifier 拒绝原因：{codes}",
            )

        selected = self._planner.rank_candidates(intent, alternatives)[0]
        repaired = selected.model_copy(
            update={
                "id": self._versioned_id(selected.id, rejected.version + 1),
                "version": rejected.version + 1,
                "parent_candidate_id": rejected.id,
            }
        )
        return PackageRepairOutcome(
            candidate=repaired,
            diff=diff_packages(rejected, repaired),
            message=repair_summary,
        )

    def repair_event(
        self,
        candidate: TravelPackageCandidate,
        event: PackageEvent,
        inventory: PackageInventory,
    ) -> PackageRepairOutcome:
        if event.id in candidate.applied_event_ids:
            return PackageRepairOutcome(
                candidate=candidate,
                diff=diff_packages(candidate, candidate),
                message="事件已在当前候选谱系中处理",
                repair_plan=self._event_repair_plan(
                    candidate,
                    event,
                    PackageRepairPlanStrategy.NO_ACTION,
                    "事件去重命中，不重复生成方案版本",
                ),
            )
        if event.target_component_id not in candidate.component_ids:
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message="事件目标不属于当前整包候选",
                repair_plan=self._event_repair_plan(
                    candidate,
                    event,
                    PackageRepairPlanStrategy.HUMAN_BLOCK,
                    "事件目标无法绑定当前候选，禁止猜测替换范围",
                ),
            )
        replacement = self._find_quote(event.replacement_component_id, inventory)
        if replacement is None or replacement.availability != QuoteAvailability.AVAILABLE:
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message="没有可用且有来源的替代报价",
                repair_plan=self._event_repair_plan(
                    candidate,
                    event,
                    PackageRepairPlanStrategy.EXPAND_CANDIDATE_POOL,
                    "当前局部候选池没有可用替代报价",
                    expand=True,
                ),
            )

        flight = candidate.flight
        lodgings = candidate.lodgings
        transfers = candidate.transfers
        if event.target_component_id == flight.id:
            if not isinstance(replacement, NormalizedFlightQuote) or not self._flight_compatible(
                flight, replacement
            ):
                return self._incompatible(candidate, event)
            flight = replacement
        elif event.target_component_id in {item.id for item in lodgings}:
            target = next(item for item in lodgings if item.id == event.target_component_id)
            if not isinstance(replacement, NormalizedLodgingQuote) or not self._lodging_compatible(
                target, replacement
            ):
                return self._incompatible(candidate, event)
            lodgings = tuple(
                replacement if item.id == event.target_component_id else item for item in lodgings
            )
        else:
            target_transfer = next(
                item for item in transfers if item.id == event.target_component_id
            )
            if not isinstance(replacement, TransferOption) or not self._transfer_compatible(
                target_transfer, replacement
            ):
                return self._incompatible(candidate, event)
            transfers = tuple(
                replacement if item.id == event.target_component_id else item for item in transfers
            )

        invalid_bindings = tuple(
            transfer.id
            for transfer in transfers
            if transfer_binding_error(transfer, lodgings) is not None
        )
        if invalid_bindings:
            return PackageRepairOutcome(
                candidate=None,
                diff=None,
                message=(
                    "局部替换会保留不再属于所选住宿的酒店专属接驳，必须重新查询并替换相关接驳"
                ),
                repair_plan=self._event_repair_plan(
                    candidate,
                    event,
                    PackageRepairPlanStrategy.EXPAND_CANDIDATE_POOL,
                    "住宿变化级联影响酒店绑定接驳，必须联合扩大相关候选池",
                    cascade=invalid_bindings,
                    expand=True,
                ),
            )

        flight_total = (
            flight.total_for_party_cents
            if flight.party_total_known and flight.total_for_party_cents is not None
            else 0
        )
        total = (
            flight_total
            + sum(
                item.total_for_party_cents
                for item in lodgings
                if item.currency == candidate.currency
            )
            + transfer_contract_total_cents(
                transfers,
                currency=candidate.currency,
            )
        )
        version = candidate.version + 1
        repaired = candidate.model_copy(
            update={
                "id": self._versioned_id(candidate.id, version),
                "version": version,
                "parent_candidate_id": candidate.id,
                "flight": flight,
                "lodgings": lodgings,
                "transfers": transfers,
                "declared_total_cents": total,
                "applied_event_ids": (*candidate.applied_event_ids, event.id),
            }
        )
        return PackageRepairOutcome(
            candidate=repaired,
            diff=diff_packages(candidate, repaired),
            message=(f"已因 {event.kind.value} 仅替换受影响组件 {event.target_component_id}"),
            repair_plan=self._event_repair_plan(
                candidate,
                event,
                PackageRepairPlanStrategy.LOCAL_REPAIR,
                "只替换事件目标，并把完整候选交给独立 ReVerifier",
            ),
        )

    def _find_quote(
        self,
        quote_id: str,
        inventory: PackageInventory,
    ) -> PackageQuote | None:
        for flight in inventory.flights:
            if flight.id == quote_id:
                return flight
        for lodging in inventory.lodgings:
            if lodging.id == quote_id:
                return lodging
        for transfer in inventory.transfers:
            if transfer.id == quote_id:
                return transfer
        return None

    def _addresses_rejection(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
        errors: tuple[PackageViolation, ...],
    ) -> bool:
        codes = {error.code for error in errors}
        replace_component_codes = codes - {
            PackageViolationCode.BUDGET_EXCEEDED,
            *self._FRAGILITY_CODES,
        }
        rejected_component_ids = {
            component_id
            for error in errors
            if error.code in replace_component_codes
            for component_id in error.component_ids
        }
        if rejected_component_ids.intersection(candidate.component_ids):
            return False
        if (
            PackageViolationCode.BUDGET_EXCEEDED in codes
            and intent.budget_cents is not None
            and candidate.computed_total_cents > intent.budget_cents
        ):
            return False
        if (
            PackageViolationCode.PARTY_AVAILABILITY_UNCONFIRMED in codes
            and not candidate.flight.party_availability_confirmed
        ):
            return False
        if PackageViolationCode.BAGGAGE_PREFERENCE in codes:
            baggage = candidate.flight.checked_baggage_per_adult_kg
            if intent.require_checked_baggage and (baggage is None or baggage <= 0):
                return False
        if PackageViolationCode.BREAKFAST_PREFERENCE in codes:
            if intent.require_breakfast is True and any(
                lodging.breakfast_included is not True for lodging in candidate.lodgings
            ):
                return False
            if intent.require_breakfast is False and any(
                lodging.breakfast_included is not False for lodging in candidate.lodgings
            ):
                return False
        return True

    def _flight_compatible(
        self,
        before: NormalizedFlightQuote,
        after: NormalizedFlightQuote,
    ) -> bool:
        return (
            before.origin == after.origin
            and before.destination == after.destination
            and before.adults == after.adults
            and before.outbound_depart_at.date() == after.outbound_depart_at.date()
            and before.return_depart_at.date() == after.return_depart_at.date()
        )

    def _lodging_compatible(
        self,
        before: NormalizedLodgingQuote,
        after: NormalizedLodgingQuote,
    ) -> bool:
        return (
            before.area == after.area
            and before.place_key == after.place_key
            and before.check_in == after.check_in
            and before.check_out == after.check_out
            and before.adults == after.adults
            and before.rooms == after.rooms
        )

    def _transfer_compatible(
        self,
        before: TransferOption,
        after: TransferOption,
    ) -> bool:
        return (
            before.origin_area == after.origin_area
            and before.destination_area == after.destination_area
            and before.origin_place_key == after.origin_place_key
            and before.destination_place_key == after.destination_place_key
            and before.travel_date == after.travel_date
            and before.adults == after.adults
            and before.price_guarantee == after.price_guarantee
        )

    def _incompatible(
        self,
        candidate: TravelPackageCandidate,
        event: PackageEvent,
    ) -> PackageRepairOutcome:
        return PackageRepairOutcome(
            candidate=None,
            diff=None,
            message="替代报价与被替换组件的日期、区域或人数口径不一致",
            repair_plan=self._event_repair_plan(
                candidate,
                event,
                PackageRepairPlanStrategy.EXPAND_CANDIDATE_POOL,
                "局部候选不兼容，扩大同日期同口径候选池后再尝试",
                expand=True,
            ),
        )

    def _event_repair_plan(
        self,
        candidate: TravelPackageCandidate,
        event: PackageEvent,
        strategy: PackageRepairPlanStrategy,
        rationale: str,
        *,
        cascade: tuple[str, ...] = (),
        expand: bool = False,
    ) -> PackageStructuredRepairPlan:
        preserve = tuple(
            component_id
            for component_id in candidate.component_ids
            if component_id != event.target_component_id and component_id not in cascade
        )
        steps: list[PackageRepairPlanStep] = []
        if preserve:
            steps.append(
                PackageRepairPlanStep(
                    order=len(steps) + 1,
                    action="preserve_unaffected_components",
                    component_ids=preserve,
                    success_invariant="未受影响组件保持逐值相等",
                )
            )
        if expand:
            steps.append(
                PackageRepairPlanStep(
                    order=len(steps) + 1,
                    action="expand_compatible_candidate_pool",
                    component_ids=(event.target_component_id,),
                    dependency_component_ids=cascade,
                    success_invariant="新候选有来源、可用、口径兼容且不破坏级联依赖",
                )
            )
        elif strategy == PackageRepairPlanStrategy.LOCAL_REPAIR:
            steps.append(
                PackageRepairPlanStep(
                    order=len(steps) + 1,
                    action="replace_target_component",
                    component_ids=(event.target_component_id,),
                    dependency_component_ids=cascade,
                    success_invariant="方案差异只包含目标组件及已声明级联组件",
                )
            )
        if strategy in {
            PackageRepairPlanStrategy.LOCAL_REPAIR,
            PackageRepairPlanStrategy.EXPAND_CANDIDATE_POOL,
            PackageRepairPlanStrategy.GLOBAL_REPLAN,
        }:
            steps.append(
                PackageRepairPlanStep(
                    order=len(steps) + 1,
                    action="independent_reverification",
                    component_ids=(event.target_component_id,),
                    dependency_component_ids=cascade,
                    success_invariant="异构确定性不变量与预算重算全部通过",
                )
            )
        return PackageStructuredRepairPlan(
            strategy=strategy,
            target_component_ids=(event.target_component_id,),
            cascade_component_ids=cascade,
            preserve_component_ids=preserve,
            candidate_pool_expansion_required=expand,
            requested_candidate_count=5 if expand else 0,
            steps=tuple(steps),
            fallback_strategy=(
                PackageRepairPlanStrategy.GLOBAL_REPLAN
                if expand
                else PackageRepairPlanStrategy.HUMAN_BLOCK
                if strategy == PackageRepairPlanStrategy.GLOBAL_REPLAN
                else None
            ),
            rationale=rationale,
        )

    def _versioned_id(self, candidate_id: str, version: int) -> str:
        base = candidate_id.rsplit(":v", maxsplit=1)[0]
        return f"{base}:v{version}"


class PackageOrchestrator:
    _FRAGILITY_CODES = frozenset(
        {
            PackageViolationCode.LATE_ARRIVAL_BOAT_RISK,
            PackageViolationCode.EARLY_DEPARTURE_BUFFER,
        }
    )

    def __init__(
        self,
        verifier: PackageVerifier | None = None,
        repairer: PackageRepairer | None = None,
        planner: PackagePlanner | None = None,
    ) -> None:
        self._planner = planner or PackagePlanner()
        self._verifier = verifier or PackageVerifier()
        self._repairer = repairer or PackageRepairer(
            planner=self._planner,
            verifier=self._verifier,
        )

    def decide_from_handoff(
        self,
        intent: PackageIntent,
        handoff: PackagePlanningHandoff,
    ) -> PackageRunResult:
        """Issue the master decision without rerunning any upstream stage."""

        initial = handoff.planner.selected_candidate
        assert initial is not None
        initial_violations = handoff.initial_verification.violations
        initial_errors = handoff.initial_verification.errors
        repaired = handoff.repair.outcome
        final = repaired.candidate or initial
        final_verification = handoff.reverification
        final_violations = (
            final_verification.violations if final_verification is not None else initial_violations
        )
        final_errors = self._errors(final_violations)
        decisions: list[PackageDecision] = []

        if initial_errors:
            fragility_only = (
                frozenset(item.code for item in initial_errors) <= self._FRAGILITY_CODES
            )
            decisions.append(
                self._decision(
                    PackageDecisionState.REJECT_AND_REPLAN,
                    (
                        "主控采纳 Verifier 拒绝：晚到赶船或早班离岛缓冲不足"
                        if fragility_only
                        else "主控采纳 Verifier 对用户硬约束或报价证据的拒绝"
                    ),
                    initial_errors,
                    initial.evidence_refs,
                )
            )
            if repaired.candidate is None:
                decisions.append(
                    self._decision(
                        PackageDecisionState.HUMAN_BLOCK,
                        repaired.message,
                        initial_errors,
                        initial.evidence_refs,
                    )
                )
                return self._result(
                    initial,
                    initial,
                    tuple(decisions),
                    initial_violations,
                    initial_violations,
                    None,
                    intent=intent,
                    candidate_pool=handoff.planner.candidates,
                    planning_handoff=handoff,
                )

        if final_verification is None:
            raise ValueError("master decision requires a ReVerifier handoff")
        if final_errors:
            decisions.append(
                self._decision(
                    PackageDecisionState.HUMAN_BLOCK,
                    (
                        "Repair 候选经 ReVerifier 复核后仍有硬约束冲突"
                        if initial_errors
                        else "初验通过但 ReVerifier 发现硬约束冲突，主控拒绝接受"
                    ),
                    final_errors,
                    final.evidence_refs,
                )
            )
        else:
            warnings = self._warnings(final_violations)
            decisions.append(
                self._decision(
                    PackageDecisionState.ACCEPT,
                    (
                        f"{repaired.message}；ReVerifier 确认硬约束通过，但公开基础价"
                        "的税费、换汇和总预算合规仍未完全验证"
                        if warnings
                        else f"{repaired.message}；ReVerifier 确认通过"
                    ),
                    warnings,
                    final.evidence_refs,
                )
            )
        return self._result(
            initial,
            final,
            tuple(decisions),
            initial_violations,
            final_violations,
            repaired.diff,
            intent=intent,
            candidate_pool=handoff.planner.candidates,
            planning_handoff=handoff,
        )

    def decide_event_from_handoff(
        self,
        intent: PackageIntent,
        current: TravelPackageCandidate,
        handoff: PackageEventPlanningHandoff,
    ) -> PackageRunResult:
        """Decide an event repair from the Repair and ReVerifier receipts."""

        repair = handoff.repair
        if (
            repair.current_candidate_id != current.id
            or repair.current_candidate_version != current.version
            or repair.current_component_ids != current.component_ids
        ):
            raise ValueError("event handoff does not match the current package")
        decisions = [
            self._decision(
                PackageDecisionState.REJECT_AND_REPLAN,
                (
                    f"收到 {repair.event.kind.value} 事件，主控只允许替换"
                    f"受影响组件 {repair.event.target_component_id}"
                ),
                (),
                current.evidence_refs,
            )
        ]
        outcome = repair.outcome
        if outcome.candidate is None:
            decisions.append(
                self._decision(
                    PackageDecisionState.HUMAN_BLOCK,
                    outcome.message,
                    (),
                    current.evidence_refs,
                )
            )
            return self._result(
                current,
                current,
                tuple(decisions),
                (),
                (),
                None,
                intent=intent,
                candidate_pool=(current,),
                event_handoff=handoff,
            )
        final = outcome.candidate
        assert handoff.reverification is not None
        final_violations = handoff.reverification.violations
        final_errors = handoff.reverification.errors
        if final_errors:
            decisions.append(
                self._decision(
                    PackageDecisionState.HUMAN_BLOCK,
                    "事件局部替换经独立 ReVerifier 复核后仍有硬约束冲突",
                    final_errors,
                    final.evidence_refs,
                )
            )
        else:
            warnings = self._warnings(final_violations)
            decisions.append(
                self._decision(
                    PackageDecisionState.ACCEPT,
                    (
                        "受影响组件已局部替换且 ReVerifier 通过；公开基础价的税费、"
                        "换汇和总预算合规仍未完全验证"
                        if warnings
                        else "受影响组件已局部替换，未受影响组件保持不变，ReVerifier 通过"
                    ),
                    warnings,
                    final.evidence_refs,
                )
            )
        return self._result(
            current,
            final,
            tuple(decisions),
            (),
            final_violations,
            outcome.diff,
            intent=intent,
            candidate_pool=(current, final),
            event_handoff=handoff,
        )

    def execute(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
        inventory: PackageInventory,
        *,
        now: datetime | None = None,
    ) -> PackageRunResult:
        initial_violations = self._verifier.verify(intent, candidate, now=now)
        initial_errors = self._errors(initial_violations)
        if not initial_errors:
            warnings = self._warnings(initial_violations)
            accepted = self._decision(
                PackageDecisionState.ACCEPT,
                (
                    "整包硬约束通过；公开基础价的税费、换汇和总预算合规仍未完全验证"
                    if warnings
                    else "整包候选通过确定性验证"
                ),
                warnings,
                candidate.evidence_refs,
            )
            return self._result(
                candidate,
                candidate,
                (accepted,),
                initial_violations,
                initial_violations,
                None,
                intent=intent,
                inventory=inventory,
            )

        initial_codes = frozenset(item.code for item in initial_errors)
        fragility_only = initial_codes <= self._FRAGILITY_CODES
        reject = self._decision(
            PackageDecisionState.REJECT_AND_REPLAN,
            (
                "主控拒绝晚到赶船或早班离岛缓冲不足的直接上岛方案"
                if fragility_only
                else "主控拒绝违反用户硬约束或报价证据约束的最低价候选"
            ),
            initial_errors,
            candidate.evidence_refs,
        )
        if fragility_only:
            repaired = self._repairer.repair_fragile_direct_island(
                intent,
                candidate,
                inventory,
                now=now,
            )
            if repaired.candidate is None:
                repaired = self._repairer.repair_with_valid_alternative(
                    intent,
                    candidate,
                    inventory,
                    now=now,
                )
        else:
            repaired = self._repairer.repair_with_valid_alternative(
                intent,
                candidate,
                inventory,
                now=now,
            )
        if repaired.candidate is None:
            blocked = self._decision(
                PackageDecisionState.HUMAN_BLOCK,
                repaired.message,
                initial_errors,
                candidate.evidence_refs,
            )
            return self._result(
                candidate,
                candidate,
                (reject, blocked),
                initial_violations,
                initial_violations,
                None,
                intent=intent,
                inventory=inventory,
            )

        final_candidate = repaired.candidate
        final_violations = self._verifier.verify(intent, final_candidate, now=now)
        final_errors = self._errors(final_violations)
        if final_errors:
            blocked = self._decision(
                PackageDecisionState.HUMAN_BLOCK,
                "分段住宿修复后仍有硬约束冲突",
                final_errors,
                final_candidate.evidence_refs,
            )
            return self._result(
                candidate,
                final_candidate,
                (reject, blocked),
                initial_violations,
                final_violations,
                repaired.diff,
                intent=intent,
                inventory=inventory,
            )
        accepted = self._decision(
            PackageDecisionState.ACCEPT,
            (
                f"{repaired.message}；硬约束通过，但公开基础价的税费、换汇和总预算合规仍未完全验证"
                if self._warnings(final_violations)
                else f"{repaired.message}；通过最终验证"
            ),
            self._warnings(final_violations),
            final_candidate.evidence_refs,
        )
        return self._result(
            candidate,
            final_candidate,
            (reject, accepted),
            initial_violations,
            final_violations,
            repaired.diff,
            intent=intent,
            inventory=inventory,
        )

    def replan_after_event(
        self,
        intent: PackageIntent,
        candidate: TravelPackageCandidate,
        event: PackageEvent,
        inventory: PackageInventory,
        *,
        now: datetime | None = None,
    ) -> PackageRunResult:
        reject = self._decision(
            PackageDecisionState.REJECT_AND_REPLAN,
            f"收到 {event.kind.value} 事件，只重算受影响组件",
            (),
            candidate.evidence_refs,
        )
        repaired = self._repairer.repair_event(candidate, event, inventory)
        if repaired.candidate is None:
            blocked = self._decision(
                PackageDecisionState.HUMAN_BLOCK,
                repaired.message,
                (),
                candidate.evidence_refs,
            )
            return self._result(
                candidate,
                candidate,
                (reject, blocked),
                (),
                (),
                None,
                intent=intent,
                inventory=inventory,
            )
        final_candidate = repaired.candidate
        final_violations = self._verifier.verify(intent, final_candidate, now=now)
        final_errors = self._errors(final_violations)
        if final_errors:
            blocked = self._decision(
                PackageDecisionState.HUMAN_BLOCK,
                "局部替换后仍未通过确定性验证",
                final_errors,
                final_candidate.evidence_refs,
            )
            return self._result(
                candidate,
                final_candidate,
                (reject, blocked),
                (),
                final_violations,
                repaired.diff,
                intent=intent,
                inventory=inventory,
            )
        accepted = self._decision(
            PackageDecisionState.ACCEPT,
            (
                "受影响组件已局部替换，未受影响组件保持不变；公开基础价的税费、"
                "换汇和总预算合规仍未完全验证"
                if self._warnings(final_violations)
                else "受影响组件已局部替换，未受影响组件保持不变"
            ),
            self._warnings(final_violations),
            final_candidate.evidence_refs,
        )
        return self._result(
            candidate,
            final_candidate,
            (reject, accepted),
            (),
            final_violations,
            repaired.diff,
            intent=intent,
            inventory=inventory,
        )

    def _decision(
        self,
        state: PackageDecisionState,
        summary: str,
        violations: tuple[PackageViolation, ...],
        evidence_refs: tuple[str, ...],
    ) -> PackageDecision:
        return PackageDecision(
            state=state,
            summary=summary,
            violation_codes=tuple(item.code for item in violations),
            evidence_refs=evidence_refs,
        )

    def _errors(
        self,
        violations: tuple[PackageViolation, ...],
    ) -> tuple[PackageViolation, ...]:
        return tuple(item for item in violations if item.severity == PackageViolationSeverity.ERROR)

    def _warnings(
        self,
        violations: tuple[PackageViolation, ...],
    ) -> tuple[PackageViolation, ...]:
        return tuple(
            item for item in violations if item.severity == PackageViolationSeverity.WARNING
        )

    def _result(
        self,
        initial: TravelPackageCandidate,
        final: TravelPackageCandidate,
        decisions: tuple[PackageDecision, ...],
        initial_violations: tuple[PackageViolation, ...],
        final_violations: tuple[PackageViolation, ...],
        diff: PackageDiff | None,
        *,
        intent: PackageIntent,
        inventory: PackageInventory | None = None,
        candidate_pool: tuple[TravelPackageCandidate, ...] | None = None,
        planning_handoff: PackagePlanningHandoff | None = None,
        event_handoff: PackageEventPlanningHandoff | None = None,
    ) -> PackageRunResult:
        preservation = diff.preservation_ratio if diff is not None else Decimal(1)
        if candidate_pool is None:
            if inventory is None:
                raise ValueError("package result requires inventory or a planner handoff")
            candidates = self._planner.generate(intent, inventory)
        else:
            candidates = candidate_pool
        return PackageRunResult(
            initial_candidate=initial,
            final_candidate=final,
            decisions=decisions,
            final_decision=decisions[-1],
            initial_violations=initial_violations,
            final_violations=final_violations,
            diff=diff,
            preservation_ratio=preservation,
            budget=package_budget(final),
            evidence_refs=final.evidence_refs,
            preference_applications=(
                breakfast_preference_application(
                    intent,
                    candidates,
                    final,
                ),
            ),
            planning_handoff=planning_handoff,
            event_handoff=event_handoff,
        )
