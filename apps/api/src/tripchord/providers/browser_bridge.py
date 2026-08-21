from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Protocol, cast
from urllib.parse import ParseResult, parse_qsl, urlencode, urlparse
from urllib.parse import quote as url_quote
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import Field, JsonValue, ValidationInfo, field_validator, model_validator

from tripchord.domain.common import DomainModel
from tripchord.formal_live_source import FormalLiveSourceAuthority

logger = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value

BRIDGE_TOKEN_HEADER = "X-TripChord-Bridge-Token"
CONTROL_TOKEN_HEADER = "X-TripChord-Control-Token"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
COMPANION_HEARTBEAT_STALE_AFTER_SECONDS = 45
RECENT_EXACT_QUOTE_REUSE_SECONDS = 600
RANGE_RECEIPT_MAX_CLOCK_SKEW_SECONDS = 30
PRODUCTION_VISIBLE_DOM_PARSER_VERSION = "tripchord-visible-dom-v3"
SOURCE_EXECUTION_ATTESTATION_SCHEMA = (
    "tripchord-browser-source-execution-attestation-v1"
)
SOURCE_EXECUTION_RECEIPT_SCHEMA = "tripchord-browser-source-execution-receipt-v1"
PRODUCTION_SOURCE_EXECUTION_ENVIRONMENT = "chrome_extension_service_worker"
UNTRUSTED_SOURCE_EXECUTION_ENVIRONMENT = "untrusted_external_executor"
_FORMAL_WORKER_SOURCE_TOKEN_CONTEXT = (
    b"tripchord-formal-worker-parent-source-v1"
)
_FORMAL_ACTIVATION_FAILPOINT_ACK_FLUSH_SECONDS = 0.05
DEFAULT_TERMINAL_RECORD_RETENTION_SECONDS = 3600
DEFAULT_MAX_TERMINAL_RECORDS = 256
# Flexible-window acquisition watchers are started immediately after submit.
# Keep their source record until that watcher consumes the terminal snapshot so
# a large burst cannot prune a result between ``complete`` and ``wait_many``.
_LEDGER_TERMINAL_RETENTION_OPTION = "__tripchord_ledger_terminal_retention"
DEFAULT_MAX_COMPANION_CONTROL_RECORDS = 64
COMPANION_CONTROL_PROTOCOL_VERSION = "tripchord-companion-control-v1"
BROWSER_BRIDGE_STATE_PATH_ENV = "TRIPCHORD_BROWSER_BRIDGE_STATE_PATH"
QUNAR_DETAIL_SEED_SELECTION_POLICY = "query-fingerprint-rotation-v1"
QUNAR_CURRENT_DETAIL_FALLBACK_SUMMARY_VERSION = (
    "tripchord-qunar-detail-fallback-summary-v2"
)
QUNAR_AUDITED_LODGING_DETAIL_PROPERTY_IDS = (
    "2112",
    "2055",
    "2071",
    "2072",
    "2075",
    "2142",
)
_ALLOW_HISTORICAL_QUNAR_FALLBACK_V1_CONTEXT_KEY = (
    "allow_historical_qunar_detail_fallback_v1"
)
_PROVIDER_DOMAINS = {
    "ctrip": ("ctrip.com",),
    "fliggy": ("fliggy.com", "fliggy.hk"),
    "qunar": ("qunar.com",),
    "tongcheng": ("ly.com", "elong.com"),
}
_FORBIDDEN_QUERY_KEYS = {
    "account",
    "book",
    "checkout",
    "cookie",
    "coupon",
    "credential",
    "localstorage",
    "order",
    "password",
    "pay",
    "payment",
    "sessionstorage",
}
_FORBIDDEN_URL_MARKERS = {
    "cashier",
    "checkout",
    "coupon",
    "order",
    "payment",
}
_FLIGHT_WORKFLOW_BY_PROVIDER = {
    "ctrip": "staged_outbound_return",
    "fliggy": "staged_outbound_return",
    "qunar": "combined_roundtrip_card",
    "tongcheng": "staged_outbound_return",
}
_FLIGHT_PARTY_STATUSES_BY_PROVIDER = {
    "ctrip": frozenset({"confirmed_for_party", "observed_party_context"}),
    "fliggy": frozenset({"comparison_only"}),
    "qunar": frozenset({"confirmed_for_party", "observed_party_context"}),
    "tongcheng": frozenset({"confirmed_for_party", "observed_party_context"}),
}
_ALLOWED_READ_ONLY_FLIGHT_ACTIONS = frozenset(
    {
        "search",
        "filter",
        "select_outbound",
        "reselect_outbound",
        "provider_auto_selected_outbound",
        "select_return",
    }
)
_FORBIDDEN_ACTION_TRACE_MARKERS = (
    "account",
    "book",
    "booking",
    "cashier",
    "checkout",
    "coupon",
    "order",
    "pay",
    "payment",
    "下单",
    "优惠券",
    "使用优惠券",
    "修改账号",
    "改账号",
    "支付",
    "预订",
)


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def formal_worker_source_token(bridge_token: str) -> str:
    """Derive a one-way worker credential distinct from the Companion token.

    A formal worker may submit/poll/cancel tasks on the parent queue, but it
    must never possess the credential accepted by Companion heartbeat, claim,
    or completion routes.  The derived value is domain-separated and cannot be
    used to recover or authenticate as the original bridge token.
    """

    if len(bridge_token) < 32:
        raise ValueError("bridge_token must contain at least 32 characters")
    return hmac.new(
        bridge_token.encode("utf-8"),
        _FORMAL_WORKER_SOURCE_TOKEN_CONTEXT,
        hashlib.sha256,
    ).hexdigest()


class BrowserProvider(StrEnum):
    CTRIP = "ctrip"
    FLIGGY = "fliggy"
    QUNAR = "qunar"
    TONGCHENG = "tongcheng"


LIVE_V4_BROWSER_PROVIDERS: tuple[BrowserProvider, ...] = (
    BrowserProvider.CTRIP,
    BrowserProvider.FLIGGY,
    BrowserProvider.QUNAR,
)
LIVE_V5_BROWSER_PROVIDERS: tuple[BrowserProvider, ...] = (
    BrowserProvider.CTRIP,
    BrowserProvider.QUNAR,
    BrowserProvider.TONGCHENG,
)


class BrowserVertical(StrEnum):
    FLIGHT = "flight"
    LODGING = "lodging"


class QuotePriceBasis(StrEnum):
    PER_PERSON = "per_person"
    PER_NIGHT = "per_night"
    TOTAL_PARTY = "total_party"
    TOTAL_STAY = "total_stay"
    UNKNOWN = "unknown"


class BrowserTaskState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            BrowserTaskState.SUCCEEDED,
            BrowserTaskState.BLOCKED,
            BrowserTaskState.FAILED,
            BrowserTaskState.CANCELLED,
        }


class BrowserCompanionControlKind(StrEnum):
    RELOAD_EXTENSION = "reload_extension"


class BrowserCompanionControlState(StrEnum):
    QUEUED = "queued"
    DRAINING = "draining"
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            BrowserCompanionControlState.APPLIED,
            BrowserCompanionControlState.FAILED,
            BrowserCompanionControlState.EXPIRED,
        }


class BrowserCompanionReloadReceiptState(StrEnum):
    ACCEPTED = "accepted"
    APPLIED = "applied"
    FAILED = "failed"


class BrowserCompanionReloadReasonCode(StrEnum):
    COMPANION_BUILD_CHANGED = "companion_build_changed"
    OPERATOR_REQUESTED = "operator_requested"
    RECOVERY = "recovery"


class BrowserFailureCode(StrEnum):
    CAPTCHA_REQUIRED = "captcha_required"
    LOGIN_REQUIRED = "login_required"
    DOM_DRIFT = "dom_drift"
    NAVIGATION_ERROR = "navigation_error"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    UNSUPPORTED_QUERY = "unsupported_query"
    EXTRACTION_ERROR = "extraction_error"
    NO_INVENTORY = "no_inventory"
    CANCELLED = "cancelled"


class LodgingInventoryReceiptState(StrEnum):
    CONFIRMED_EMPTY = "confirmed_empty"
    BOUNDED_NO_EXACT_QUOTE = "bounded_no_exact_quote"
    BOUNDED_PROVIDER_PENDING = "bounded_provider_pending"


class LodgingInventoryConfirmationScope(StrEnum):
    CONFIRMED_VISIBLE_SEARCH = "confirmed_visible_search"


class LodgingInventoryPriceBasis(StrEnum):
    PER_NIGHT = "per_night"
    TOTAL_STAY = "total_stay"
    UNKNOWN = "unknown"


class LodgingInventoryPriceFinality(StrEnum):
    EXACT_CANDIDATE = "exact_candidate"
    STARTING_OR_ESTIMATED = "starting_or_estimated"
    UNKNOWN = "unknown"


class FlightSearchReceiptState(StrEnum):
    COMPARISON_PRICE_ONLY = "comparison_price_only"
    BOUNDED_NO_EXACT_QUOTE = "bounded_no_exact_quote"


class FlightSearchConfirmationScope(StrEnum):
    CONFIRMED_VISIBLE_SEARCH = "confirmed_visible_search"


class FlightCandidatePriceClassification(StrEnum):
    COMPARISON_ONLY = "comparison_only"
    STARTING_OR_ESTIMATED = "starting_or_estimated"
    NO_VISIBLE_PRICE = "no_visible_price"


def _require_timezone(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value


def _require_optional_timezone(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _require_timezone(value)


def _is_allowed_provider_url(provider: BrowserProvider, url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path_segments = {
        segment.lower()
        for segment in parsed.path.replace("-", "/").replace("_", "/").split("/")
        if segment
    }
    query_keys = {pair.partition("=")[0].lower() for pair in parsed.query.split("&") if pair}
    return (
        parsed.scheme == "https"
        and any(
            host == domain or host.endswith(f".{domain}")
            for domain in _PROVIDER_DOMAINS[provider.value]
        )
        and parsed.username is None
        and parsed.password is None
        and not any(
            marker in segment
            and not _is_audited_read_only_search_path(provider, parsed, segment)
            for segment in path_segments
            for marker in _FORBIDDEN_URL_MARKERS
        )
        and not any(
            key in _FORBIDDEN_URL_MARKERS
            and not _is_lodging_search_checkout_date(provider, parsed, key)
            for key in query_keys
        )
    )


def _is_audited_read_only_search_path(
    provider: BrowserProvider,
    parsed: ParseResult,
    segment: str,
) -> bool:
    return (
        provider == BrowserProvider.TONGCHENG
        and (parsed.hostname or "").lower() == "www.ly.com"
        and parsed.path.lower() == "/eliflight/book1.html"
        and segment == "book1.html"
    )


def _is_lodging_search_checkout_date(
    provider: BrowserProvider,
    parsed: ParseResult,
    query_key: str,
) -> bool:
    if query_key != "checkout":
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    is_ctrip_search = (
        provider == BrowserProvider.CTRIP
        and host == "hotels.ctrip.com"
        and path in {"/hotels/list", "/hotels/detail"}
    )
    is_fliggy_search = (
        provider == BrowserProvider.FLIGGY
        and host == "hotel.fliggy.com"
        and path in {"/hotel_list3.htm", "/hotel_detail2.htm"}
    )
    if not is_ctrip_search and not is_fliggy_search:
        return False
    pairs = parse_qsl(
        parsed.query,
        keep_blank_values=True,
    )
    checkins = [value for key, value in pairs if key.lower() == "checkin"]
    checkouts = [value for key, value in pairs if key.lower() == "checkout"]
    if len(checkins) != 1 or len(checkouts) != 1:
        return False
    try:
        checkin = date.fromisoformat(checkins[0])
        checkout = date.fromisoformat(checkouts[0])
    except ValueError:
        return False
    return checkout > checkin


class BrowserSearchQuery(DomainModel):
    origin: str | None = None
    destination: str
    origin_code: str | None = None
    destination_code: str | None = None
    start_date: date
    end_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=9)
    party_shape_supported: bool = True
    party_shape_failure: str | None = None
    rooms: int = Field(default=1, ge=1, le=8)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    search_url: str | None = None
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_party_shape_contract(self) -> BrowserSearchQuery:
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children ages must be between 0 and 17")
        if self.children > 0 and len(self.children_ages) != self.children:
            raise ValueError("children_ages must contain exactly one age for every child")
        if self.children == 0 and self.children_ages:
            raise ValueError("children_ages require children")
        if (self.children or self.infants) and self.party_shape_supported:
            raise ValueError(
                "mixed child/infant parties require an explicit provider party-shape contract"
            )
        if not self.party_shape_supported and not self.party_shape_failure:
            raise ValueError("unsupported party shape requires an explicit failure")
        return self

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("origin_code", "destination_code")
    @classmethod
    def normalize_iata_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
            raise ValueError("location codes must be three-letter IATA codes")
        return normalized

    @field_validator("search_url")
    @classmethod
    def validate_search_url_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("search_url must be an HTTPS URL without embedded credentials")
        return value

    @model_validator(mode="after")
    def validate_dates(self) -> BrowserSearchQuery:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        normalized_keys = {key.lower().replace("-", "").replace("_", "") for key in self.options}
        if any(forbidden in key for key in normalized_keys for forbidden in _FORBIDDEN_QUERY_KEYS):
            raise ValueError(
                "query options cannot contain account, transaction, or browser secrets"
            )
        return self


class BrowserRangeCapabilityStatus(StrEnum):
    """What the browser actually proved about a provider's date-range UI."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class BrowserRangePriceFinality(StrEnum):
    EXACT = "exact"
    STARTING = "starting"
    UNKNOWN = "unknown"


class BrowserRangePriceBasis(StrEnum):
    TOTAL_FOR_PARTY = "total_for_party"
    PER_PERSON = "per_person"
    UNKNOWN = "unknown"


class BrowserRangeEvidenceType(StrEnum):
    VISIBLE_DOM = "visible_dom"
    PROVIDER_NETWORK = "provider_network"
    NONE = "none"


class BrowserRangeParty(DomainModel):
    adults: int = Field(ge=1, le=9)
    children: int = Field(default=0, ge=0, le=9)
    children_ages: tuple[int, ...] = ()
    infants: int = Field(default=0, ge=0, le=9)
    rooms: int = Field(default=1, ge=1, le=8)

    @model_validator(mode="after")
    def validate_children(self) -> BrowserRangeParty:
        if len(self.children_ages) != self.children:
            raise ValueError("range party children_ages must match children")
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("range party children ages must be between 0 and 17")
        return self


class BrowserDateRangeQuery(DomainModel):
    """Immutable input to a provider's batch-date capability probe.

    ``requested_pairs`` is deliberately explicit: a range response is never
    allowed to silently widen or reinterpret the dates the caller requested.
    """

    provider: BrowserProvider
    kind: BrowserVertical
    origin: str | None = None
    destination: str
    origin_code: str | None = None
    destination_code: str | None = None
    requested_pairs: tuple[tuple[date, date], ...]
    party: BrowserRangeParty
    currency: str = Field(min_length=3, max_length=3)
    tenant_partition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_version: str = Field(min_length=1, max_length=64)
    parser_version: str = Field(min_length=1, max_length=64)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def validate_pairs(self) -> BrowserDateRangeQuery:
        if not self.requested_pairs:
            raise ValueError("range probe requires at least one requested date pair")
        if len(set(self.requested_pairs)) != len(self.requested_pairs):
            raise ValueError("range probe requested_pairs must be unique")
        if any(checkout <= checkin for checkin, checkout in self.requested_pairs):
            raise ValueError("range probe checkout must be after checkin")
        if self.kind == BrowserVertical.FLIGHT and not self.origin:
            raise ValueError("range flight probes require an origin")
        if self.kind == BrowserVertical.FLIGHT:
            for code in (self.origin_code, self.destination_code):
                if (
                    code is None
                    or len(code) != 3
                    or not code.isascii()
                    or not code.isalpha()
                ):
                    raise ValueError(
                        "range flight probes require audited IATA origin and destination"
                    )
        return self

    @property
    def fingerprint_sha256(self) -> str:
        return browser_range_query_fingerprint_sha256(self)


class BrowserRangeCell(DomainModel):
    """One visible calendar cell; starting prices are never exact quotes."""

    start_date: date
    end_date: date
    party: BrowserRangeParty
    currency: str = Field(min_length=3, max_length=3)
    amount: Decimal | None = Field(default=None, ge=0)
    price_basis: BrowserRangePriceBasis = BrowserRangePriceBasis.UNKNOWN
    party_total_known: bool = False
    taxes_and_fees_included: bool | None = None
    product_identity: str | None = Field(default=None, min_length=1, max_length=240)
    quote: BrowserQuote | None = None
    price_finality: BrowserRangePriceFinality = BrowserRangePriceFinality.UNKNOWN
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_lodging_dates(cls, value: object) -> object:
        if isinstance(value, dict):
            value = dict(value)
            if "start_date" not in value and "checkin" in value:
                value["start_date"] = value.pop("checkin")
            if "end_date" not in value and "checkout" in value:
                value["end_date"] = value.pop("checkout")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @model_validator(mode="after")
    def validate_cell(self) -> BrowserRangeCell:
        if self.end_date <= self.start_date:
            raise ValueError("range cell checkout must be after checkin")
        if self.price_finality == BrowserRangePriceFinality.EXACT and self.amount is None:
            raise ValueError("exact range cell requires an amount")
        if self.price_finality == BrowserRangePriceFinality.STARTING and self.amount is None:
            raise ValueError("starting range cell requires an amount")
        if self.price_finality == BrowserRangePriceFinality.EXACT and (
            self.price_basis != BrowserRangePriceBasis.TOTAL_FOR_PARTY
            or not self.party_total_known
            or self.taxes_and_fees_included is not True
            or not self.product_identity
        ):
            raise ValueError(
                "exact range cell requires comparable party total, included taxes, "
                "and product identity"
            )
        return self


class RangeCapabilityEvidence(DomainModel):
    status: BrowserRangeCapabilityStatus
    provider: BrowserProvider
    contract_version: str = Field(min_length=1, max_length=64)
    parser_version: str = Field(min_length=1, max_length=64)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: datetime
    query_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_id: str | None = None
    lease_id: str | None = None
    evidence_type: BrowserRangeEvidenceType = BrowserRangeEvidenceType.NONE
    source_url: str | None = None
    response_shape_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @model_validator(mode="after")
    def validate_evidence_boundary(self) -> RangeCapabilityEvidence:
        if self.evidence_type == BrowserRangeEvidenceType.NONE:
            if self.source_url is not None or self.response_shape_sha256 is not None:
                raise ValueError("none range evidence cannot claim a source or response shape")
        elif self.response_shape_sha256 is None:
            raise ValueError("range evidence requires response_shape_sha256")
        if (self.task_id is None) != (self.lease_id is None):
            raise ValueError("range evidence task_id and lease_id must be paired")
        if self.task_id is not None and (
            not self.task_id.strip() or not self.lease_id or not self.lease_id.strip()
        ):
            raise ValueError("range evidence task_id and lease_id must be non-blank")
        return self


class BrowserRangeCompletion(DomainModel):
    """Receipt for a range probe, including its exact coverage boundary."""

    schema_version: str = Field(pattern=r"^tripchord-browser-range-receipt-v1$")
    query: BrowserDateRangeQuery
    capability: RangeCapabilityEvidence
    cells: tuple[BrowserRangeCell, ...] = ()
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_receipt(self) -> BrowserRangeCompletion:
        if self.capability.provider != self.query.provider:
            raise ValueError("range capability provider does not match query")
        if self.capability.contract_version != self.query.contract_version:
            raise ValueError("range capability contract version does not match query")
        if self.capability.parser_version != self.query.parser_version:
            raise ValueError("range capability parser version does not match query")
        if self.capability.query_fingerprint_sha256 != self.query.fingerprint_sha256:
            raise ValueError("range capability query fingerprint does not match query")
        expected_pairs = set(self.query.requested_pairs)
        cell_pairs = {(cell.start_date, cell.end_date) for cell in self.cells}
        if len(cell_pairs) != len(self.cells):
            raise ValueError("range receipt cells must contain unique date pairs")
        if not cell_pairs <= expected_pairs:
            raise ValueError("range receipt contains a date pair not requested")
        for cell in self.cells:
            if cell.party != self.query.party:
                raise ValueError("range cell party does not match requested party")
            if cell.currency != self.query.currency:
                raise ValueError("range cell currency does not match requested currency")
            if cell.price_finality == BrowserRangePriceFinality.EXACT:
                error = exact_cell_binding_error(
                    self.query,
                    self.capability,
                    cell,
                    self.expires_at,
                    datetime.now(UTC),
                )
                if error is not None:
                    raise ValueError(error)
        if self.capability.status == BrowserRangeCapabilityStatus.CONFIRMED and any(
            cell.price_finality == BrowserRangePriceFinality.EXACT
            and (
                cell.quote is not None
                and (
                    cell.quote.provider != self.query.provider
                    or cell.quote.kind != self.query.kind
                )
            )
            for cell in self.cells
        ):
            raise ValueError("range cell quote provider or kind does not match query")
        if self.receipt_sha256 != browser_range_receipt_sha256(self):
            raise ValueError("range receipt_sha256 does not match canonical receipt payload")
        if self.expires_at is not None and self.expires_at <= self.capability.captured_at:
            raise ValueError("range receipt expires_at must be after captured_at")
        if self.expires_at is not None and (
            self.expires_at - self.capability.captured_at
        ).total_seconds() > RECENT_EXACT_QUOTE_REUSE_SECONDS:
            raise ValueError("range receipt TTL cannot exceed exact quote freshness window")
        return self

    @property
    def complete_coverage(self) -> bool:
        return (
            self.capability.status == BrowserRangeCapabilityStatus.CONFIRMED
            and self.usable_exact_pairs == set(self.query.requested_pairs)
            and self.expires_at is not None
        )

    @property
    def usable_exact_pairs(self) -> set[tuple[date, date]]:
        if self.expires_at is None or self.expired:
            return set()
        return {
            (cell.start_date, cell.end_date)
            for cell in self.cells
            if cell.price_finality == BrowserRangePriceFinality.EXACT
            and exact_cell_binding_error(
                self.query, self.capability, cell, self.expires_at, datetime.now(UTC)
            )
            is None
        }

    @property
    def requires_single_date_fallback(self) -> bool:
        return not self.complete_coverage

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and datetime.now(UTC) >= self.expires_at


def _range_json_default(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported range receipt value: {type(value).__name__}")


def browser_range_receipt_sha256(receipt: BrowserRangeCompletion | object) -> str:
    payload = (
        receipt.model_dump(mode="json")
        if isinstance(receipt, BrowserRangeCompletion)
        else receipt
    )
    if isinstance(payload, dict):
        payload = {
            key: value for key, value in payload.items() if key != "receipt_sha256"
        }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=_range_json_default,
        ).encode("utf-8")
    ).hexdigest()


def browser_range_query_fingerprint_sha256(query: BrowserDateRangeQuery | object) -> str:
    payload = query.model_dump(mode="json") if isinstance(query, BrowserDateRangeQuery) else query
    return _canonical_json_sha256(payload)


def range_completion_fallback_pairs(
    receipt: BrowserRangeCompletion,
) -> tuple[tuple[date, date], ...]:
    """Return all requested pairs for conservative single-date fallback."""

    if receipt.capability.status in {
        BrowserRangeCapabilityStatus.REJECTED,
        BrowserRangeCapabilityStatus.INCONCLUSIVE,
    }:
        return receipt.query.requested_pairs
    if receipt.expired:
        return receipt.query.requested_pairs
    covered = receipt.usable_exact_pairs
    return tuple(pair for pair in receipt.query.requested_pairs if pair not in covered)


class LodgingInventoryConfirmedQuery(DomainModel):
    destination: str = Field(min_length=1)
    start_date: date
    end_date: date
    adults: int = Field(ge=1, le=9)
    rooms: int = Field(ge=1, le=8)
    options: dict[str, JsonValue]

    @model_validator(mode="after")
    def validate_exact_lodging_scope(self) -> LodgingInventoryConfirmedQuery:
        if self.end_date <= self.start_date:
            raise ValueError("inventory receipt checkout must be after checkin")
        required = {
            "expected_lodging_place_key",
            "expected_package_area",
            "segment",
        }
        missing = required - self.options.keys()
        if missing:
            raise ValueError(
                "inventory receipt confirmed_query options miss exact place, area, or segment"
            )
        if any(
            not isinstance(self.options[field], str) or not str(self.options[field]).strip()
            for field in required
        ):
            raise ValueError(
                "inventory receipt exact place, area, and segment must be non-empty strings"
            )
        return self


class QunarLodgingExplicitEmptyEvidence(DomainModel):
    contract_version: str = Field(pattern="^qunar-visible-zero-inventory-v1$")
    result_count_text: str = Field(pattern="^共 0 家酒店满足条件$")
    empty_message: str = Field(pattern="^很抱歉，没有找到相关的酒店$")


class QunarLodgingPendingEvidence(DomainModel):
    contract_version: str = Field(pattern="^qunar-visible-search-pending-v1$")
    result_count_text: str = Field(pattern="^共 家酒店满足条件$")
    pending_message: str = Field(
        pattern=r"^请稍等,您查询的结果正在实时搜索中\.\.\.$"
    )
    observed_duration_ms: int = Field(ge=25_000, le=120_000)


class QunarLodgingConfirmedEmptyObservationReceipt(DomainModel):
    """One immutable parser-v1 observation embedded in the v2 parent receipt.

    ``captured_at`` intentionally remains the original string. Normalizing a
    JavaScript ``.000Z`` timestamp through ``datetime`` would change its bytes
    and make an independent SHA recomputation impossible.
    """

    schema_version: str = Field(pattern="^tripchord-lodging-inventory-receipt-v1$")
    parser_version: str = Field(pattern=f"^{PRODUCTION_VISIBLE_DOM_PARSER_VERSION}$")
    provider: BrowserProvider
    state: LodgingInventoryReceiptState
    confirmed_query: LodgingInventoryConfirmedQuery
    confirmation_scope: LodgingInventoryConfirmationScope
    scan_limit: int = Field(ge=1, le=100)
    scanned_count: int = Field(ge=0, le=100)
    candidate_summaries: tuple[JsonValue, ...] = ()
    explicit_empty_evidence: QunarLodgingExplicitEmptyEvidence | None = None
    provider_pending_evidence: QunarLodgingPendingEvidence | None = None
    page_url: str
    captured_at: str

    @field_validator("captured_at")
    @classmethod
    def validate_original_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observation captured_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("observation captured_at must include a timezone")
        return value

    @field_validator("page_url")
    @classmethod
    def validate_observation_url(cls, value: str) -> str:
        if not _is_allowed_provider_url(BrowserProvider.QUNAR, value):
            raise ValueError("observation page_url must be an allowed Qunar URL")
        return value

    @model_validator(mode="after")
    def validate_confirmed_empty_child(
        self,
    ) -> QunarLodgingConfirmedEmptyObservationReceipt:
        if (
            self.provider != BrowserProvider.QUNAR
            or self.state != LodgingInventoryReceiptState.CONFIRMED_EMPTY
            or self.scanned_count != 0
            or self.candidate_summaries
            or self.explicit_empty_evidence is None
            or self.provider_pending_evidence is not None
        ):
            raise ValueError(
                "observation child must be an audited Qunar parser-v1 confirmed_empty receipt"
            )
        return self


class QunarLodgingObservationLineage(DomainModel):
    schema_version: str = Field(pattern="^tripchord-browser-lineage-hash-v1$")
    isolation_scope: str = Field(
        pattern="^companion_owned_unfocused_normal_window_active_tab$"
    )
    runtime_lineage_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    window_lineage_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    tab_lineage_sha256: str = Field(pattern="^[a-f0-9]{64}$")


class QunarLodgingDetailFallbackResultState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSING = "missing"
    TARGET_REJECTED = "target_rejected"
    REDIRECT_OR_ISOLATION_REJECTED = "redirect_or_isolation_rejected"


class QunarLodgingDetailFallbackResult(DomainModel):
    property_id: str = Field(pattern="^(2112|2055|2071|2072|2075|2142)$")
    state: QunarLodgingDetailFallbackResultState
    verified_quote_count: int = Field(ge=0, le=12)


def _rotated_qunar_detail_property_ids(offset: int) -> tuple[str, str]:
    property_count = len(QUNAR_AUDITED_LODGING_DETAIL_PROPERTY_IDS)
    normalized_offset = offset % property_count
    rotated = (
        QUNAR_AUDITED_LODGING_DETAIL_PROPERTY_IDS[normalized_offset:]
        + QUNAR_AUDITED_LODGING_DETAIL_PROPERTY_IDS[:normalized_offset]
    )
    return rotated[0], rotated[1]


def _qunar_detail_seed_offset(query: LodgingInventoryConfirmedQuery) -> int:
    options = query.options
    fingerprint_payload = {
        "adults": query.adults,
        "destination": query.destination.strip().lower(),
        "end_date": query.end_date.isoformat(),
        "expected_lodging_place_key": str(
            options["expected_lodging_place_key"]
        ).strip().lower(),
        "expected_package_area": str(options["expected_package_area"])
        .strip()
        .lower(),
        "rooms": query.rooms,
        "segment": str(options["segment"]).strip().lower(),
        "start_date": query.start_date.isoformat(),
    }
    canonical = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    hash_value = 0x811C9DC5
    utf16_units = canonical.encode("utf-16-le")
    for index in range(0, len(utf16_units), 2):
        code_unit = int.from_bytes(utf16_units[index : index + 2], "little")
        hash_value ^= code_unit
        hash_value = (hash_value * 0x01000193) & 0xFFFFFFFF
    return hash_value % len(QUNAR_AUDITED_LODGING_DETAIL_PROPERTY_IDS)


def qunar_detail_seed_selection(
    query: LodgingInventoryConfirmedQuery,
) -> tuple[int, tuple[str, str]]:
    """Independently reproduce the Browser Companion's bounded seed selection."""

    offset = _qunar_detail_seed_offset(query)
    return offset, _rotated_qunar_detail_property_ids(offset)


class QunarLodgingDetailFallbackSummary(DomainModel):
    contract_version: str = Field(
        pattern="^tripchord-qunar-detail-fallback-summary-v[12]$"
    )
    attempted: bool
    target_limit: int = Field(ge=1, le=2)
    seed_selection_policy: str | None = None
    seed_selection_offset: int | None = Field(default=None, ge=0, le=5)
    target_property_ids: tuple[str, str]
    observed_results: tuple[
        QunarLodgingDetailFallbackResult,
        QunarLodgingDetailFallbackResult,
    ]
    verified_quote_count: int = Field(ge=0, le=12)

    @model_validator(mode="after")
    def validate_frozen_fallback(
        self,
        info: ValidationInfo,
    ) -> QunarLodgingDetailFallbackSummary:
        if self.contract_version.endswith("-v1"):
            historical_context = bool(
                isinstance(info.context, dict)
                and info.context.get(
                    _ALLOW_HISTORICAL_QUNAR_FALLBACK_V1_CONTEXT_KEY
                )
                is True
            )
            if not historical_context:
                raise ValueError(
                    "legacy Qunar fallback-summary-v1 requires the explicit "
                    "historical parsing path"
                )
            expected = ("2112", "2055")
            selection_contract_valid = (
                self.seed_selection_policy is None
                and self.seed_selection_offset is None
            )
        else:
            offset = self.seed_selection_offset
            expected = (
                _rotated_qunar_detail_property_ids(offset)
                if offset is not None
                else ("", "")
            )
            selection_contract_valid = (
                self.seed_selection_policy == QUNAR_DETAIL_SEED_SELECTION_POLICY
                and offset is not None
            )
        if (
            self.attempted is not True
            or self.target_limit != 2
            or not selection_contract_valid
            or self.target_property_ids != expected
            or tuple(item.property_id for item in self.observed_results) != expected
            or sum(item.verified_quote_count for item in self.observed_results)
            != self.verified_quote_count
        ):
            raise ValueError(
                "detail fallback summary does not match its audited two-target selection"
            )
        return self


class LodgingInventoryCandidateSummary(DomainModel):
    candidate_index: int = Field(ge=0, le=11)
    title: Annotated[str, Field(min_length=1, max_length=180)] | None
    area_evidence: Annotated[str, Field(min_length=1, max_length=240)] | None
    room_evidence: Annotated[str, Field(min_length=1, max_length=180)] | None
    price_evidence: Annotated[str, Field(min_length=1, max_length=180)] | None
    price_basis: LodgingInventoryPriceBasis
    price_finality: LodgingInventoryPriceFinality

    @model_validator(mode="after")
    def validate_visible_candidate_evidence(
        self,
    ) -> LodgingInventoryCandidateSummary:
        evidence = (
            self.title,
            self.area_evidence,
            self.room_evidence,
            self.price_evidence,
        )
        if not any(value is not None and value.strip() for value in evidence):
            raise ValueError(
                "inventory candidate summary requires at least one visible evidence field"
            )
        if any(value is not None and value != value.strip() for value in evidence):
            raise ValueError("inventory candidate evidence must be sanitized")
        return self


class FlightSearchConfirmedQuery(DomainModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    start_date: date
    end_date: date
    adults: int = Field(ge=1, le=9)
    origin_code: str = Field(min_length=3, max_length=3)
    destination_code: str = Field(min_length=3, max_length=3)

    @field_validator("origin_code", "destination_code")
    @classmethod
    def normalize_iata_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
            raise ValueError("flight receipt location codes must be three-letter IATA codes")
        return normalized

    @model_validator(mode="after")
    def validate_round_trip(self) -> FlightSearchConfirmedQuery:
        if self.end_date <= self.start_date:
            raise ValueError("flight receipt return date must be after departure date")
        return self


class FlightSearchCandidateSummary(DomainModel):
    candidate_index: int = Field(ge=0, le=19)
    title: Annotated[str, Field(min_length=1, max_length=180)] | None
    route_evidence: Annotated[str, Field(min_length=1, max_length=240)] | None
    schedule_evidence: Annotated[str, Field(min_length=1, max_length=240)] | None
    price_evidence: Annotated[str, Field(min_length=1, max_length=180)] | None
    currency: Annotated[str, Field(min_length=3, max_length=3)] | None
    amount: Decimal | None = Field(default=None, gt=0)
    price_basis: QuotePriceBasis
    price_classification: FlightCandidatePriceClassification
    # Route identifiers may be preserved even when the visible price remains
    # comparison-only.  They never turn a comparison observation into a
    # publishable quote without the separate party-total and segment contract.
    outbound_flight_numbers: tuple[str, ...] = ()
    return_flight_numbers: tuple[str, ...] = ()
    outbound_segments: tuple[dict[str, JsonValue], ...] = ()
    return_segments: tuple[dict[str, JsonValue], ...] = ()
    origin_airport_code: str | None = None
    destination_airport_code: str | None = None

    @field_validator("currency")
    @classmethod
    def normalize_optional_currency(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @model_validator(mode="after")
    def validate_visible_candidate_evidence(self) -> FlightSearchCandidateSummary:
        textual_evidence = (
            self.title,
            self.route_evidence,
            self.schedule_evidence,
            self.price_evidence,
        )
        if not any(value is not None and value.strip() for value in textual_evidence):
            raise ValueError("flight candidate summary requires visible evidence")
        if any(value is not None and value != value.strip() for value in textual_evidence):
            raise ValueError("flight candidate evidence must be sanitized")
        price_bearing = (
            self.price_classification != FlightCandidatePriceClassification.NO_VISIBLE_PRICE
        )
        if price_bearing:
            if (
                self.price_evidence is None
                or self.currency is None
                or self.amount is None
                or self.price_basis not in {QuotePriceBasis.PER_PERSON, QuotePriceBasis.TOTAL_PARTY}
            ):
                raise ValueError(
                    "price-bearing flight candidate requires amount, currency, "
                    "visible price evidence, and a usable comparison basis"
                )
        elif (
            self.price_evidence is not None
            or self.currency is not None
            or self.amount is not None
            or self.price_basis != QuotePriceBasis.UNKNOWN
        ):
            raise ValueError(
                "no-visible-price flight candidate cannot carry structured price evidence"
            )
        return self

    @property
    def price_bearing(self) -> bool:
        return self.price_classification != FlightCandidatePriceClassification.NO_VISIBLE_PRICE


def lodging_inventory_receipt_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def lodging_inventory_query_fingerprint_sha256(payload: object) -> str:
    """Hash the exact canonical confirmed-query payload used by both observations."""

    return lodging_inventory_receipt_sha256(payload)


class QunarLodgingReceiptObservation(DomainModel):
    ordinal: int = Field(ge=1, le=2)
    receipt: QunarLodgingConfirmedEmptyObservationReceipt
    receipt_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    captured_at: str
    query_fingerprint_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    lineage: QunarLodgingObservationLineage

    @field_validator("captured_at")
    @classmethod
    def validate_original_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observation captured_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("observation captured_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_child_hash_and_query(self) -> QunarLodgingReceiptObservation:
        raw_child = self.receipt.model_dump(mode="json")
        if (
            self.captured_at != self.receipt.captured_at
            or lodging_inventory_receipt_sha256(raw_child) != self.receipt_sha256
            or lodging_inventory_query_fingerprint_sha256(
                self.receipt.confirmed_query.model_dump(mode="json")
            )
            != self.query_fingerprint_sha256
        ):
            raise ValueError(
                "observation timestamp, child receipt SHA, or query fingerprint is invalid"
            )
        return self


class QunarLodgingConfirmedEmptyObservationChain(DomainModel):
    schema_version: str = Field(
        pattern="^tripchord-qunar-empty-observation-chain-v1$"
    )
    query_fingerprint_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    observations: tuple[
        QunarLodgingReceiptObservation,
        QunarLodgingReceiptObservation,
    ]
    observed_interval_ms: int = Field(ge=2_000, le=120_000)
    detail_fallback: QunarLodgingDetailFallbackSummary
    sealed_at: str

    @field_validator("sealed_at")
    @classmethod
    def validate_sealed_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observation chain sealed_at must be ISO-8601") from exc
        if parsed.tzinfo is None:
            raise ValueError("observation chain sealed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_order_query_and_lineage(
        self,
    ) -> QunarLodgingConfirmedEmptyObservationChain:
        first, second = self.observations
        first_at = datetime.fromisoformat(first.captured_at.replace("Z", "+00:00"))
        second_at = datetime.fromisoformat(second.captured_at.replace("Z", "+00:00"))
        sealed_at = datetime.fromisoformat(self.sealed_at.replace("Z", "+00:00"))
        exact_interval_ms = int((second_at - first_at).total_seconds() * 1000)
        if (
            first.ordinal != 1
            or second.ordinal != 2
            or first.query_fingerprint_sha256 != self.query_fingerprint_sha256
            or second.query_fingerprint_sha256 != self.query_fingerprint_sha256
            or first.receipt.confirmed_query != second.receipt.confirmed_query
            or first.lineage != second.lineage
            or exact_interval_ms != self.observed_interval_ms
            or exact_interval_ms < 2_000
            or sealed_at < second_at
        ):
            raise ValueError(
                "observation chain is unordered, too short, query-mismatched, or lineage-mismatched"
            )
        if self.detail_fallback.contract_version.endswith("-v2"):
            expected_offset = _qunar_detail_seed_offset(first.receipt.confirmed_query)
            if (
                self.detail_fallback.seed_selection_offset != expected_offset
                or self.detail_fallback.target_property_ids
                != _rotated_qunar_detail_property_ids(expected_offset)
            ):
                raise ValueError(
                    "detail fallback seed selection does not match the confirmed query"
                )
        return self


class LodgingInventoryReceipt(DomainModel):
    schema_version: str = Field(
        default="tripchord-lodging-inventory-receipt-v1",
        pattern="^tripchord-lodging-inventory-receipt-v[12]$",
    )
    parser_version: str = Field(pattern=f"^{PRODUCTION_VISIBLE_DOM_PARSER_VERSION}$")
    provider: BrowserProvider
    state: LodgingInventoryReceiptState
    confirmed_query: LodgingInventoryConfirmedQuery
    confirmation_scope: LodgingInventoryConfirmationScope
    scan_limit: int = Field(ge=1, le=100)
    scanned_count: int = Field(ge=0, le=100)
    candidate_summaries: tuple[LodgingInventoryCandidateSummary, ...] = ()
    explicit_empty_evidence: QunarLodgingExplicitEmptyEvidence | None = None
    provider_pending_evidence: QunarLodgingPendingEvidence | None = None
    page_url: str
    captured_at: datetime
    observation_chain: QunarLodgingConfirmedEmptyObservationChain | None = None

    _validate_captured_at = field_validator("captured_at")(_require_timezone)

    @field_validator("page_url")
    @classmethod
    def validate_page_url_shape(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError(
                "inventory receipt page_url must be HTTPS without embedded credentials"
            )
        return value

    @model_validator(mode="after")
    def validate_bounded_scan_evidence(self) -> LodgingInventoryReceipt:
        if not _is_allowed_provider_url(self.provider, self.page_url):
            raise ValueError("inventory receipt page_url does not belong to the declared provider")
        if self.scanned_count > self.scan_limit:
            raise ValueError("inventory receipt scan exceeds its declared finite limit")
        if self.state == LodgingInventoryReceiptState.CONFIRMED_EMPTY:
            if (
                self.schema_version != "tripchord-lodging-inventory-receipt-v2"
                or self.provider != BrowserProvider.QUNAR
                or self.scanned_count != 0
                or self.candidate_summaries
                or self.explicit_empty_evidence is None
                or self.provider_pending_evidence is not None
                or self.observation_chain is None
            ):
                raise ValueError(
                    "confirmed_empty requires the audited Qunar zero-result copy contract"
                )
        elif self.state == LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING:
            if (
                self.schema_version != "tripchord-lodging-inventory-receipt-v1"
                or self.provider != BrowserProvider.QUNAR
                or self.scanned_count != 0
                or self.candidate_summaries
                or self.explicit_empty_evidence is not None
                or self.provider_pending_evidence is None
                or self.observation_chain is not None
            ):
                raise ValueError(
                    "bounded_provider_pending requires the audited Qunar pending-shell contract"
                )
        else:
            if (
                self.schema_version != "tripchord-lodging-inventory-receipt-v1"
                or self.explicit_empty_evidence is not None
                or self.provider_pending_evidence is not None
                or self.observation_chain is not None
            ):
                raise ValueError("bounded_no_exact_quote cannot carry exhaustive empty evidence")
            if self.scanned_count == 0:
                raise ValueError(
                    "zero-scan inventory receipt is unproven without audited empty evidence"
                )
            summaries_are_usable = (
                bool(self.candidate_summaries)
                and len(self.candidate_summaries) == self.scanned_count
                and tuple(summary.candidate_index for summary in self.candidate_summaries)
                == tuple(range(self.scanned_count))
            )
            if not summaries_are_usable:
                raise ValueError(
                    "positive inventory scan requires continuous, typed candidate summaries"
                )
        if self.observation_chain is not None:
            first, second = self.observation_chain.observations
            second_at = datetime.fromisoformat(
                second.captured_at.replace("Z", "+00:00")
            )
            if (
                self.confirmed_query != second.receipt.confirmed_query
                or self.page_url != second.receipt.page_url
                or self.captured_at != second_at
                or self.parser_version != second.receipt.parser_version
                or self.provider != second.receipt.provider
                or self.confirmation_scope != second.receipt.confirmation_scope
                or self.scan_limit != second.receipt.scan_limit
                or self.scanned_count != second.receipt.scanned_count
                or self.explicit_empty_evidence != second.receipt.explicit_empty_evidence
                or self.provider_pending_evidence
                != second.receipt.provider_pending_evidence
                or first.receipt.page_url != self.page_url
            ):
                raise ValueError(
                    "v2 parent receipt does not exactly wrap the second canonical observation"
                )
        return self

    def computed_sha256(self) -> str:
        return lodging_inventory_receipt_sha256(self.model_dump(mode="json"))


def parse_historical_lodging_inventory_receipt(
    payload: object,
) -> LodgingInventoryReceipt:
    """Parse an archived receipt without authorizing it for current live use.

    Current Browser Bridge completion, quote normalization and Done-Gate paths
    deliberately use the normal ``model_validate`` entrypoint, which rejects
    the fixed-target v1 Qunar fallback summary. Only callers whose code path is
    explicitly historical may opt into this parser.
    """

    return LodgingInventoryReceipt.model_validate(
        payload,
        context={_ALLOW_HISTORICAL_QUNAR_FALLBACK_V1_CONTEXT_KEY: True},
    )


def flight_search_receipt_sha256(payload: object) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class FlightSearchReceipt(DomainModel):
    schema_version: str = Field(
        default="tripchord-flight-search-receipt-v1",
        pattern="^tripchord-flight-search-receipt-v1$",
    )
    parser_version: str = Field(pattern=f"^{PRODUCTION_VISIBLE_DOM_PARSER_VERSION}$")
    provider: BrowserProvider
    state: FlightSearchReceiptState
    confirmed_query: FlightSearchConfirmedQuery
    confirmation_scope: FlightSearchConfirmationScope
    scan_limit: int = Field(ge=1, le=20)
    scanned_count: int = Field(ge=1, le=20)
    candidate_summaries: tuple[FlightSearchCandidateSummary, ...]
    explicit_empty_evidence: None
    page_url: str
    captured_at: datetime

    _validate_captured_at = field_validator("captured_at")(_require_timezone)

    @field_validator("page_url")
    @classmethod
    def validate_page_url_shape(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("flight receipt page_url must be HTTPS without embedded credentials")
        return value

    @model_validator(mode="after")
    def validate_bounded_visible_search(self) -> FlightSearchReceipt:
        if not _is_allowed_provider_url(self.provider, self.page_url):
            raise ValueError("flight receipt page_url does not belong to the declared provider")
        if self.scanned_count > self.scan_limit:
            raise ValueError("flight receipt scan exceeds its declared finite limit")
        if len(self.candidate_summaries) != self.scanned_count or tuple(
            item.candidate_index for item in self.candidate_summaries
        ) != tuple(range(self.scanned_count)):
            raise ValueError(
                "flight receipt requires continuous, typed summaries for every scanned candidate"
            )
        price_bearing_count = sum(candidate.price_bearing for candidate in self.candidate_summaries)
        if (
            self.state == FlightSearchReceiptState.COMPARISON_PRICE_ONLY
            and price_bearing_count == 0
        ):
            raise ValueError(
                "comparison_price_only requires at least one visible price-bearing candidate"
            )
        if (
            self.state == FlightSearchReceiptState.BOUNDED_NO_EXACT_QUOTE
            and price_bearing_count != 0
        ):
            raise ValueError("bounded_no_exact_quote cannot hide visible comparison-price evidence")
        return self

    @property
    def price_bearing_candidate_count(self) -> int:
        return sum(candidate.price_bearing for candidate in self.candidate_summaries)

    def computed_sha256(self) -> str:
        return flight_search_receipt_sha256(self.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class TrustedSearchUrlContract:
    provider: BrowserProvider
    kind: BrowserVertical
    canonical_url: str
    party_availability_confirmed: bool
    pricing_context: str
    url_readback: tuple[tuple[str, JsonValue], ...]


_QUNAR_AUDITED_CITY_IDENTITIES = {
    "HGH": ("杭州", frozenset({"杭州", "hangzhou", "hgh"})),
    "MLE": ("马累", frozenset({"马累", "马尔代夫", "male", "mle"})),
}


def _normalized_location_alias(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value.strip()).lower()
        if not unicodedata.combining(character)
    )


def _require_round_trip_iata(
    query: BrowserSearchQuery,
    provider_name: str,
) -> tuple[str, str, date]:
    if query.end_date is None:
        raise ValueError(f"{provider_name} trusted flight URL requires a return date")
    if query.origin_code is None or query.destination_code is None:
        raise ValueError(
            f"{provider_name} trusted flight URL requires audited origin_code and destination_code"
        )
    return query.origin_code, query.destination_code, query.end_date


def ctrip_trusted_flight_search_url(query: BrowserSearchQuery) -> str:
    origin_code, destination_code, end_date = _require_round_trip_iata(query, "Ctrip")
    route = f"round-{origin_code.lower()}-{destination_code.lower()}"
    dates = f"{query.start_date.isoformat()}_{end_date.isoformat()}"
    return (
        f"https://flights.ctrip.com/international/search/{route}"
        f"?depdate={dates}&cabin=y_s&adult={query.adults}&child=0&infant=0"
    )


def fliggy_trusted_flight_search_url(query: BrowserSearchQuery) -> str:
    origin_code, destination_code, end_date = _require_round_trip_iata(query, "Fliggy")
    return (
        "https://sijipiao.fliggy.com/ie/flight_search_result.htm"
        f"?tripType=1&depCity={origin_code}&arrCity={destination_code}"
        f"&depDate={query.start_date.isoformat()}&arrDate={end_date.isoformat()}"
    )


def qunar_trusted_flight_city_names(query: BrowserSearchQuery) -> tuple[str, str]:
    origin_code, destination_code, _ = _require_round_trip_iata(query, "Qunar")
    if query.origin is None:
        raise ValueError("Qunar trusted flight URL requires an audited origin name")
    names: list[str] = []
    for field, value, code in (
        ("origin", query.origin, origin_code),
        ("destination", query.destination, destination_code),
    ):
        identity = _QUNAR_AUDITED_CITY_IDENTITIES.get(code)
        if identity is None:
            raise ValueError(
                f"Qunar trusted flight URL has no audited {field} identity for IATA {code}"
            )
        canonical_name, aliases = identity
        if _normalized_location_alias(value) not in aliases:
            raise ValueError(
                f"Qunar trusted flight URL requires an audited {field} name/IATA identity pair"
            )
        names.append(canonical_name)
    return names[0], names[1]


def qunar_trusted_flight_search_url(query: BrowserSearchQuery) -> str:
    _, _, end_date = _require_round_trip_iata(query, "Qunar")
    origin_name, destination_name = qunar_trusted_flight_city_names(query)
    parameters = (
        ("from", "flight_int_search"),
        ("showTotalPr", "0"),
        ("searchType", "RoundTripFlight"),
        ("fromCity", origin_name),
        ("toCity", destination_name),
        ("adultNum", str(query.adults)),
        ("childNum", "0"),
        ("fromDate", query.start_date.isoformat()),
        ("toDate", end_date.isoformat()),
    )
    encoded = urlencode(parameters, quote_via=url_quote, safe="")
    return f"https://flight.qunar.com/twell/flight/Search.jsp?{encoded}"


def tongcheng_trusted_flight_city_names(query: BrowserSearchQuery) -> tuple[str, str]:
    origin_code, destination_code, _ = _require_round_trip_iata(query, "Tongcheng")
    audited = {
        "HGH": ("杭州", {"杭州", "hangzhou", "hgh"}),
        "MLE": ("马累", {"马累", "马尔代夫", "male", "mle"}),
    }
    names: list[str] = []
    for field, value, code in (
        ("origin", query.origin, origin_code),
        ("destination", query.destination, destination_code),
    ):
        identity = audited.get(code)
        if identity is None or value is None:
            raise ValueError(
                f"Tongcheng trusted flight URL has no audited {field} identity for IATA {code}"
            )
        canonical_name, aliases = identity
        if _normalized_location_alias(value) not in aliases:
            raise ValueError(
                f"Tongcheng trusted flight URL requires an audited {field} name/IATA identity pair"
            )
        names.append(canonical_name)
    return names[0], names[1]


def tongcheng_trusted_flight_search_url(query: BrowserSearchQuery) -> str:
    origin_code, destination_code, end_date = _require_round_trip_iata(query, "Tongcheng")
    origin_name, destination_name = tongcheng_trusted_flight_city_names(query)
    # Tongcheng's public book1 list page decodes this seven-field `para`
    # contract as route, dates, trip type, party and cabin. It is a search
    # result surface despite the historical `book1.html` route name.
    para = "*".join(
        (
            origin_code,
            destination_code,
            query.start_date.isoformat(),
            end_date.isoformat(),
            "RT",
            f"{query.adults}_0_0",
            "Y|S|C|F",
        )
    )
    return (
        "https://www.ly.com/eliflight/book1.html"
        f"?para={url_quote(para, safe='*')}"
        f"&departureCity={url_quote(origin_name, safe='')}"
        f"&arrivalCity={url_quote(destination_name, safe='')}"
    )


def tongcheng_trusted_lodging_search_url(query: BrowserSearchQuery) -> str:
    """Build the extension's exact, read-only Tongcheng lodging result URL."""

    expected_place_key = str(
        query.options.get("expected_lodging_place_key", "")
    ).strip().lower()
    city_ids = {
        "hulhumale": "110018578",
        "maafushi": "110018575",
    }
    city_id = city_ids.get(expected_place_key)
    if (
        city_id is None
        or query.rooms != 1
        or query.end_date is None
        or query.end_date <= query.start_date
    ):
        raise ValueError(
            "Tongcheng trusted lodging URL requires an audited city and one room"
        )
    return (
        "https://www.ly.com/hotel/hotellist"
        f"?city={city_id}"
        f"&inDate={query.start_date.isoformat()}"
        f"&outDate={query.end_date.isoformat()}"
        f"&adultsNumber={query.adults}"
        "&roomNum=1&intl=1"
    )


def trusted_search_url_contract(
    provider: BrowserProvider,
    kind: BrowserVertical,
    query: BrowserSearchQuery,
) -> TrustedSearchUrlContract | None:
    """Return an exact, provider-specific contract for an internally generated URL.

    Matching the canonical URL byte-for-byte intentionally rejects alternate hosts,
    paths, duplicated/reordered fields, fragments, and any additional query parameter.
    """

    if query.search_url is None:
        return None
    if kind == BrowserVertical.LODGING and provider == BrowserProvider.TONGCHENG:
        expected = tongcheng_trusted_lodging_search_url(query)
        contract = TrustedSearchUrlContract(
            provider=provider,
            kind=kind,
            canonical_url=expected,
            party_availability_confirmed=True,
            pricing_context="requested_adults_and_single_room_in_search_url",
            url_readback=(
                ("destination", query.destination),
                ("start_date", query.start_date.isoformat()),
                ("end_date", query.end_date.isoformat() if query.end_date else None),
                ("adults", query.adults),
                ("rooms", query.rooms),
                ("expected_lodging_place_key", query.options.get("expected_lodging_place_key")),
            ),
        )
        if not hmac.compare_digest(query.search_url, expected):
            raise ValueError(
                "Tongcheng search_url does not exactly match the audited lodging contract"
            )
        return contract
    if kind != BrowserVertical.FLIGHT:
        raise ValueError("trusted search URLs are only enabled for audited read-only routes")
    if provider == BrowserProvider.CTRIP:
        expected = ctrip_trusted_flight_search_url(query)
        contract = TrustedSearchUrlContract(
            provider=provider,
            kind=kind,
            canonical_url=expected,
            party_availability_confirmed=True,
            pricing_context="requested_adults_in_search_url",
            url_readback=(
                ("origin_code", query.origin_code),
                ("destination_code", query.destination_code),
                ("start_date", query.start_date.isoformat()),
                ("end_date", query.end_date.isoformat() if query.end_date else None),
                ("adults", query.adults),
            ),
        )
    elif provider == BrowserProvider.FLIGGY:
        expected = fliggy_trusted_flight_search_url(query)
        contract = TrustedSearchUrlContract(
            provider=provider,
            kind=kind,
            canonical_url=expected,
            party_availability_confirmed=False,
            pricing_context="per_person_x_requested_adults",
            url_readback=(
                ("origin_code", query.origin_code),
                ("destination_code", query.destination_code),
                ("start_date", query.start_date.isoformat()),
                ("end_date", query.end_date.isoformat() if query.end_date else None),
            ),
        )
    elif provider == BrowserProvider.QUNAR:
        expected = qunar_trusted_flight_search_url(query)
        origin_name, destination_name = qunar_trusted_flight_city_names(query)
        contract = TrustedSearchUrlContract(
            provider=provider,
            kind=kind,
            canonical_url=expected,
            party_availability_confirmed=True,
            pricing_context="requested_adults_in_search_url",
            url_readback=(
                ("origin", origin_name),
                ("destination", destination_name),
                ("start_date", query.start_date.isoformat()),
                ("end_date", query.end_date.isoformat() if query.end_date else None),
                ("adults", query.adults),
            ),
        )
    elif provider == BrowserProvider.TONGCHENG:
        expected = tongcheng_trusted_flight_search_url(query)
        origin_name, destination_name = tongcheng_trusted_flight_city_names(query)
        contract = TrustedSearchUrlContract(
            provider=provider,
            kind=kind,
            canonical_url=expected,
            party_availability_confirmed=True,
            pricing_context="requested_adults_in_search_url",
            url_readback=(
                ("origin", origin_name),
                ("destination", destination_name),
                ("origin_code", query.origin_code),
                ("destination_code", query.destination_code),
                ("start_date", query.start_date.isoformat()),
                ("end_date", query.end_date.isoformat() if query.end_date else None),
                ("adults", query.adults),
            ),
        )
    else:
        raise ValueError(f"{provider.value} does not have an audited trusted search URL contract")
    if not hmac.compare_digest(query.search_url, expected):
        raise ValueError(
            f"{provider.value} search_url does not exactly match the audited flight contract"
        )
    return contract


class BrowserTaskSubmission(DomainModel):
    provider: BrowserProvider
    kind: BrowserVertical
    query: BrowserSearchQuery
    timeout_seconds: int = Field(default=120, ge=15, le=300)
    max_attempts: int = Field(default=2, ge=1, le=3)
    # A privacy-preserving hash owned by the API composition layer. Exact
    # quotes are reusable only inside this partition. ``None`` intentionally
    # disables reuse for callers that have no authenticated tenant/user scope.
    reuse_partition_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_vertical_and_url(self) -> BrowserTaskSubmission:
        if not self.query.party_shape_supported:
            raise ValueError(
                self.query.party_shape_failure
                or "provider does not support the requested mixed party shape"
            )
        if self.kind == BrowserVertical.FLIGHT and not self.query.origin:
            raise ValueError("flight searches require an origin")
        if self.kind == BrowserVertical.LODGING and self.query.end_date is None:
            raise ValueError("lodging searches require a checkout date")
        if self.query.search_url and not _is_allowed_provider_url(
            self.provider, self.query.search_url
        ):
            raise ValueError("search_url does not belong to the selected provider")
        if self.query.search_url:
            try:
                trusted_search_url_contract(self.provider, self.kind, self.query)
            except ValueError as exc:
                raise ValueError(
                    "search_url does not match an audited provider search contract"
                ) from exc
        return self


class BrowserQuote(DomainModel):
    provider: BrowserProvider
    kind: BrowserVertical
    page_url: str
    captured_at: datetime
    parser_version: str = Field(min_length=1, max_length=64)
    visible_evidence: str = Field(min_length=2, max_length=100_000)
    evidence_sha256: str
    currency: str = Field(min_length=3, max_length=3)
    amount: Decimal = Field(ge=0)
    price_basis: QuotePriceBasis
    taxes_included: bool | None
    title: str = Field(min_length=1, max_length=500)
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()

    @field_validator("evidence_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("evidence_sha256 must be a lowercase hexadecimal SHA-256 digest")
        return normalized

    @model_validator(mode="after")
    def validate_page_url(self) -> BrowserQuote:
        if not _is_allowed_provider_url(self.provider, self.page_url):
            raise ValueError("page_url does not belong to the quote provider")
        if self.parser_version != PRODUCTION_VISIBLE_DOM_PARSER_VERSION:
            raise ValueError("parser_version must match the production visible-DOM parser contract")
        common = {
            "query",
            "driver",
            "price_text",
            "visible_terms",
            "extraction",
        }
        required = (
            common
            | {
                "origin",
                "destination",
                "adults",
                "outbound_departure_at",
                "outbound_arrival_at",
                "return_departure_at",
                "return_arrival_at",
                "carrier_text",
                "connection_text",
                "baggage_text",
                "workflow_kind",
                "combination_status",
                "combination_id",
                "journey_price_scope",
                "price_finality",
                "price_basis_evidence",
                "tax_evidence",
                "party_availability_status",
                "selection_evidence",
                "action_trace",
            }
            if self.kind == BrowserVertical.FLIGHT
            else common
            | {
                "destination",
                "check_in",
                "check_out",
                "adults",
                "rooms",
                "room_text",
                "area_text",
                "breakfast_text",
                "cancellation_text",
                "transfer_text",
            }
        )
        missing = sorted(required - self.details.keys())
        if missing:
            raise ValueError(f"quote details are missing required fields: {missing}")
        if not isinstance(self.details["query"], dict):
            raise ValueError("quote details query must be an object")
        if not isinstance(self.details["driver"], dict):
            raise ValueError("quote details driver must be an object")
        query = self.details["query"]
        driver = self.details["driver"]
        query_fields = {
            "origin",
            "destination",
            "start_date",
            "end_date",
            "adults",
            "rooms",
            "currency",
            "origin_code",
            "destination_code",
            "search_url",
        }
        driver_fields = {
            "mode",
            "triggered",
            "confirmed_query",
            "confirmation_scope",
        }
        if missing_query := sorted(query_fields - query.keys()):
            raise ValueError(f"quote details query is missing fields: {missing_query}")
        if missing_driver := sorted(driver_fields - driver.keys()):
            raise ValueError(f"quote details driver is missing fields: {missing_driver}")
        if not isinstance(driver["triggered"], bool):
            raise ValueError("quote details driver triggered must be boolean")
        if driver["confirmed_query"] is not None and not isinstance(
            driver["confirmed_query"], dict
        ):
            raise ValueError("quote details driver confirmed_query must be an object or null")
        if self.kind == BrowserVertical.FLIGHT:
            self._validate_complete_round_trip()
        return self

    def _validate_complete_round_trip(self) -> None:
        expected_workflow = _FLIGHT_WORKFLOW_BY_PROVIDER[self.provider.value]
        if self.details["workflow_kind"] != expected_workflow:
            raise ValueError(
                f"{self.provider.value} flight workflow_kind must be {expected_workflow}"
            )
        fixed_fields = {
            "combination_status": "round_trip_complete",
            "journey_price_scope": "round_trip",
            "price_finality": "final_for_combination",
        }
        for field, expected in fixed_fields.items():
            if self.details[field] != expected:
                raise ValueError(f"flight {field} must be {expected}")
        party_status = self.details["party_availability_status"]
        if party_status not in _FLIGHT_PARTY_STATUSES_BY_PROVIDER[self.provider.value]:
            raise ValueError(
                "flight party_availability_status must describe the exact adult search "
                "context or an independently proven party price; allowed values: "
                + ", ".join(sorted(_FLIGHT_PARTY_STATUSES_BY_PROVIDER[self.provider.value]))
            )

        for field in (
            "combination_id",
            "price_basis_evidence",
            "tax_evidence",
            "selection_evidence",
        ):
            value = self.details[field]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"flight {field} must be a non-empty string")

        timestamps: dict[str, datetime] = {}
        for field in (
            "outbound_departure_at",
            "outbound_arrival_at",
            "return_departure_at",
            "return_arrival_at",
        ):
            raw = self.details[field]
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError(f"flight {field} must be a non-empty ISO timestamp")
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(f"flight {field} must be an ISO timestamp") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"flight {field} must include an explicit timezone")
            timestamps[field] = parsed
        if timestamps["outbound_arrival_at"] <= timestamps["outbound_departure_at"]:
            raise ValueError("flight outbound arrival must be after departure")
        if timestamps["return_departure_at"] <= timestamps["outbound_arrival_at"]:
            raise ValueError("flight return departure must be after outbound arrival")
        if timestamps["return_arrival_at"] <= timestamps["return_departure_at"]:
            raise ValueError("flight return arrival must be after departure")

        trace = self.details["action_trace"]
        if not isinstance(trace, list) or not trace or len(trace) > 8:
            raise ValueError("flight action_trace must contain between one and eight actions")
        actions: list[str] = []
        for index, entry in enumerate(trace):
            if not isinstance(entry, dict):
                raise ValueError(f"flight action_trace[{index}] must be an object")
            action = entry.get("action")
            if not isinstance(action, str) or action not in _ALLOWED_READ_ONLY_FLIGHT_ACTIONS:
                raise ValueError(
                    f"flight action_trace[{index}].action is outside the read-only allowlist"
                )
            serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True).lower()
            if any(marker in serialized for marker in _FORBIDDEN_ACTION_TRACE_MARKERS):
                raise ValueError(
                    f"flight action_trace[{index}] contains a transaction or account action"
                )
            actions.append(action)
        if actions[0] != "search":
            raise ValueError("flight action_trace must begin with search")
        if expected_workflow == "staged_outbound_return":
            if not any(
                action in {"select_outbound", "provider_auto_selected_outbound"}
                for action in actions
            ):
                raise ValueError(
                    "staged flight action_trace must prove a read-only outbound selection"
                )
        elif any(
            action in {
                "select_outbound",
                "reselect_outbound",
                "provider_auto_selected_outbound",
            }
            for action in actions
        ):
            raise ValueError("combined round-trip cards must not select an outbound flight")


def exact_cell_binding_error(
    query: BrowserDateRangeQuery,
    capability: RangeCapabilityEvidence,
    cell: BrowserRangeCell,
    expires_at: datetime | None,
    now: datetime,
) -> str | None:
    """Single fail-closed predicate shared by receipt and fallback decisions."""

    if expires_at is None or now >= expires_at:
        return "range receipt is missing or past expiry"
    quote = cell.quote
    if quote is None:
        return "exact range cell requires a bound BrowserQuote"
    maximum_future_time = now + timedelta(seconds=RANGE_RECEIPT_MAX_CLOCK_SKEW_SECONDS)
    timestamps = (capability.captured_at, cell.captured_at, quote.captured_at)
    if any(timestamp > maximum_future_time for timestamp in timestamps):
        return "range evidence is future-dated beyond allowed clock skew"
    if expires_at > now + timedelta(
        seconds=(
            RECENT_EXACT_QUOTE_REUSE_SECONDS + RANGE_RECEIPT_MAX_CLOCK_SKEW_SECONDS
        )
    ):
        return "range receipt expiry is future-dated beyond its freshness window"
    if now - quote.captured_at > timedelta(seconds=RECENT_EXACT_QUOTE_REUSE_SECONDS):
        return "bound BrowserQuote is stale"
    if max(timestamps) - min(timestamps) > timedelta(seconds=RECENT_EXACT_QUOTE_REUSE_SECONDS):
        return "range evidence is outside one freshness window"
    if capability.status != BrowserRangeCapabilityStatus.CONFIRMED:
        return "capability is not confirmed"
    if capability.evidence_type == BrowserRangeEvidenceType.NONE:
        return "confirmed range evidence cannot be none"
    if not capability.source_url or not capability.response_shape_sha256:
        return "confirmed range evidence requires source and response shape"
    if capability.task_id is None or capability.lease_id is None:
        return "confirmed range evidence requires task and lease lineage"
    if not capability.task_id.strip() or not capability.lease_id.strip():
        return "confirmed range evidence requires non-blank task and lease lineage"
    if (
        not capability.source_url.startswith("https://")
        or not _is_allowed_provider_url(query.provider, capability.source_url)
    ):
        return "confirmed range evidence source_url is not an allowed provider URL"
    visible_evidence_sha256 = hashlib.sha256(
        quote.visible_evidence.encode("utf-8")
    ).hexdigest()
    if (
        quote.provider != query.provider
        or quote.kind != query.kind
        or quote.currency != query.currency
        or quote.amount != cell.amount
        or quote.price_basis != QuotePriceBasis.TOTAL_PARTY
        or quote.taxes_included is not True
        or quote.evidence_sha256 != cell.evidence_sha256
        or quote.evidence_sha256 != visible_evidence_sha256
        or cell.price_basis != BrowserRangePriceBasis.TOTAL_FOR_PARTY
        or not cell.party_total_known
        or cell.taxes_and_fees_included is not True
    ):
        return "bound BrowserQuote does not match exact range cell"
    quote_query = quote.details.get("query")
    if not isinstance(quote_query, dict):
        return "bound BrowserQuote query is missing"
    if (
        quote_query.get("start_date") != cell.start_date.isoformat()
        or quote_query.get("end_date") != cell.end_date.isoformat()
        or quote_query.get("origin") != query.origin
        or quote_query.get("destination") != query.destination
        or quote_query.get("origin_code") != query.origin_code
        or quote_query.get("destination_code") != query.destination_code
        or quote_query.get("currency") != query.currency
        or quote_query.get("adults") != cell.party.adults
        or quote_query.get("rooms") != cell.party.rooms
    ):
        return "bound BrowserQuote route, dates, or party do not match"
    if query.party.children or query.party.infants:
        return "mixed party range requires complete quote party fields"
    identity = (
        quote.details.get("combination_id")
        if query.kind == BrowserVertical.FLIGHT
        else f"{quote.title}|{quote.details.get('room_text', '')}"
    )
    if not isinstance(identity, str) or identity != cell.product_identity:
        return "range product_identity does not match bound BrowserQuote"
    return None


BrowserRangeCell.model_rebuild(_types_namespace={"BrowserQuote": BrowserQuote})


class BrowserFailure(DomainModel):
    code: BrowserFailureCode
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    page_url: str | None = None
    captured_at: datetime
    details: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at(cls, value: datetime) -> datetime:
        return _require_timezone(value)

    @field_validator("page_url")
    @classmethod
    def validate_page_url_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("failure page_url must be HTTPS")
        return value


class BrowserTaskCompletion(DomainModel):
    state: BrowserTaskState
    quotes: tuple[BrowserQuote, ...] = ()
    failure: BrowserFailure | None = None

    @model_validator(mode="after")
    def validate_terminal_result(self) -> BrowserTaskCompletion:
        if self.state not in {
            BrowserTaskState.SUCCEEDED,
            BrowserTaskState.BLOCKED,
            BrowserTaskState.FAILED,
        }:
            raise ValueError("completion state must be succeeded, blocked, or failed")
        if self.state == BrowserTaskState.SUCCEEDED:
            if not self.quotes:
                raise ValueError("successful completion requires at least one quote")
            if self.failure is not None:
                raise ValueError("successful completion cannot include a failure")
        elif self.failure is None:
            raise ValueError("blocked and failed completions require a structured failure")
        elif self.quotes:
            raise ValueError("blocked and failed completions cannot include quotes")
        elif self.failure.code == BrowserFailureCode.CANCELLED:
            raise ValueError("cancelled failures are reserved for the bridge controller")
        if self.state == BrowserTaskState.BLOCKED and self.failure is not None:
            allowed = {
                BrowserFailureCode.CAPTCHA_REQUIRED,
                BrowserFailureCode.LOGIN_REQUIRED,
                BrowserFailureCode.PERMISSION_DENIED,
            }
            if self.failure.code not in allowed:
                raise ValueError("blocked state is reserved for user-action or permission gates")
        return self


class BrowserTaskSnapshot(DomainModel):
    id: str
    provider: BrowserProvider
    kind: BrowserVertical
    query: BrowserSearchQuery
    state: BrowserTaskState
    created_at: datetime
    updated_at: datetime
    attempt_count: int = Field(ge=0)
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    quotes: tuple[BrowserQuote, ...] = ()
    failure: BrowserFailure | None = None
    reused_from_task_id: str | None = None
    reuse_age_seconds: float | None = Field(default=None, ge=0)
    inflight_coalesced: bool = False


class BrowserTaskLease(DomainModel):
    task_id: str
    provider: BrowserProvider
    kind: BrowserVertical
    query: BrowserSearchQuery
    timeout_seconds: int
    claim_token: str
    claimed_at: datetime
    lease_expires_at: datetime


class SubmitBrowserTasksRequest(DomainModel):
    tasks: tuple[BrowserTaskSubmission, ...] = Field(min_length=1, max_length=24)


class SubmitBrowserTasksResponse(DomainModel):
    tasks: tuple[BrowserTaskSnapshot, ...]


class SubmitFormalBrowserTasksRequest(DomainModel):
    execution_capability: dict[str, object]
    tasks: tuple[BrowserTaskSubmission, ...] = Field(min_length=1, max_length=24)


class CancelFormalBrowserTasksRequest(DomainModel):
    execution_capability: dict[str, object]
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=24)
    reason: str = Field(min_length=1, max_length=1000)


class ReadFormalBrowserTasksRequest(DomainModel):
    execution_capability: dict[str, object]
    task_ids: tuple[str, ...] = Field(min_length=1, max_length=24)


class FormalIComSearchRequest(DomainModel):
    execution_capability: dict[str, object]
    query_task_id: str = Field(min_length=1, max_length=240)
    query: dict[str, object]


class BrowserCompanionBuildIdentity(DomainModel):
    protocol_version: str = Field(
        default=COMPANION_CONTROL_PROTOCOL_VERSION,
        pattern=f"^{COMPANION_CONTROL_PROTOCOL_VERSION}$",
    )
    manifest_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )
    build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_runtime_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )


class BrowserCompanionReloadReceipt(DomainModel):
    companion_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^companion-reload-[A-Za-z0-9_-]+$",
    )
    receipt_token: str = Field(min_length=32, max_length=128)
    delivery_generation: int = Field(ge=1, le=32)
    state: BrowserCompanionReloadReceiptState
    build_identity: BrowserCompanionBuildIdentity
    runtime_instance_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
    )
    previous_runtime_instance_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
    )
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_receipt_shape(self) -> BrowserCompanionReloadReceipt:
        if self.state == BrowserCompanionReloadReceiptState.FAILED:
            if self.failure_code is None:
                raise ValueError("failed reload receipt requires failure_code")
        elif self.failure_code is not None:
            raise ValueError("only a failed reload receipt can carry failure_code")
        return self


class BrowserCompanionReloadControl(DomainModel):
    action: BrowserCompanionControlKind = BrowserCompanionControlKind.RELOAD_EXTENSION
    protocol_version: str = Field(
        default=COMPANION_CONTROL_PROTOCOL_VERSION,
        pattern=f"^{COMPANION_CONTROL_PROTOCOL_VERSION}$",
    )
    request_id: str
    target_build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_runtime_instance_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
    )
    delivery_generation: int = Field(ge=1, le=32)
    receipt_token: str = Field(min_length=32, max_length=128)
    expires_at: datetime

    _validate_expires_at = field_validator("expires_at")(_require_timezone)


class BrowserCompanionReloadRequestBody(DomainModel):
    expected_current_build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_build_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: BrowserCompanionReloadReasonCode
    expires_in_seconds: int = Field(default=120, ge=15, le=600)
    max_drain_seconds: int = Field(default=120, ge=0, le=600)

    @model_validator(mode="after")
    def validate_reload_window(self) -> BrowserCompanionReloadRequestBody:
        if hmac.compare_digest(
            self.expected_current_build_sha256,
            self.target_build_sha256,
        ):
            raise ValueError("target build must differ from the expected current build")
        if self.max_drain_seconds > self.expires_in_seconds:
            raise ValueError("max_drain_seconds cannot exceed expires_in_seconds")
        return self


class BrowserCompanionReloadRequestSnapshot(DomainModel):
    id: str
    kind: BrowserCompanionControlKind
    companion_id: str
    idempotency_key: str
    expected_current_build_sha256: str
    target_build_sha256: str
    reason_code: BrowserCompanionReloadReasonCode
    state: BrowserCompanionControlState
    requested_at: datetime
    updated_at: datetime
    expires_at: datetime
    drain_deadline_at: datetime
    delivery_generation: int = Field(ge=0, le=32)
    expected_runtime_instance_id: str
    accepted_at: datetime | None = None
    applied_at: datetime | None = None
    observed_build_sha256: str | None = None
    observed_runtime_instance_id: str | None = None
    failure_code: str | None = None

    _validate_requested_at = field_validator("requested_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)
    _validate_expires_at = field_validator("expires_at")(_require_timezone)
    _validate_drain_deadline_at = field_validator("drain_deadline_at")(_require_timezone)
    _validate_accepted_at = field_validator("accepted_at")(_require_optional_timezone)
    _validate_applied_at = field_validator("applied_at")(_require_optional_timezone)


class ClaimBrowserTasksRequest(DomainModel):
    companion_id: str = Field(min_length=1, max_length=128)
    providers: tuple[BrowserProvider, ...] = ()
    authorized_scope_keys: tuple[str, ...] = ()
    adapter_version: str | None = None
    contract_version: str | None = None
    limit: int = Field(default=6, ge=1, le=6)
    build_identity: BrowserCompanionBuildIdentity | None = None
    runtime_instance_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$",
    )
    reload_receipt: BrowserCompanionReloadReceipt | None = None

    @model_validator(mode="after")
    def validate_control_identity(self) -> ClaimBrowserTasksRequest:
        if (self.build_identity is None) != (self.runtime_instance_id is None):
            raise ValueError(
                "build_identity and runtime_instance_id must be supplied together"
            )
        if (
            self.reload_receipt is not None
            and self.reload_receipt.companion_id != self.companion_id
        ):
            raise ValueError("reload_receipt companion_id must match the claim companion")
        if self.reload_receipt is not None and self.build_identity is None:
            raise ValueError("reload_receipt requires the current companion build identity")
        if self.reload_receipt is not None and (
            self.reload_receipt.build_identity != self.build_identity
            or self.reload_receipt.runtime_instance_id != self.runtime_instance_id
        ):
            raise ValueError(
                "reload_receipt identity must match the claim runtime identity"
            )
        return self


class FormalBrowserActivationRequest(DomainModel):
    job_id: str = Field(min_length=1, max_length=200)
    challenge_id: str = Field(min_length=1, max_length=200)
    execution_capability: dict[str, object]
    companion_binding: dict[str, object]


class ClaimBrowserTasksResponse(DomainModel):
    leases: tuple[BrowserTaskLease, ...]
    control: BrowserCompanionReloadControl | None = None
    formal_activation_request: FormalBrowserActivationRequest | None = None


class BrowserCompanionHeartbeatRequest(DomainModel):
    companion_id: str = Field(min_length=1, max_length=128)
    providers: tuple[BrowserProvider, ...] = Field(min_length=1)
    authorized_scope_keys: tuple[str, ...] = ()
    adapter_version: str | None = None
    contract_version: str | None = None
    build_identity: BrowserCompanionBuildIdentity | None = None
    runtime_instance_id: str | None = None
    formal_activation_ack: FormalBrowserActivationRequest | None = None

    @model_validator(mode="after")
    def validate_runtime_identity(self) -> BrowserCompanionHeartbeatRequest:
        if (self.build_identity is None) != (self.runtime_instance_id is None):
            raise ValueError(
                "build_identity and runtime_instance_id must be supplied together"
            )
        return self


class BrowserCompanionHeartbeat(DomainModel):
    companion_id: str = Field(min_length=1, max_length=128)
    providers: tuple[BrowserProvider, ...] = Field(min_length=1)
    last_seen: datetime
    age_seconds: float = Field(ge=0)
    is_fresh: bool
    authorized_scope_keys: tuple[str, ...] = ()
    adapter_version: str | None = None
    contract_version: str | None = None
    build_identity: BrowserCompanionBuildIdentity | None = None
    runtime_instance_id: str | None = None

    _validate_last_seen = field_validator("last_seen")(_require_timezone)


class BrowserCompanionHeartbeatResponse(BrowserCompanionHeartbeat):
    formal_activation_request: FormalBrowserActivationRequest | None = None


class BrowserCompanionStatusResponse(DomainModel):
    status: str
    server_time: datetime
    stale_after_seconds: int = Field(gt=0)
    companions: tuple[BrowserCompanionHeartbeat, ...]

    _validate_server_time = field_validator("server_time")(_require_timezone)


class BrowserSourceExecutionAttestation(DomainModel):
    """Public Companion identity bound to one visible-DOM observation.

    The lease token authorizes a state transition; it is deliberately not
    accepted as proof that the production parser/build/runtime performed the
    observation.  This separate envelope is checked against the live claim
    heartbeat and the exact task/query/result before a formal task can finish.
    """

    schema_version: str = Field(
        default=SOURCE_EXECUTION_ATTESTATION_SCHEMA,
        pattern=f"^{SOURCE_EXECUTION_ATTESTATION_SCHEMA}$",
    )
    task_id: str = Field(min_length=1)
    provider: BrowserProvider
    kind: BrowserVertical
    companion_id: str = Field(min_length=1, max_length=128)
    runtime_instance_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$"
    )
    build_identity: BrowserCompanionBuildIdentity
    execution_environment: str = Field(
        pattern=(
            "^(chrome_extension_service_worker|"
            "untrusted_external_executor)$"
        )
    )
    parser_version: str = Field(min_length=1, max_length=64)
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime

    _validate_completed_at = field_validator("completed_at")(_require_timezone)


class BrowserSourceExecutionReceipt(DomainModel):
    """Server-derived, formal-job-bound proof for one Companion observation."""

    schema_version: str = Field(
        default=SOURCE_EXECUTION_RECEIPT_SCHEMA,
        pattern=f"^{SOURCE_EXECUTION_RECEIPT_SCHEMA}$",
    )
    task_id: str = Field(min_length=1)
    provider: BrowserProvider
    kind: BrowserVertical
    companion_id: str = Field(min_length=1, max_length=128)
    runtime_instance_id: str
    build_identity: BrowserCompanionBuildIdentity
    execution_environment: str
    parser_version: str
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability_id: str = Field(min_length=1)
    challenge_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    terminal_job_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    job_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _validate_completed_at = field_validator("completed_at")(_require_timezone)


class CompleteBrowserTaskRequest(DomainModel):
    claim_token: str = Field(min_length=16)
    completion: BrowserTaskCompletion
    source_execution_attestation: BrowserSourceExecutionAttestation | None = None


class BrowserBridgeError(RuntimeError):
    pass


class BrowserTaskNotFoundError(BrowserBridgeError):
    pass


class BrowserClaimError(BrowserBridgeError):
    pass


class BrowserCompanionControlError(BrowserBridgeError):
    pass


class BrowserCompanionReloadNotFoundError(BrowserCompanionControlError):
    pass


@dataclass
class _TaskRecord:
    id: str
    submission: BrowserTaskSubmission
    state: BrowserTaskState
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    claim_token: str | None = None
    lease_expires_at: datetime | None = None
    quotes: tuple[BrowserQuote, ...] = ()
    failure: BrowserFailure | None = None
    reused_from_task_id: str | None = None
    reuse_age_seconds: float | None = None
    inflight_coalesce_count: int = 0
    formal_execution_capability: dict[str, object] | None = None
    source_execution_receipt: BrowserSourceExecutionReceipt | None = None


@dataclass(frozen=True)
class _CompanionHeartbeatRecord:
    companion_id: str
    providers: tuple[BrowserProvider, ...]
    last_seen: datetime
    authorized_scope_keys: tuple[str, ...] = ()
    adapter_version: str | None = None
    contract_version: str | None = None
    build_identity: BrowserCompanionBuildIdentity | None = None
    runtime_instance_id: str | None = None


@dataclass
class _CompanionReloadRecord:
    id: str
    companion_id: str
    idempotency_key: str
    request: BrowserCompanionReloadRequestBody
    request_fingerprint_sha256: str
    state: BrowserCompanionControlState
    requested_at: datetime
    updated_at: datetime
    expires_at: datetime
    drain_deadline_at: datetime
    expected_runtime_instance_id: str
    delivery_generation: int = 0
    receipt_token_sha256: str | None = None
    accepted_at: datetime | None = None
    applied_at: datetime | None = None
    observed_build_sha256: str | None = None
    observed_runtime_instance_id: str | None = None
    failure_code: str | None = None


class PersistedBrowserTaskRecord(DomainModel):
    """Task state safe to retain without a browser or lease identity."""

    id: str = Field(min_length=1)
    submission: BrowserTaskSubmission
    state: BrowserTaskState
    created_at: datetime
    updated_at: datetime
    attempt_count: int = Field(ge=0)
    quotes: tuple[BrowserQuote, ...] = ()
    failure: BrowserFailure | None = None
    reused_from_task_id: str | None = None
    reuse_age_seconds: float | None = Field(default=None, ge=0)
    formal_execution_capability: dict[str, object] | None = None

    _validate_created_at = field_validator("created_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)


class PersistedBrowserCompanionReloadRecord(DomainModel):
    id: str
    companion_id: str
    idempotency_key: str
    request: BrowserCompanionReloadRequestBody
    request_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: BrowserCompanionControlState
    requested_at: datetime
    updated_at: datetime
    expires_at: datetime
    drain_deadline_at: datetime
    expected_runtime_instance_id: str
    delivery_generation: int = Field(ge=0, le=32)
    receipt_token_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    accepted_at: datetime | None = None
    applied_at: datetime | None = None
    observed_build_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    observed_runtime_instance_id: str | None = None
    failure_code: str | None = None

    _validate_requested_at = field_validator("requested_at")(_require_timezone)
    _validate_updated_at = field_validator("updated_at")(_require_timezone)
    _validate_expires_at = field_validator("expires_at")(_require_timezone)
    _validate_drain_deadline_at = field_validator("drain_deadline_at")(_require_timezone)
    _validate_accepted_at = field_validator("accepted_at")(_require_optional_timezone)
    _validate_applied_at = field_validator("applied_at")(_require_optional_timezone)


class BrowserBridgePersistedState(DomainModel):
    schema_version: str = Field(
        default="tripchord-browser-bridge-state-v2",
        pattern="^tripchord-browser-bridge-state-v2$",
    )
    saved_at: datetime
    tasks: tuple[PersistedBrowserTaskRecord, ...] = ()
    reload_requests: tuple[PersistedBrowserCompanionReloadRecord, ...] = ()

    _validate_saved_at = field_validator("saved_at")(_require_timezone)

    @model_validator(mode="before")
    @classmethod
    def migrate_v1_state(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("schema_version") == (
            "tripchord-browser-bridge-state-v1"
        ):
            migrated = dict(value)
            migrated["schema_version"] = "tripchord-browser-bridge-state-v2"
            migrated["reload_requests"] = []
            return migrated
        return value


class BrowserBridgeStateStore(Protocol):
    """External state boundary; implementations must stay local and lease-token-free."""

    def load(self) -> BrowserBridgePersistedState | None: ...

    def save(self, state: BrowserBridgePersistedState) -> None: ...


class JsonFileBrowserBridgeStateStore:
    """Atomic local JSON store used only when explicitly configured by path."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("browser bridge state path must be absolute")
        self._path = path

    def load(self) -> BrowserBridgePersistedState | None:
        if not self._path.exists():
            return None
        try:
            payload: object = json.loads(self._path.read_text(encoding="utf-8"))
            return BrowserBridgePersistedState.model_validate(payload)
        except (OSError, ValueError) as exc:
            raise BrowserBridgeError(
                f"browser bridge state cannot be loaded: {self._path}"
            ) from exc

    def save(self, state: BrowserBridgePersistedState) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self._path.with_name(
            f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path)
            self._path.chmod(0o600)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def browser_bridge_state_store_from_env() -> BrowserBridgeStateStore | None:
    raw_path = os.environ.get(BROWSER_BRIDGE_STATE_PATH_ENV, "").strip()
    if not raw_path:
        return None
    return JsonFileBrowserBridgeStateStore(Path(raw_path))


class BrowserTaskBridge:
    """Lease queue for an explicitly paired local Chrome companion.

    It remains in-memory by default. Setting ``TRIPCHORD_BROWSER_BRIDGE_STATE_PATH``
    enables an atomic, local-only task snapshot. Claim tokens and companion heartbeat
    identity are never persisted; a claimed task is safely requeued after restart.
    """

    def __init__(
        self,
        *,
        max_pending_tasks: int = 100,
        state_store: BrowserBridgeStateStore | None = None,
        terminal_retention_seconds: int = DEFAULT_TERMINAL_RECORD_RETENTION_SECONDS,
        max_terminal_records: int = DEFAULT_MAX_TERMINAL_RECORDS,
        max_companion_control_records: int = DEFAULT_MAX_COMPANION_CONTROL_RECORDS,
        now: Callable[[], datetime] | None = None,
        source_authority: FormalLiveSourceAuthority | None = None,
        durable_store: Any | None = None,
        durable_tenant_id: str | None = None,
        durable_tenant_partition: str | None = None,
    ) -> None:
        if max_pending_tasks < 1:
            raise ValueError("max_pending_tasks must be positive")
        if terminal_retention_seconds < RECENT_EXACT_QUOTE_REUSE_SECONDS:
            raise ValueError(
                "terminal retention must cover the exact-quote reuse window"
            )
        if max_terminal_records < 1:
            raise ValueError("max_terminal_records must be positive")
        if max_companion_control_records < 1:
            raise ValueError("max_companion_control_records must be positive")
        self._records: dict[str, _TaskRecord] = {}
        self._active_consumers: dict[str, int] = {}
        self._companion_heartbeats: dict[str, _CompanionHeartbeatRecord] = {}
        self._reload_requests: dict[str, _CompanionReloadRecord] = {}
        self._max_pending_tasks = max_pending_tasks
        self._terminal_retention = timedelta(seconds=terminal_retention_seconds)
        self._max_terminal_records = max_terminal_records
        self._max_companion_control_records = max_companion_control_records
        self._now = now or (lambda: datetime.now(UTC))
        if durable_store is not None and state_store is not None:
            raise ValueError("durable browser bridge cannot also use JSON state")
        self._durable_store = durable_store
        self._durable_tenant_id = durable_tenant_id or "browser-bridge"
        self._durable_tenant_partition = durable_tenant_partition or hashlib.sha256(
            self._durable_tenant_id.encode("utf-8")
        ).hexdigest()
        self._durable_claims: dict[str, Any] = {}
        self._state_store = (
            None
            if durable_store is not None
            else state_store or browser_bridge_state_store_from_env()
        )
        self._source_authority = source_authority
        self._changed = asyncio.Condition()
        self._restore_persisted_state()

    def bind_source_authority(
        self,
        source_authority: FormalLiveSourceAuthority,
    ) -> None:
        """Bind the production authority once, including externally built bridges."""

        if self._source_authority is not None and self._source_authority is not source_authority:
            raise RuntimeError("browser bridge already uses a different source authority")
        self._source_authority = source_authority

    @property
    def durable_enabled(self) -> bool:
        return self._durable_store is not None

    async def _durable_submit_many(
        self,
        values: tuple[BrowserTaskSubmission, ...],
        capability: dict[str, object] | None,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        assert self._durable_store is not None
        pending = await self._durable_store.count_pending(tenant_id=self._durable_tenant_id)
        task_ids = [f"browser-task-{uuid4()}" for _ in values]
        projected_keys: set[str] = set()
        for submission, task_id in zip(values, task_ids, strict=True):
            options = submission.query.options
            allow_reuse = options.get("__tripchord_allow_recent_quote_reuse") is True
            force_fresh = options.get("__tripchord_force_fresh") is True
            if not allow_reuse or force_fresh:
                projected_keys.add(task_id)
                continue
            tenant_partition = (
                submission.reuse_partition_sha256
                or hashlib.sha256(task_id.encode("utf-8")).hexdigest()
            )
            partition = self._durable_store._digest(
                {
                    "tenant_id": self._durable_tenant_id,
                    "tenant_partition": tenant_partition,
                    "capability": capability,
                }
            )
            projected_keys.add(
                self._durable_store._digest(
                    {
                        "authority": self._durable_store._authority_partition,
                        "tenant": self._durable_tenant_id,
                        "partition": partition,
                        "fingerprint": self._durable_store._submission_fingerprint(
                            submission
                        ),
                    }
                )
            )
        if pending + len(projected_keys) > self._max_pending_tasks:
            raise BrowserBridgeError("browser task queue capacity exceeded")
        snapshots: list[BrowserTaskSnapshot] = []
        for submission, task_id in zip(values, task_ids, strict=True):
            job_id = (
                str(capability["terminal_job_id"])
                if capability is not None and capability.get("terminal_job_id") is not None
                else None
            )
            request_sha256 = (
                str(capability["request_sha256"])
                if capability is not None and capability.get("request_sha256") is not None
                else None
            )
            run_id = (
                str(capability["run_id"])
                if capability is not None and capability.get("run_id") is not None
                else None
            )
            allow_reuse = (
                submission.query.options.get("__tripchord_allow_recent_quote_reuse")
                is True
            )
            force_fresh = (
                submission.query.options.get("__tripchord_force_fresh") is True
            )
            projection = await self._durable_store.submit_consumer(
                submission,
                consumer_id=task_id,
                tenant_id=self._durable_tenant_id,
                # The browser protocol has no user-tenant field.  Reuse
                # partitions are server-derived by the planner; absent one,
                # isolate this request rather than trusting a client tenant.
                tenant_partition=(
                    submission.reuse_partition_sha256
                    or hashlib.sha256(task_id.encode("utf-8")).hexdigest()
                ),
                capability=capability,
                job_id=job_id,
                request_sha256=request_sha256,
                run_id=run_id,
                run_revision=None,
                allow_recent_quote_reuse=allow_reuse,
                force_fresh=force_fresh,
            )
            snapshots.append(projection.snapshot)
        return tuple(snapshots)

    async def _durable_session(
        self,
        companion_id: str,
        *,
        providers: tuple[BrowserProvider, ...],
        scopes: tuple[str, ...] = (),
        adapter_version: str | None = None,
        contract_version: str | None = None,
        build_identity: BrowserCompanionBuildIdentity | None = None,
        runtime_instance_id: str | None = None,
    ) -> Any:
        assert self._durable_store is not None
        session_row_id = hashlib.sha256(
            f"{self._durable_store._authority_partition}:companion:{companion_id}".encode()
        ).hexdigest()
        return await self._durable_store.upsert_companion_session(
            session_id=session_row_id,
            companion_id=companion_id,
            runtime_instance_id=runtime_instance_id,
            build_identity=(
                build_identity.model_dump(mode="json") if build_identity is not None else None
            ),
            providers=[provider.value for provider in providers],
            scopes=list(scopes),
            expires_at=self._utc_now()
            + timedelta(seconds=COMPANION_HEARTBEAT_STALE_AFTER_SECONDS),
            adapter_version=adapter_version,
            contract_version=contract_version,
        )

    async def _durable_claim(
        self,
        companion_id: str,
        *,
        providers: tuple[BrowserProvider, ...],
        authorized_scope_keys: tuple[str, ...] = (),
        adapter_version: str | None = None,
        contract_version: str | None = None,
        build_identity: BrowserCompanionBuildIdentity | None = None,
        runtime_instance_id: str | None = None,
        limit: int = 6,
    ) -> tuple[BrowserTaskLease, ...]:
        assert self._durable_store is not None
        session = await self._durable_session(
            companion_id,
            providers=providers or tuple(BrowserProvider),
            scopes=authorized_scope_keys,
            adapter_version=adapter_version,
            contract_version=contract_version,
            build_identity=build_identity,
            runtime_instance_id=runtime_instance_id,
        )
        leases = await self._durable_store.claim_acquisitions(
            owner=companion_id,
            session_id=session.id,
            session_generation=session.session_generation,
            runtime_instance_id=runtime_instance_id,
            build_identity=(
                build_identity.model_dump(mode="json") if build_identity is not None else None
            ),
            limit=limit,
        )
        result: list[BrowserTaskLease] = []
        for lease in leases:
            public_id = lease.public_task_id
            self._durable_claims[public_id] = lease
            result.append(
                BrowserTaskLease(
                    task_id=public_id,
                    provider=lease.submission.provider,
                    kind=lease.submission.kind,
                    query=lease.submission.query,
                    timeout_seconds=lease.submission.timeout_seconds,
                    claim_token=lease.claim_token,
                    claimed_at=lease.claimed_at,
                    lease_expires_at=lease.lease_expires_at,
                )
            )
        return tuple(result)

    async def _durable_record_for(
        self,
        task_id: str,
        lease: Any,
    ) -> _TaskRecord:
        assert self._durable_store is not None
        projection = await self._durable_store.get_consumer(
            task_id, tenant_id=self._durable_tenant_id
        )
        if projection is None:
            raise BrowserTaskNotFoundError(f"browser task not found: {task_id}")
        snapshot = projection.snapshot
        return _TaskRecord(
            id=task_id,
            submission=lease.submission,
            state=snapshot.state,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            attempt_count=snapshot.attempt_count,
            claimed_by=lease.owner,
            claimed_at=lease.claimed_at,
            claim_token=lease.claim_token,
            lease_expires_at=lease.lease_expires_at,
            quotes=snapshot.quotes,
            failure=snapshot.failure,
            reused_from_task_id=snapshot.reused_from_task_id,
            reuse_age_seconds=snapshot.reuse_age_seconds,
            inflight_coalesce_count=1 if snapshot.inflight_coalesced else 0,
            formal_execution_capability=lease.capability,
        )

    async def _durable_restore_claim_heartbeat(self, lease: Any) -> None:
        """Rehydrate formal attestation identity when completion lands on B."""

        assert self._durable_store is not None
        if lease.capability is None:
            return
        session = await self._durable_store.get_companion_session(lease.session_id)
        if session is None:
            raise BrowserClaimError("claimed Companion session is unavailable")
        heartbeat = _CompanionHeartbeatRecord(
            companion_id=lease.owner,
            providers=tuple(BrowserProvider(value) for value in session.providers),
            last_seen=session.last_seen_at,
            authorized_scope_keys=tuple(session.scopes),
            adapter_version=session.adapter_version,
            contract_version=session.contract_version,
            build_identity=(
                BrowserCompanionBuildIdentity.model_validate(session.build_identity)
                if session.build_identity is not None
                else None
            ),
            runtime_instance_id=session.runtime_instance_id,
        )
        self._companion_heartbeats[lease.owner] = heartbeat

    async def publish_pending_completions(self) -> int:
        """Finish prepared completions after a process crash or lease expiry."""

        if self._durable_store is None:
            return 0
        published = 0
        pending = await self._durable_store.list_pending_completions(
            tenant_id=self._durable_tenant_id
        )
        for lease, completion, snapshot, receipt, digest, event_details in pending:
            try:
                record = await self._durable_record_for(lease.public_task_id, lease)
                if record.formal_execution_capability is not None:
                    self._record_formal_completion(
                        record,
                        completion,
                        snapshot,
                        receipt,
                        event_details=event_details,
                    )
                await self._durable_store.finalize_acquisition_completion(
                    lease.acquisition_id,
                    tenant_id=self._durable_tenant_id,
                    completion_sha256=digest,
                )
                published += 1
            except (BrowserClaimError, RuntimeError):
                # A conflicting formal event or a transient DB failure leaves
                # the outbox intact for the next publisher pass.
                continue
        return published

    async def submit_many(
        self, submissions: Iterable[BrowserTaskSubmission]
    ) -> tuple[BrowserTaskSnapshot, ...]:
        values = tuple(submissions)
        if not values:
            raise ValueError("at least one browser task is required")
        capability = (
            self._source_authority.current_execution_capability()
            if self._source_authority is not None
            else None
        )
        if capability is not None:
            capability_partition = hashlib.sha256(
                json.dumps(
                    capability,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            values = tuple(
                submission.model_copy(
                    update={
                        "reuse_partition_sha256": hashlib.sha256(
                            (
                                f"{submission.reuse_partition_sha256 or ''}\0"
                                f"{capability_partition}"
                            ).encode()
                        ).hexdigest()
                    }
                )
                for submission in values
            )
        if self._durable_store is not None:
            return await self._durable_submit_many(values, capability)
        async with self._changed:
            self._housekeep_and_notify_locked()
            pending = sum(not record.state.terminal for record in self._records.values())
            projected_new = 0
            projected_keys: set[tuple[BrowserProvider, BrowserVertical, str, str]] = set()
            now = self._utc_now()
            for submission in values:
                if self._recent_reusable_record(submission, now) is not None:
                    continue
                active = self._active_reusable_record(submission)
                if active is not None:
                    continue
                key = self._singleflight_key(submission)
                if key is None or key not in projected_keys:
                    projected_new += 1
                if key is not None:
                    projected_keys.add(key)
            if pending + projected_new > self._max_pending_tasks:
                raise BrowserBridgeError("browser task queue capacity exceeded")
            snapshots: list[BrowserTaskSnapshot] = []
            for submission in values:
                now = self._utc_now()
                task_id = f"browser-task-{uuid4()}"
                reusable = self._recent_reusable_record(submission, now)
                if reusable is not None:
                    oldest_quote_age = max(
                        0.0,
                        max(
                            (now - quote.captured_at.astimezone(UTC)).total_seconds()
                            for quote in reusable.quotes
                        ),
                    )
                    record = _TaskRecord(
                        id=task_id,
                        submission=submission,
                        state=BrowserTaskState.SUCCEEDED,
                        created_at=now,
                        updated_at=now,
                        quotes=reusable.quotes,
                        reused_from_task_id=reusable.id,
                        reuse_age_seconds=oldest_quote_age,
                        formal_execution_capability=capability,
                    )
                    self._records[task_id] = record
                    snapshots.append(self._snapshot(record))
                    continue
                active = self._active_reusable_record(submission)
                if active is not None:
                    self._active_consumers[active.id] = (
                        self._active_consumers.get(active.id, 1) + 1
                    )
                    active.inflight_coalesce_count += 1
                    snapshots.append(self._snapshot(active))
                    continue
                record = _TaskRecord(
                    id=task_id,
                    submission=submission,
                    state=BrowserTaskState.QUEUED,
                    created_at=now,
                    updated_at=now,
                    formal_execution_capability=capability,
                )
                self._records[task_id] = record
                self._active_consumers[task_id] = 1
                snapshots.append(self._snapshot(record))
            self._prune_terminal_records()
            self._persist_state()
            self._changed.notify_all()
            return tuple(snapshots)

    async def formal_execution_capability(
        self,
        task_id: str,
    ) -> dict[str, object] | None:
        """Return only the signed scope attached at formal task submission."""

        if self._durable_store is not None:
            capability = await self._durable_store.get_consumer_capability(
                task_id, tenant_id=self._durable_tenant_id
            )
            return dict(capability) if capability is not None else None

        async with self._changed:
            record = self._record(task_id)
            capability = record.formal_execution_capability
            return dict(capability) if capability is not None else None

    async def source_execution_receipt(
        self,
        task_id: str,
    ) -> BrowserSourceExecutionReceipt | None:
        """Return the server-derived receipt attached to a terminal formal task."""

        if self._durable_store is not None:
            projection = await self._durable_store.get_consumer(
                task_id, tenant_id=self._durable_tenant_id
            )
            if projection is None or projection.source_receipt is None:
                return None
            return BrowserSourceExecutionReceipt.model_validate(projection.source_receipt)

        async with self._changed:
            receipt = self._record(task_id).source_execution_receipt
            return receipt.model_copy(deep=True) if receipt is not None else None

    async def claim(
        self,
        companion_id: str,
        *,
        providers: Iterable[BrowserProvider] = (),
        limit: int = 6,
    ) -> tuple[BrowserTaskLease, ...]:
        self._validate_claim_arguments(companion_id, limit)
        requested_providers = tuple(dict.fromkeys(providers))
        if self._durable_store is not None:
            async with self._changed:
                self._record_companion_heartbeat(
                    companion_id,
                    requested_providers or tuple(BrowserProvider),
                )
            return await self._durable_claim(
                companion_id,
                providers=requested_providers,
                limit=limit,
            )
        async with self._changed:
            self._housekeep_and_notify_locked()
            self._record_companion_heartbeat(
                companion_id,
                requested_providers or tuple(BrowserProvider),
            )
            leases = self._claim_leases_locked(
                companion_id,
                requested_providers,
                limit,
            )
            if leases:
                self._persist_state()
                self._changed.notify_all()
            return leases

    async def claim_response(
        self,
        companion_id: str,
        *,
        providers: Iterable[BrowserProvider] = (),
        limit: int = 6,
        authorized_scope_keys: tuple[str, ...] = (),
        adapter_version: str | None = None,
        contract_version: str | None = None,
        build_identity: BrowserCompanionBuildIdentity | None = None,
        runtime_instance_id: str | None = None,
        reload_receipt: BrowserCompanionReloadReceipt | None = None,
    ) -> ClaimBrowserTasksResponse:
        self._validate_claim_arguments(companion_id, limit)
        if (build_identity is None) != (runtime_instance_id is None):
            raise ValueError(
                "build_identity and runtime_instance_id must be supplied together"
        )
        requested_providers = tuple(dict.fromkeys(providers))
        async with self._changed:
            self._housekeep_and_notify_locked()
            if reload_receipt is not None:
                if reload_receipt.companion_id != companion_id:
                    raise BrowserCompanionControlError(
                        "reload receipt companion does not match claim companion"
                    )
                self._apply_reload_receipt_locked(reload_receipt)
            self._record_companion_heartbeat(
                companion_id,
                requested_providers or tuple(BrowserProvider),
                authorized_scope_keys=authorized_scope_keys,
                adapter_version=adapter_version,
                contract_version=contract_version,
                build_identity=build_identity,
                runtime_instance_id=runtime_instance_id,
            )
            control, control_blocks_leases = self._reload_control_locked(companion_id)
            leases = (
                ()
                if control_blocks_leases
                else (
                    await self._durable_claim(
                        companion_id,
                        providers=requested_providers,
                        authorized_scope_keys=authorized_scope_keys,
                        adapter_version=adapter_version,
                        contract_version=contract_version,
                        build_identity=build_identity,
                        runtime_instance_id=runtime_instance_id,
                        limit=limit,
                    )
                    if self._durable_store is not None
                    else self._claim_leases_locked(
                        companion_id,
                        requested_providers,
                        limit,
                    )
                )
            )
            if leases and control is not None:
                raise RuntimeError("browser leases and reload control cannot be issued together")
            self._persist_state()
            if leases or control is not None or reload_receipt is not None:
                self._changed.notify_all()
            return ClaimBrowserTasksResponse(leases=leases, control=control)

    @staticmethod
    def _validate_claim_arguments(companion_id: str, limit: int) -> None:
        if not companion_id or len(companion_id) > 128:
            raise ValueError("companion_id must contain 1 to 128 characters")
        if limit < 1 or limit > 6:
            raise ValueError("limit must be between 1 and 6")

    def _claim_leases_locked(
        self,
        companion_id: str,
        requested_providers: tuple[BrowserProvider, ...],
        limit: int,
    ) -> tuple[BrowserTaskLease, ...]:
        allowed = set(requested_providers)
        queued = [
            record
            for record in self._records.values()
            if record.state == BrowserTaskState.QUEUED
            and (not allowed or record.submission.provider in allowed)
        ]
        queued.sort(key=lambda record: (record.created_at, record.id))
        active_qunar_lodging = sum(
            record.state == BrowserTaskState.CLAIMED
            and record.submission.provider == BrowserProvider.QUNAR
            and record.submission.kind == BrowserVertical.LODGING
            for record in self._records.values()
        )
        selected = self._fair_provider_batch(
            queued,
            limit,
            qunar_lodging_slots=max(0, 1 - active_qunar_lodging),
        )
        leases: list[BrowserTaskLease] = []
        for record in selected:
            now = self._utc_now()
            token = secrets.token_urlsafe(32)
            expires_at = now + timedelta(seconds=record.submission.timeout_seconds)
            record.state = BrowserTaskState.CLAIMED
            record.updated_at = now
            record.attempt_count += 1
            record.claimed_by = companion_id
            record.claimed_at = now
            record.claim_token = token
            record.lease_expires_at = expires_at
            leases.append(
                BrowserTaskLease(
                    task_id=record.id,
                    provider=record.submission.provider,
                    kind=record.submission.kind,
                    query=record.submission.query,
                    timeout_seconds=record.submission.timeout_seconds,
                    claim_token=token,
                    claimed_at=now,
                    lease_expires_at=expires_at,
                )
            )
        return tuple(leases)

    async def companion_status(self) -> BrowserCompanionStatusResponse:
        if self._durable_store is not None:
            now = self._utc_now()
            sessions = await self._durable_store.list_companion_sessions()
            companions = tuple(
                BrowserCompanionHeartbeat(
                    companion_id=row.companion_id,
                    providers=tuple(BrowserProvider(value) for value in row.providers),
                    last_seen=_aware(row.last_seen_at),
                    age_seconds=max(0.0, (now - _aware(row.last_seen_at)).total_seconds()),
                    is_fresh=(
                        now - _aware(row.last_seen_at)
                        <= timedelta(seconds=COMPANION_HEARTBEAT_STALE_AFTER_SECONDS)
                    ),
                    authorized_scope_keys=tuple(row.scopes),
                    adapter_version=row.adapter_version,
                    contract_version=row.contract_version,
                    build_identity=(
                        BrowserCompanionBuildIdentity.model_validate(row.build_identity)
                        if row.build_identity is not None
                        else None
                    ),
                    runtime_instance_id=row.runtime_instance_id,
                )
                for row in sessions
            )
            return BrowserCompanionStatusResponse(
                status="connected" if any(item.is_fresh for item in companions) else "disconnected",
                server_time=now,
                stale_after_seconds=COMPANION_HEARTBEAT_STALE_AFTER_SECONDS,
                companions=companions,
            )
        async with self._changed:
            now = self._utc_now()
            companions = tuple(
                self._heartbeat_snapshot(record, now)
                for record in sorted(
                    self._companion_heartbeats.values(),
                    key=lambda item: (-item.last_seen.timestamp(), item.companion_id),
                )
            )
            return BrowserCompanionStatusResponse(
                status=(
                    "connected"
                    if any(companion.is_fresh for companion in companions)
                    else "disconnected"
                ),
                server_time=now,
                stale_after_seconds=COMPANION_HEARTBEAT_STALE_AFTER_SECONDS,
                companions=companions,
            )

    async def heartbeat(
        self,
        companion_id: str,
        *,
        providers: Iterable[BrowserProvider],
        authorized_scope_keys: tuple[str, ...] = (),
        adapter_version: str | None = None,
        contract_version: str | None = None,
        build_identity: BrowserCompanionBuildIdentity | None = None,
        runtime_instance_id: str | None = None,
    ) -> BrowserCompanionHeartbeat:
        requested_providers = tuple(dict.fromkeys(providers))
        if not companion_id or len(companion_id) > 128:
            raise ValueError("companion_id must contain 1 to 128 characters")
        if not requested_providers:
            raise ValueError("heartbeat requires at least one provider")
        if self._durable_store is not None:
            async with self._changed:
                self._record_companion_heartbeat(
                    companion_id,
                    requested_providers,
                    authorized_scope_keys=authorized_scope_keys,
                    adapter_version=adapter_version,
                    contract_version=contract_version,
                    build_identity=build_identity,
                    runtime_instance_id=runtime_instance_id,
                )
                record = self._companion_heartbeats[companion_id]
                response = self._heartbeat_snapshot(record, self._utc_now())
            session = await self._durable_session(
                companion_id,
                providers=requested_providers,
                scopes=authorized_scope_keys,
                adapter_version=adapter_version,
                contract_version=contract_version,
                build_identity=build_identity,
                runtime_instance_id=runtime_instance_id,
            )
            await self._durable_store.renew_session_leases(
                session_id=session.id,
                session_generation=session.session_generation,
                runtime_instance_id=runtime_instance_id,
                build_identity=(
                    build_identity.model_dump(mode="json")
                    if build_identity is not None
                    else None
                ),
            )
            return response
        async with self._changed:
            self._record_companion_heartbeat(
                companion_id,
                requested_providers,
                authorized_scope_keys=authorized_scope_keys,
                adapter_version=adapter_version,
                contract_version=contract_version,
                build_identity=build_identity,
                runtime_instance_id=runtime_instance_id,
            )
            record = self._companion_heartbeats[companion_id]
            return self._heartbeat_snapshot(record, self._utc_now())

    async def request_reload(
        self,
        companion_id: str,
        *,
        idempotency_key: str,
        request: BrowserCompanionReloadRequestBody,
    ) -> BrowserCompanionReloadRequestSnapshot:
        self._validate_claim_arguments(companion_id, 1)
        self._validate_idempotency_key(idempotency_key)
        fingerprint = self._reload_request_fingerprint(request)
        async with self._changed:
            self._housekeep_and_notify_locked()
            same_key = next(
                (
                    record
                    for record in self._reload_requests.values()
                    if record.companion_id == companion_id
                    and hmac.compare_digest(record.idempotency_key, idempotency_key)
                ),
                None,
            )
            if same_key is not None:
                if not hmac.compare_digest(
                    same_key.request_fingerprint_sha256,
                    fingerprint,
                ):
                    raise BrowserCompanionControlError(
                        "idempotency key was already used with a different reload request"
                    )
                return self._reload_snapshot(same_key)
            pending = next(
                (
                    record
                    for record in self._reload_requests.values()
                    if record.companion_id == companion_id and not record.state.terminal
                ),
                None,
            )
            if pending is not None:
                raise BrowserCompanionControlError(
                    "companion already has a non-terminal reload request"
                )
            heartbeat_record = self._companion_heartbeats.get(companion_id)
            if heartbeat_record is None:
                raise BrowserCompanionControlError(
                    "companion has not reported a control-capable runtime identity"
                )
            heartbeat = self._heartbeat_snapshot(heartbeat_record, self._utc_now())
            if (
                not heartbeat.is_fresh
                or heartbeat.build_identity is None
                or heartbeat.runtime_instance_id is None
            ):
                raise BrowserCompanionControlError(
                    "companion runtime identity is absent or stale"
                )
            if not hmac.compare_digest(
                heartbeat.build_identity.build_sha256,
                request.expected_current_build_sha256,
            ):
                raise BrowserCompanionControlError(
                    "expected current build does not match the connected companion"
                )
            if len(self._reload_requests) >= self._max_companion_control_records:
                self._prune_terminal_reload_requests()
            if len(self._reload_requests) >= self._max_companion_control_records:
                raise BrowserCompanionControlError(
                    "browser companion control record capacity exceeded"
                )
            now = self._utc_now()
            active = self._active_claims_for_companion(companion_id)
            record = _CompanionReloadRecord(
                id=f"companion-reload-{secrets.token_urlsafe(16)}",
                companion_id=companion_id,
                idempotency_key=idempotency_key,
                request=request,
                request_fingerprint_sha256=fingerprint,
                state=(
                    BrowserCompanionControlState.DRAINING
                    if active
                    else BrowserCompanionControlState.QUEUED
                ),
                requested_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=request.expires_in_seconds),
                drain_deadline_at=now + timedelta(seconds=request.max_drain_seconds),
                expected_runtime_instance_id=heartbeat.runtime_instance_id,
            )
            self._reload_requests[record.id] = record
            self._persist_state()
            self._changed.notify_all()
            return self._reload_snapshot(record)

    async def get_reload_request(
        self,
        companion_id: str,
        request_id: str,
    ) -> BrowserCompanionReloadRequestSnapshot:
        async with self._changed:
            self._housekeep_and_notify_locked()
            record = self._reload_requests.get(request_id)
            if record is None or record.companion_id != companion_id:
                raise BrowserCompanionReloadNotFoundError(
                    f"browser companion reload request not found: {request_id}"
                )
            return self._reload_snapshot(record)

    async def record_reload_receipt(
        self,
        receipt: BrowserCompanionReloadReceipt,
    ) -> BrowserCompanionReloadRequestSnapshot:
        async with self._changed:
            self._housekeep_and_notify_locked()
            record = self._apply_reload_receipt_locked(receipt)
            heartbeat = self._companion_heartbeats.get(receipt.companion_id)
            providers = heartbeat.providers if heartbeat is not None else tuple(BrowserProvider)
            self._record_companion_heartbeat(
                receipt.companion_id,
                providers,
                build_identity=receipt.build_identity,
                runtime_instance_id=receipt.runtime_instance_id,
            )
            self._persist_state()
            self._changed.notify_all()
            return self._reload_snapshot(record)

    async def complete(
        self,
        task_id: str,
        claim_token: str,
        completion: BrowserTaskCompletion,
        source_execution_attestation: BrowserSourceExecutionAttestation | None = None,
    ) -> BrowserTaskSnapshot:
        if self._durable_store is not None:
            lease = self._durable_claims.get(task_id)
            if lease is None:
                lease = await self._durable_store.get_claim_lease(
                    task_id,
                    tenant_id=self._durable_tenant_id,
                    claim_token=claim_token,
                )
            if lease is None:
                raise BrowserClaimError("task does not have an active claim")
            record = await self._durable_record_for(task_id, lease)
            if record.claim_token is None or not hmac.compare_digest(
                record.claim_token, claim_token
            ):
                raise BrowserClaimError("claim token does not match the task lease")
            self._validate_completion(record.submission, completion)
            pending = await self._durable_store.get_pending_completion(
                lease.acquisition_id,
                tenant_id=self._durable_tenant_id,
            )
            if pending is not None:
                pending_completion, frozen_snapshot, pending_receipt, _pending_digest = pending
                if (
                    pending_completion != completion
                    or (
                        source_execution_attestation is not None
                        and pending_receipt is not None
                        and self._validate_source_execution_attestation(
                            record,
                            completion,
                            source_execution_attestation,
                            now=self._utc_now(),
                        )
                        != pending_receipt
                    )
                ):
                    raise BrowserClaimError("completion retry differs")
                # The DB outbox is the source of truth after prepare.  The
                # old Companion session/lease may have expired by recovery.
                source_receipt = pending_receipt
                event_details = await self._durable_store.get_pending_completion_event_details(
                    lease.acquisition_id,
                    tenant_id=self._durable_tenant_id,
                )
            else:
                if record.lease_expires_at is None or record.lease_expires_at <= self._utc_now():
                    raise BrowserClaimError("task lease has expired")
                await self._durable_restore_claim_heartbeat(lease)
                source_receipt = self._validate_source_execution_attestation(
                    record,
                    completion,
                    source_execution_attestation,
                    now=self._utc_now(),
                )
                frozen_snapshot = None
            now = self._utc_now()
            previous = (
                record.state,
                record.updated_at,
                record.quotes,
                record.failure,
                record.source_execution_receipt,
            )
            record.state = completion.state
            record.updated_at = now
            record.quotes = completion.quotes
            record.failure = completion.failure
            record.source_execution_receipt = source_receipt
            if frozen_snapshot is None:
                # Freeze the terminal public result exactly once.  In
                # particular, do not hash the pre-completion CLAIMED snapshot.
                frozen_snapshot = self._snapshot(record)
                event_details = self._formal_completion_details(
                    record, completion, frozen_snapshot, source_receipt
                )
            elif event_details is None:
                event_details = self._formal_completion_details(
                    record, completion, frozen_snapshot, source_receipt
                )
            try:
                completion_digest = await self._durable_store.prepare_acquisition_completion(
                    lease.acquisition_id,
                    tenant_id=self._durable_tenant_id,
                    owner=lease.owner,
                    generation=lease.generation,
                    claim_token=claim_token,
                    session_id=lease.session_id,
                    session_generation=lease.session_generation,
                    completion=completion,
                    completion_snapshot=(
                        frozen_snapshot
                        if frozen_snapshot is not None
                        else self._snapshot(record)
                    ),
                    source_receipt=source_receipt,
                    event_details=event_details,
                    runtime_instance_id=lease.runtime_instance_id,
                    build_identity=lease.build_identity,
                )
                self._record_formal_completion(
                    record,
                    completion,
                    frozen_snapshot if frozen_snapshot is not None else self._snapshot(record),
                    source_receipt,
                    event_details=event_details,
                )
                projection = await self._durable_store.finalize_acquisition_completion(
                    lease.acquisition_id,
                    tenant_id=self._durable_tenant_id,
                    completion_sha256=completion_digest,
                )
            except RuntimeError as exc:
                (
                    record.state,
                    record.updated_at,
                    record.quotes,
                    record.failure,
                    record.source_execution_receipt,
                ) = previous
                raise BrowserClaimError(str(exc)) from exc
            self._durable_claims.pop(task_id, None)
            return cast(BrowserTaskSnapshot, projection)
        async with self._changed:
            self._housekeep_and_notify_locked()
            record = self._record(task_id)
            if record.state != BrowserTaskState.CLAIMED or record.claim_token is None:
                raise BrowserClaimError("task does not have an active claim")
            if not hmac.compare_digest(record.claim_token, claim_token):
                raise BrowserClaimError("claim token does not match the task lease")
            if record.lease_expires_at is None or record.lease_expires_at <= self._utc_now():
                raise BrowserClaimError("task lease has expired")
            self._validate_completion(record.submission, completion)
            now = self._utc_now()
            source_receipt = self._validate_source_execution_attestation(
                record,
                completion,
                source_execution_attestation,
                now=now,
            )
            previous_terminal_fields = (
                record.state,
                record.updated_at,
                record.quotes,
                record.failure,
                record.source_execution_receipt,
            )
            record.state = completion.state
            record.updated_at = now
            record.quotes = completion.quotes
            record.failure = completion.failure
            record.source_execution_receipt = source_receipt
            snapshot = self._snapshot(record)
            try:
                self._record_formal_completion(
                    record,
                    completion,
                    snapshot,
                    source_receipt,
                )
            except BaseException:
                (
                    record.state,
                    record.updated_at,
                    record.quotes,
                    record.failure,
                    record.source_execution_receipt,
                ) = previous_terminal_fields
                raise
            record.claim_token = None
            record.lease_expires_at = None
            if not self._terminal_record_has_pending_ledger_waiter(record):
                self._active_consumers.pop(record.id, None)
            self._prune_terminal_records()
            self._persist_state()
            self._changed.notify_all()
            return snapshot

    def _record_formal_completion(
        self,
        record: _TaskRecord,
        completion: BrowserTaskCompletion,
        snapshot: BrowserTaskSnapshot,
        source_receipt: BrowserSourceExecutionReceipt | None,
        event_details: dict[str, Any] | None = None,
    ) -> None:
        capability = record.formal_execution_capability
        authority = self._source_authority
        if authority is None or capability is None:
            return
        if source_receipt is None:
            raise BrowserClaimError("formal task has no source execution receipt")
        details = event_details or self._formal_completion_details(
            record, completion, snapshot, source_receipt
        )
        if details is None:
            raise BrowserClaimError("formal completion details are unavailable")
        try:
            with authority.execution_scope(capability):
                ensure = getattr(authority, "ensure_browser_complete", None)
                if ensure is not None:
                    ensure(subject_id=record.id, details=details)
                else:
                    # Keep lightweight test/legacy authorities compatible;
                    # production FormalLiveSourceAuthority always exposes the
                    # idempotent ensure path.
                    authority.record_browser_http(
                        "browser_complete",
                        subject_ids=(record.id,),
                        details=details,
                    )
        except ValueError as exc:
            raise BrowserClaimError(str(exc)) from exc

    def _formal_completion_details(
        self,
        record: _TaskRecord,
        completion: BrowserTaskCompletion,
        snapshot: BrowserTaskSnapshot,
        source_receipt: BrowserSourceExecutionReceipt | None,
    ) -> dict[str, Any] | None:
        capability = record.formal_execution_capability
        authority = self._source_authority
        if authority is None or capability is None:
            return None
        if source_receipt is None:
            raise BrowserClaimError("formal task has no source execution receipt")
        snapshot_payload = snapshot.model_dump(mode="json")
        return {
            "task_id": record.id,
            "completion": completion.model_dump(mode="json"),
            "source_execution_receipt": source_receipt.model_dump(mode="json"),
            "snapshot": snapshot_payload,
            "formal_query": authority.formal_browser_query(
                task_id=snapshot.id,
                provider=snapshot.provider.value,
                kind=snapshot.kind.value,
                query=snapshot.query.model_dump(mode="json"),
            ),
            "result_sha256": _canonical_json_sha256(snapshot_payload),
        }

    def _validate_source_execution_attestation(
        self,
        record: _TaskRecord,
        completion: BrowserTaskCompletion,
        attestation: BrowserSourceExecutionAttestation | None,
        *,
        now: datetime,
    ) -> BrowserSourceExecutionReceipt | None:
        capability = record.formal_execution_capability
        if attestation is None:
            if capability is not None:
                raise BrowserClaimError(
                    "formal task requires a source execution attestation"
                )
            return None
        if record.claimed_by is None or record.claimed_at is None:
            raise BrowserClaimError("source execution attestation has no active claim")
        heartbeat = self._companion_heartbeats.get(record.claimed_by)
        if (
            heartbeat is None
            or heartbeat.build_identity is None
            or heartbeat.runtime_instance_id is None
            or now - heartbeat.last_seen
            > timedelta(seconds=COMPANION_HEARTBEAT_STALE_AFTER_SECONDS)
        ):
            raise BrowserClaimError(
                "source execution attestation has no fresh Companion runtime identity"
            )
        # The claim endpoint omits ``None`` fields from its response.  Hash the
        # exact query shape delivered to Companion so a valid completion is not
        # rejected merely because optional fields were absent on the wire.
        query_payload = record.submission.query.model_dump(
            mode="json",
            exclude_none=True,
        )
        query_sha256 = _canonical_json_sha256(query_payload)
        observation_payload = {
            "task_id": record.id,
            "provider": record.submission.provider.value,
            "kind": record.submission.kind.value,
            "query": query_payload,
            "quote_evidence_sha256": [
                quote.evidence_sha256 for quote in completion.quotes
            ],
            "parser_version": attestation.parser_version,
        }
        expected_observation_sha256 = _canonical_json_sha256(observation_payload)
        mismatch_fields = tuple(
            field
            for field, differs in (
                ("task_id", attestation.task_id != record.id),
                ("provider", attestation.provider != record.submission.provider),
                ("kind", attestation.kind != record.submission.kind),
                ("claimed_companion", attestation.companion_id != record.claimed_by),
                ("heartbeat_companion", attestation.companion_id != heartbeat.companion_id),
                (
                    "runtime_instance_id",
                    attestation.runtime_instance_id != heartbeat.runtime_instance_id,
                ),
                ("build_identity", attestation.build_identity != heartbeat.build_identity),
                (
                    "parser_version",
                    attestation.parser_version != PRODUCTION_VISIBLE_DOM_PARSER_VERSION,
                ),
                ("query_sha256", attestation.query_sha256 != query_sha256),
                (
                    "source_observation_sha256",
                    attestation.source_observation_sha256
                    != expected_observation_sha256,
                ),
            )
            if differs
        )
        if mismatch_fields:
            raise BrowserClaimError(
                "source execution attestation differs from the claimed task/runtime: "
                + ", ".join(mismatch_fields)
            )
        if any(
            quote.parser_version != attestation.parser_version
            for quote in completion.quotes
        ):
            raise BrowserClaimError(
                "source execution attestation parser differs from its quotes"
            )
        completed_at = attestation.completed_at.astimezone(UTC)
        if (
            completed_at < record.claimed_at.astimezone(UTC)
            or completed_at > now.astimezone(UTC) + timedelta(seconds=1)
        ):
            raise BrowserClaimError(
                "source execution attestation timestamp is outside its claim"
            )
        if capability is None:
            return None
        if (
            attestation.execution_environment
            != PRODUCTION_SOURCE_EXECUTION_ENVIRONMENT
        ):
            raise BrowserClaimError(
                "formal task requires the production extension execution environment"
            )
        required_capability_fields = {
            "capability_id",
            "challenge_id",
            "run_id",
            "terminal_job_id",
            "request_sha256",
            "job_graph_sha256",
            "attempt_digest",
        }
        if any(
            not isinstance(capability.get(field), str)
            or not capability.get(field)
            for field in required_capability_fields
        ):
            raise BrowserClaimError(
                "source execution attestation has an invalid formal capability"
            )
        serialized_attestation = attestation.model_dump(mode="json")
        unsigned = {
            "schema_version": SOURCE_EXECUTION_RECEIPT_SCHEMA,
            "task_id": record.id,
            "provider": record.submission.provider.value,
            "kind": record.submission.kind.value,
            "companion_id": heartbeat.companion_id,
            "runtime_instance_id": heartbeat.runtime_instance_id,
            "build_identity": heartbeat.build_identity.model_dump(mode="json"),
            "execution_environment": attestation.execution_environment,
            "parser_version": attestation.parser_version,
            "query_sha256": query_sha256,
            "source_observation_sha256": expected_observation_sha256,
            "completion_sha256": _canonical_json_sha256(
                completion.model_dump(mode="json")
            ),
            **{
                field: capability[field]
                for field in sorted(required_capability_fields)
            },
            "completed_at": serialized_attestation["completed_at"],
        }
        return BrowserSourceExecutionReceipt.model_validate(
            {
                **unsigned,
                "receipt_sha256": _canonical_json_sha256(unsigned),
            }
        )

    async def get(self, task_id: str) -> BrowserTaskSnapshot:
        if self._durable_store is not None:
            await self.publish_pending_completions()
            projection = await self._durable_store.get_consumer(
                task_id, tenant_id=self._durable_tenant_id
            )
            if projection is None:
                raise BrowserTaskNotFoundError(f"browser task not found: {task_id}")
            return cast(BrowserTaskSnapshot, projection.snapshot)
        async with self._changed:
            self._housekeep_and_notify_locked()
            return self._snapshot(self._record(task_id))

    async def cancel_many(
        self,
        task_ids: Iterable[str],
        *,
        reason: str,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return ()
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 1000:
            raise ValueError("cancellation reason must contain 1 to 1000 characters")
        if self._durable_store is not None:
            snapshots: list[BrowserTaskSnapshot] = []
            for task_id in ids:
                projection = await self._durable_store.cancel_consumer(
                    task_id,
                    tenant_id=self._durable_tenant_id,
                )
                if projection is None:
                    raise BrowserTaskNotFoundError(f"browser task not found: {task_id}")
                snapshots.append(projection.snapshot)
                self._durable_claims.pop(task_id, None)
            return tuple(snapshots)
        async with self._changed:
            self._housekeep_and_notify_locked()
            records = tuple(self._record(task_id) for task_id in ids)
            changed = False
            retention_released = False
            for record in records:
                if record.state.terminal:
                    retention_released = (
                        self._release_terminal_waiter_locked(record)
                        or retention_released
                    )
                    continue
                consumers = self._active_consumers.get(record.id, 1)
                if consumers > 1:
                    self._active_consumers[record.id] = consumers - 1
                    continue
                previous_state = record.state
                now = self._utc_now()
                record.state = BrowserTaskState.CANCELLED
                record.updated_at = now
                record.claim_token = None
                record.lease_expires_at = None
                record.quotes = ()
                record.failure = BrowserFailure(
                    code=BrowserFailureCode.CANCELLED,
                    message=normalized_reason,
                    retryable=False,
                    captured_at=now,
                    details={"previous_state": previous_state.value},
                )
                self._active_consumers.pop(record.id, None)
                changed = True
            if changed or retention_released:
                self._prune_terminal_records()
                self._persist_state()
                self._changed.notify_all()
            return tuple(self._snapshot(record) for record in records)

    async def wait_many(
        self,
        task_ids: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> tuple[BrowserTaskSnapshot, ...]:
        ids = tuple(dict.fromkeys(task_ids))
        if not ids:
            return ()
        if self._durable_store is not None:
            await self.publish_pending_completions()
            projections = await asyncio.gather(
                *(
                    self._durable_store.wait_consumer(
                        task_id,
                        tenant_id=self._durable_tenant_id,
                        timeout_seconds=timeout_seconds,
                    )
                    for task_id in ids
                )
            )
            if any(projection is None for projection in projections):
                raise BrowserTaskNotFoundError("browser task not found")
            return tuple(projection.snapshot for projection in projections if projection)

        async def wait_for_terminal() -> tuple[BrowserTaskSnapshot, ...]:
            async with self._changed:
                while True:
                    self._housekeep_and_notify_locked()
                    records = tuple(self._record(task_id) for task_id in ids)
                    if all(record.state.terminal for record in records):
                        snapshots = tuple(self._snapshot(record) for record in records)
                        for record in records:
                            self._release_terminal_waiter_locked(record)
                        self._prune_terminal_records()
                        return snapshots
                    lease_expiries = tuple(
                        record.lease_expires_at
                        for record in records
                        if record.state == BrowserTaskState.CLAIMED
                        and record.lease_expires_at is not None
                    )
                    if not lease_expiries:
                        await self._changed.wait()
                        continue
                    seconds_until_expiry = max(
                        0.0,
                        (min(lease_expiries).astimezone(UTC) - self._utc_now()).total_seconds(),
                    )
                    if seconds_until_expiry == 0:
                        continue
                    try:
                        async with asyncio.timeout(seconds_until_expiry):
                            await self._changed.wait()
                    except TimeoutError:
                        # Lease expiry is itself a state transition boundary. Wake
                        # even when the Companion has stopped polling so the next
                        # loop can requeue or terminalize the task via housekeeping.
                        continue

        async with asyncio.timeout(timeout_seconds):
            return await wait_for_terminal()

    def _release_terminal_waiter_locked(self, record: _TaskRecord) -> bool:
        if (
            record.submission.query.options.get(_LEDGER_TERMINAL_RETENTION_OPTION)
            is not True
        ):
            return False
        consumers = self._active_consumers.get(record.id, 0)
        if consumers == 0:
            return False
        if consumers <= 1:
            self._active_consumers.pop(record.id, None)
        else:
            self._active_consumers[record.id] = consumers - 1
        return True

    def _record(self, task_id: str) -> _TaskRecord:
        record = self._records.get(task_id)
        if record is None:
            raise BrowserTaskNotFoundError(f"browser task not found: {task_id}")
        return record

    def _recent_reusable_record(
        self,
        submission: BrowserTaskSubmission,
        now: datetime,
    ) -> _TaskRecord | None:
        if submission.query.options.get("__tripchord_allow_recent_quote_reuse") is not True:
            return None
        if submission.reuse_partition_sha256 is None:
            return None
        fingerprint = self._reuse_fingerprint(submission.query)
        candidates = sorted(
            self._records.values(),
            key=lambda record: (record.updated_at, record.id),
            reverse=True,
        )
        for record in candidates:
            if (
                record.submission.provider != submission.provider
                or record.submission.kind != submission.kind
                or record.state != BrowserTaskState.SUCCEEDED
                or not record.quotes
                or record.submission.reuse_partition_sha256 is None
                or not hmac.compare_digest(
                    record.submission.reuse_partition_sha256,
                    submission.reuse_partition_sha256,
                )
                or self._reuse_fingerprint(record.submission.query) != fingerprint
            ):
                continue
            ages = tuple(
                (now - quote.captured_at.astimezone(UTC)).total_seconds() for quote in record.quotes
            )
            # Match NormalizedQuote.is_fresh's half-open [captured_at,
            # expires_at) window: a quote is expired at exactly 600 seconds.
            if ages and all(0 <= age < RECENT_EXACT_QUOTE_REUSE_SECONDS for age in ages):
                return record
        return None

    def _active_reusable_record(
        self,
        submission: BrowserTaskSubmission,
    ) -> _TaskRecord | None:
        key = self._singleflight_key(submission)
        if key is None:
            return None
        candidates = sorted(
            self._records.values(),
            key=lambda record: (record.created_at, record.id),
        )
        for record in candidates:
            if record.state.terminal:
                continue
            if self._singleflight_key(record.submission) == key:
                return record
        return None

    def _singleflight_key(
        self,
        submission: BrowserTaskSubmission,
    ) -> tuple[BrowserProvider, BrowserVertical, str, str] | None:
        if submission.query.options.get("__tripchord_allow_recent_quote_reuse") is not True:
            return None
        partition = submission.reuse_partition_sha256
        if partition is None:
            return None
        return (
            submission.provider,
            submission.kind,
            partition,
            self._reuse_fingerprint(submission.query),
        )

    @staticmethod
    def _reuse_fingerprint(query: BrowserSearchQuery) -> str:
        payload = query.model_dump(mode="json")
        options = dict(payload.get("options") or {})
        payload["options"] = {
            key: value for key, value in options.items() if not key.startswith("__tripchord_")
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def _record_companion_heartbeat(
        self,
        companion_id: str,
        providers: tuple[BrowserProvider, ...],
        *,
        authorized_scope_keys: tuple[str, ...] = (),
        adapter_version: str | None = None,
        contract_version: str | None = None,
        build_identity: BrowserCompanionBuildIdentity | None = None,
        runtime_instance_id: str | None = None,
    ) -> None:
        previous = self._companion_heartbeats.get(companion_id)
        self._companion_heartbeats[companion_id] = _CompanionHeartbeatRecord(
            companion_id=companion_id,
            providers=providers,
            last_seen=self._utc_now(),
            authorized_scope_keys=(
                authorized_scope_keys
                if authorized_scope_keys
                else previous.authorized_scope_keys if previous is not None else ()
            ),
            adapter_version=(
                adapter_version
                if adapter_version is not None
                else previous.adapter_version if previous is not None else None
            ),
            contract_version=(
                contract_version
                if contract_version is not None
                else previous.contract_version if previous is not None else None
            ),
            build_identity=(
                build_identity
                if build_identity is not None
                else previous.build_identity if previous is not None else None
            ),
            runtime_instance_id=(
                runtime_instance_id
                if runtime_instance_id is not None
                else previous.runtime_instance_id if previous is not None else None
            ),
        )

    def _heartbeat_snapshot(
        self,
        record: _CompanionHeartbeatRecord,
        now: datetime,
    ) -> BrowserCompanionHeartbeat:
        age_seconds = max(0.0, (now - record.last_seen).total_seconds())
        return BrowserCompanionHeartbeat(
            companion_id=record.companion_id,
            providers=record.providers,
            last_seen=record.last_seen,
            age_seconds=age_seconds,
            is_fresh=age_seconds <= COMPANION_HEARTBEAT_STALE_AFTER_SECONDS,
            authorized_scope_keys=record.authorized_scope_keys,
            adapter_version=record.adapter_version,
            contract_version=record.contract_version,
            build_identity=record.build_identity,
            runtime_instance_id=record.runtime_instance_id,
        )

    @staticmethod
    def _validate_idempotency_key(value: str) -> None:
        safe_characters = (
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789._:-"
        )
        if (
            len(value) < 8
            or len(value) > 128
            or any(character not in safe_characters for character in value)
        ):
            raise ValueError(
                "idempotency key must contain 8 to 128 safe ASCII characters"
            )

    @staticmethod
    def _reload_request_fingerprint(
        request: BrowserCompanionReloadRequestBody,
    ) -> str:
        return hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    def _active_claims_for_companion(self, companion_id: str) -> tuple[str, ...]:
        return tuple(
            record.id
            for record in self._records.values()
            if record.state == BrowserTaskState.CLAIMED
            and record.claimed_by == companion_id
        )

    def _reload_control_locked(
        self,
        companion_id: str,
    ) -> tuple[BrowserCompanionReloadControl | None, bool]:
        record = next(
            (
                candidate
                for candidate in sorted(
                    self._reload_requests.values(),
                    key=lambda item: (item.requested_at, item.id),
                )
                if candidate.companion_id == companion_id
                and not candidate.state.terminal
            ),
            None,
        )
        if record is None:
            return None, False
        now = self._utc_now()
        if self._active_claims_for_companion(companion_id):
            if record.state in {
                BrowserCompanionControlState.QUEUED,
                BrowserCompanionControlState.DRAINING,
            }:
                record.state = BrowserCompanionControlState.DRAINING
                record.updated_at = now
            return None, True
        if record.state not in {
            BrowserCompanionControlState.QUEUED,
            BrowserCompanionControlState.DRAINING,
        }:
            return None, True
        heartbeat = self._companion_heartbeats.get(companion_id)
        if (
            heartbeat is None
            or heartbeat.build_identity is None
            or heartbeat.runtime_instance_id is None
            or not hmac.compare_digest(
                heartbeat.build_identity.build_sha256,
                record.request.expected_current_build_sha256,
            )
            or not hmac.compare_digest(
                heartbeat.runtime_instance_id,
                record.expected_runtime_instance_id,
            )
        ):
            record.state = BrowserCompanionControlState.FAILED
            record.updated_at = now
            record.failure_code = "runtime_changed_before_dispatch"
            return None, False
        receipt_token = secrets.token_urlsafe(32)
        record.delivery_generation += 1
        record.receipt_token_sha256 = hashlib.sha256(
            receipt_token.encode("utf-8")
        ).hexdigest()
        record.state = BrowserCompanionControlState.DISPATCHED
        record.updated_at = now
        return (
            BrowserCompanionReloadControl(
                request_id=record.id,
                target_build_sha256=record.request.target_build_sha256,
                expected_runtime_instance_id=record.expected_runtime_instance_id,
                delivery_generation=record.delivery_generation,
                receipt_token=receipt_token,
                expires_at=record.expires_at,
            ),
            True,
        )

    def _apply_reload_receipt_locked(
        self,
        receipt: BrowserCompanionReloadReceipt,
    ) -> _CompanionReloadRecord:
        record = self._reload_requests.get(receipt.request_id)
        if record is None or record.companion_id != receipt.companion_id:
            raise BrowserCompanionReloadNotFoundError(
                f"browser companion reload request not found: {receipt.request_id}"
            )
        if record.receipt_token_sha256 is None:
            raise BrowserCompanionControlError("reload request has no active receipt token")
        supplied_sha256 = hashlib.sha256(receipt.receipt_token.encode("utf-8")).hexdigest()
        if (
            receipt.delivery_generation != record.delivery_generation
            or not hmac.compare_digest(record.receipt_token_sha256, supplied_sha256)
        ):
            raise BrowserCompanionControlError(
                "reload receipt token or delivery generation does not match"
            )
        if (
            receipt.previous_runtime_instance_id is not None
            and not hmac.compare_digest(
                receipt.previous_runtime_instance_id,
                record.expected_runtime_instance_id,
            )
        ):
            raise BrowserCompanionControlError(
                "reload receipt previous runtime does not match the dispatched runtime"
            )
        now = self._utc_now()
        if record.state == BrowserCompanionControlState.EXPIRED or now >= record.expires_at:
            record.state = BrowserCompanionControlState.EXPIRED
            record.updated_at = now
            record.failure_code = "reload_request_expired"
            raise BrowserCompanionControlError("reload request has expired")
        if receipt.state == BrowserCompanionReloadReceiptState.ACCEPTED:
            if record.state == BrowserCompanionControlState.ACCEPTED:
                return record
            if record.state != BrowserCompanionControlState.DISPATCHED:
                raise BrowserCompanionControlError(
                    "accepted receipt requires a dispatched reload request"
                )
            if (
                not hmac.compare_digest(
                    receipt.runtime_instance_id,
                    record.expected_runtime_instance_id,
                )
                or not hmac.compare_digest(
                    receipt.build_identity.build_sha256,
                    record.request.expected_current_build_sha256,
                )
            ):
                raise BrowserCompanionControlError(
                    "accepted receipt must come from the dispatched old runtime and build"
                )
            record.state = BrowserCompanionControlState.ACCEPTED
            record.accepted_at = now
            record.updated_at = now
            return record
        if receipt.state == BrowserCompanionReloadReceiptState.APPLIED:
            if record.state == BrowserCompanionControlState.APPLIED:
                if (
                    record.observed_runtime_instance_id == receipt.runtime_instance_id
                    and record.observed_build_sha256 == receipt.build_identity.build_sha256
                ):
                    return record
                raise BrowserCompanionControlError(
                    "applied reload receipt conflicts with the terminal observation"
                )
            if record.state != BrowserCompanionControlState.ACCEPTED:
                raise BrowserCompanionControlError(
                    "applied receipt requires an accepted reload request"
                )
            if hmac.compare_digest(
                receipt.runtime_instance_id,
                record.expected_runtime_instance_id,
            ):
                raise BrowserCompanionControlError(
                    "applied receipt must come from a new runtime instance"
                )
            if not hmac.compare_digest(
                receipt.build_identity.build_sha256,
                record.request.target_build_sha256,
            ):
                raise BrowserCompanionControlError(
                    "applied receipt build does not match the reload target"
                )
            record.state = BrowserCompanionControlState.APPLIED
            record.applied_at = now
            record.updated_at = now
            record.observed_build_sha256 = receipt.build_identity.build_sha256
            record.observed_runtime_instance_id = receipt.runtime_instance_id
            return record
        if record.state == BrowserCompanionControlState.FAILED:
            return record
        if record.state not in {
            BrowserCompanionControlState.DISPATCHED,
            BrowserCompanionControlState.ACCEPTED,
        }:
            raise BrowserCompanionControlError(
                "failed receipt requires a dispatched or accepted reload request"
            )
        record.state = BrowserCompanionControlState.FAILED
        record.updated_at = now
        record.failure_code = receipt.failure_code
        record.observed_build_sha256 = receipt.build_identity.build_sha256
        record.observed_runtime_instance_id = receipt.runtime_instance_id
        return record

    def _reload_snapshot(
        self,
        record: _CompanionReloadRecord,
    ) -> BrowserCompanionReloadRequestSnapshot:
        return BrowserCompanionReloadRequestSnapshot(
            id=record.id,
            kind=BrowserCompanionControlKind.RELOAD_EXTENSION,
            companion_id=record.companion_id,
            idempotency_key=record.idempotency_key,
            expected_current_build_sha256=record.request.expected_current_build_sha256,
            target_build_sha256=record.request.target_build_sha256,
            reason_code=record.request.reason_code,
            state=record.state,
            requested_at=record.requested_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            drain_deadline_at=record.drain_deadline_at,
            delivery_generation=record.delivery_generation,
            expected_runtime_instance_id=record.expected_runtime_instance_id,
            accepted_at=record.accepted_at,
            applied_at=record.applied_at,
            observed_build_sha256=record.observed_build_sha256,
            observed_runtime_instance_id=record.observed_runtime_instance_id,
            failure_code=record.failure_code,
        )

    def _validate_completion(
        self,
        submission: BrowserTaskSubmission,
        completion: BrowserTaskCompletion,
    ) -> None:
        for quote in completion.quotes:
            if quote.provider != submission.provider or quote.kind != submission.kind:
                raise BrowserClaimError("quote provider or kind does not match the claimed task")
        if (
            completion.failure
            and completion.failure.page_url
            and not _is_allowed_provider_url(submission.provider, completion.failure.page_url)
        ):
            raise BrowserClaimError("failure page_url does not match the claimed provider")
        if submission.kind != BrowserVertical.LODGING or completion.failure is None:
            return
        failure = completion.failure
        details = failure.details
        raw_receipt = details.get("inventory_receipt")
        sealed_sha256 = details.get("inventory_receipt_sha256")
        declared_state = details.get("inventory_result_state")
        audited_states = {state.value for state in LodgingInventoryReceiptState}
        if (
            declared_state not in audited_states
            and raw_receipt is None
            and sealed_sha256 is None
        ):
            return
        if (
            declared_state not in audited_states
            or not isinstance(raw_receipt, dict)
            or not isinstance(sealed_sha256, str)
        ):
            raise BrowserClaimError(
                "lodging inventory outcome requires a typed, SHA-sealed receipt"
            )
        try:
            receipt = LodgingInventoryReceipt.model_validate(raw_receipt)
        except ValueError as exc:
            raise BrowserClaimError("lodging inventory receipt is invalid") from exc
        expected_failure_codes = {
            LodgingInventoryReceiptState.CONFIRMED_EMPTY: {
                BrowserFailureCode.NO_INVENTORY,
            },
            LodgingInventoryReceiptState.BOUNDED_NO_EXACT_QUOTE: {
                BrowserFailureCode.DOM_DRIFT,
                BrowserFailureCode.EXTRACTION_ERROR,
            },
            LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING: {
                BrowserFailureCode.EXTRACTION_ERROR,
            },
        }
        expected_options = {
            key: value
            for key in (
                "expected_lodging_place_key",
                "expected_package_area",
                "segment",
            )
            if isinstance((value := submission.query.options.get(key)), str)
            and value
        }
        confirmed = receipt.confirmed_query
        expected_confirmed_exhaustive = (
            receipt.state == LodgingInventoryReceiptState.CONFIRMED_EMPTY
        )
        pending_duration_valid = True
        if receipt.state == LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING:
            pending = receipt.provider_pending_evidence
            pending_duration_valid = bool(
                pending is not None
                and details.get("bounded_pending_observed_ms")
                == pending.observed_duration_ms
            )
        elif details.get("bounded_pending_observed_ms") is not None:
            pending_duration_valid = False
        chain_version = details.get("inventory_observation_chain_schema_version")
        expected_chain_version = (
            "tripchord-qunar-empty-observation-chain-v1"
            if receipt.state == LodgingInventoryReceiptState.CONFIRMED_EMPTY
            else None
        )
        if (
            lodging_inventory_receipt_sha256(raw_receipt) != sealed_sha256
            or receipt.provider != submission.provider
            or receipt.state.value != declared_state
            or failure.code not in expected_failure_codes[receipt.state]
            or details.get("confirmed_exhaustive")
            is not expected_confirmed_exhaustive
            or details.get("scanned_count") != receipt.scanned_count
            or confirmed.destination != submission.query.destination
            or confirmed.start_date != submission.query.start_date
            or confirmed.end_date != submission.query.end_date
            or confirmed.adults != submission.query.adults
            or confirmed.rooms != submission.query.rooms
            or len(expected_options) != 3
            or confirmed.options != expected_options
            or receipt.page_url != failure.page_url
            or receipt.captured_at != failure.captured_at
            or chain_version != expected_chain_version
            or not pending_duration_valid
        ):
            raise BrowserClaimError(
                "lodging inventory receipt does not match the claimed query or failure"
            )

    def _reclaim_expired(self) -> bool:
        now = self._utc_now()
        changed = False
        for record in self._records.values():
            if (
                record.state != BrowserTaskState.CLAIMED
                or record.lease_expires_at is None
                or record.lease_expires_at > now
            ):
                continue
            changed = True
            record.claim_token = None
            record.lease_expires_at = None
            record.claimed_by = None
            record.claimed_at = None
            record.updated_at = now
            if record.attempt_count < record.submission.max_attempts:
                record.state = BrowserTaskState.QUEUED
            else:
                record.state = BrowserTaskState.FAILED
                self._active_consumers.pop(record.id, None)
                record.failure = BrowserFailure(
                    code=BrowserFailureCode.TIMEOUT,
                    message="browser companion did not complete the task before its lease expired",
                    retryable=True,
                    captured_at=now,
                )
        return changed

    def _housekeep(self) -> bool:
        reclaimed = self._reclaim_expired()
        reloads_updated = self._expire_reload_requests()
        pruned = self._prune_terminal_records()
        reloads_pruned = self._prune_terminal_reload_requests()
        return reclaimed or reloads_updated or pruned or reloads_pruned

    def _housekeep_and_notify_locked(self) -> bool:
        """Publish every housekeeping mutation to condition waiters.

        Callers must hold ``self._changed``. Lease expiry can turn a claimed task
        into a queued or terminal task without a Companion completion, so merely
        persisting that transition would strand ``wait_many`` until its outer
        multi-wave timeout.
        """

        changed = self._housekeep()
        if changed:
            self._persist_state()
            self._changed.notify_all()
        return changed

    def _expire_reload_requests(self) -> bool:
        now = self._utc_now()
        changed = False
        for record in self._reload_requests.values():
            if record.state.terminal:
                continue
            if now >= record.expires_at:
                record.state = BrowserCompanionControlState.EXPIRED
                record.updated_at = now
                record.failure_code = "reload_request_expired"
                changed = True
                continue
            if (
                record.state
                in {
                    BrowserCompanionControlState.QUEUED,
                    BrowserCompanionControlState.DRAINING,
                }
                and now >= record.drain_deadline_at
                and self._active_claims_for_companion(record.companion_id)
            ):
                record.state = BrowserCompanionControlState.FAILED
                record.updated_at = now
                record.failure_code = "drain_timeout"
                changed = True
        return changed

    def _prune_terminal_records(self) -> bool:
        now = self._utc_now()
        terminal = sorted(
            (record for record in self._records.values() if record.state.terminal),
            key=lambda record: (record.updated_at, record.id),
        )
        remove_ids = {
            record.id
            for record in terminal
            if (
                now - record.updated_at.astimezone(UTC) > self._terminal_retention
                and not self._terminal_record_has_pending_ledger_waiter(record)
            )
        }
        retained = [record for record in terminal if record.id not in remove_ids]
        # The bounded cap applies to ordinary terminal history. Ledger-backed
        # records are removed as soon as their registered waiter consumes the
        # result, so they must not be allowed to evict each other first.
        ordinary_retained = [
            record
            for record in retained
            if not self._terminal_record_has_pending_ledger_waiter(record)
        ]
        overflow = max(0, len(ordinary_retained) - self._max_terminal_records)
        remove_ids.update(record.id for record in ordinary_retained[:overflow])
        for task_id in remove_ids:
            self._records.pop(task_id, None)
            self._active_consumers.pop(task_id, None)
        return bool(remove_ids)

    def _terminal_record_has_pending_ledger_waiter(self, record: _TaskRecord) -> bool:
        return bool(
            record.submission.query.options.get(_LEDGER_TERMINAL_RETENTION_OPTION)
            is True
            and self._active_consumers.get(record.id, 0) > 0
        )

    def _prune_terminal_reload_requests(self) -> bool:
        now = self._utc_now()
        terminal = sorted(
            (
                record
                for record in self._reload_requests.values()
                if record.state.terminal
            ),
            key=lambda record: (record.updated_at, record.id),
        )
        remove_ids = {
            record.id
            for record in terminal
            if now - record.updated_at.astimezone(UTC) > self._terminal_retention
        }
        retained = [record for record in terminal if record.id not in remove_ids]
        nonterminal_count = sum(
            not record.state.terminal for record in self._reload_requests.values()
        )
        allowed_terminal = max(
            0,
            self._max_companion_control_records - nonterminal_count,
        )
        overflow = max(0, len(retained) - allowed_terminal)
        remove_ids.update(record.id for record in retained[:overflow])
        for request_id in remove_ids:
            self._reload_requests.pop(request_id, None)
        return bool(remove_ids)

    def _restore_persisted_state(self) -> None:
        if self._state_store is None:
            return
        state = self._state_store.load()
        if state is None:
            return
        now = self._utc_now()
        for persisted in state.tasks:
            if persisted.id in self._records:
                raise BrowserBridgeError(
                    f"browser bridge state contains duplicate task id: {persisted.id}"
                )
            record = _TaskRecord(
                id=persisted.id,
                submission=persisted.submission,
                state=persisted.state,
                created_at=persisted.created_at,
                updated_at=persisted.updated_at,
                attempt_count=persisted.attempt_count,
                claimed_by=None,
                claimed_at=None,
                claim_token=None,
                lease_expires_at=None,
                quotes=persisted.quotes,
                failure=persisted.failure,
                reused_from_task_id=persisted.reused_from_task_id,
                reuse_age_seconds=persisted.reuse_age_seconds,
                formal_execution_capability=(
                    dict(persisted.formal_execution_capability)
                    if persisted.formal_execution_capability is not None
                    else None
                ),
            )
            if record.state == BrowserTaskState.CLAIMED:
                record.claimed_by = None
                record.claimed_at = None
                record.lease_expires_at = None
                record.updated_at = now
                if record.attempt_count < record.submission.max_attempts:
                    record.state = BrowserTaskState.QUEUED
                    record.failure = None
                else:
                    record.state = BrowserTaskState.FAILED
                    record.failure = BrowserFailure(
                        code=BrowserFailureCode.TIMEOUT,
                        message=(
                            "browser bridge restarted after the final allowed task claim"
                        ),
                        retryable=True,
                        captured_at=now,
                        details={"recovered_after_restart": True},
                    )
            self._records[record.id] = record
            if not record.state.terminal:
                self._active_consumers[record.id] = 1
        for persisted_reload in state.reload_requests:
            if persisted_reload.id in self._reload_requests:
                raise BrowserBridgeError(
                    "browser bridge state contains duplicate reload request id: "
                    f"{persisted_reload.id}"
                )
            self._reload_requests[persisted_reload.id] = _CompanionReloadRecord(
                id=persisted_reload.id,
                companion_id=persisted_reload.companion_id,
                idempotency_key=persisted_reload.idempotency_key,
                request=persisted_reload.request,
                request_fingerprint_sha256=(
                    persisted_reload.request_fingerprint_sha256
                ),
                state=persisted_reload.state,
                requested_at=persisted_reload.requested_at,
                updated_at=persisted_reload.updated_at,
                expires_at=persisted_reload.expires_at,
                drain_deadline_at=persisted_reload.drain_deadline_at,
                expected_runtime_instance_id=(
                    persisted_reload.expected_runtime_instance_id
                ),
                delivery_generation=persisted_reload.delivery_generation,
                receipt_token_sha256=persisted_reload.receipt_token_sha256,
                accepted_at=persisted_reload.accepted_at,
                applied_at=persisted_reload.applied_at,
                observed_build_sha256=persisted_reload.observed_build_sha256,
                observed_runtime_instance_id=(
                    persisted_reload.observed_runtime_instance_id
                ),
                failure_code=persisted_reload.failure_code,
            )
        self._housekeep()
        self._persist_state()

    def _persist_state(self) -> None:
        if self._state_store is None:
            return
        records = tuple(
            PersistedBrowserTaskRecord(
                id=record.id,
                submission=record.submission,
                state=record.state,
                created_at=record.created_at,
                updated_at=record.updated_at,
                attempt_count=record.attempt_count,
                quotes=record.quotes,
                failure=record.failure,
                reused_from_task_id=record.reused_from_task_id,
                reuse_age_seconds=record.reuse_age_seconds,
                formal_execution_capability=record.formal_execution_capability,
            )
            for record in sorted(
                self._records.values(),
                key=lambda item: (item.created_at, item.id),
            )
        )
        reload_requests = tuple(
            PersistedBrowserCompanionReloadRecord(
                id=record.id,
                companion_id=record.companion_id,
                idempotency_key=record.idempotency_key,
                request=record.request,
                request_fingerprint_sha256=record.request_fingerprint_sha256,
                state=record.state,
                requested_at=record.requested_at,
                updated_at=record.updated_at,
                expires_at=record.expires_at,
                drain_deadline_at=record.drain_deadline_at,
                expected_runtime_instance_id=record.expected_runtime_instance_id,
                delivery_generation=record.delivery_generation,
                receipt_token_sha256=record.receipt_token_sha256,
                accepted_at=record.accepted_at,
                applied_at=record.applied_at,
                observed_build_sha256=record.observed_build_sha256,
                observed_runtime_instance_id=record.observed_runtime_instance_id,
                failure_code=record.failure_code,
            )
            for record in sorted(
                self._reload_requests.values(),
                key=lambda item: (item.requested_at, item.id),
            )
        )
        self._state_store.save(
            BrowserBridgePersistedState(
                saved_at=self._utc_now(),
                tasks=records,
                reload_requests=reload_requests,
            )
        )

    def _snapshot(self, record: _TaskRecord) -> BrowserTaskSnapshot:
        return BrowserTaskSnapshot(
            id=record.id,
            provider=record.submission.provider,
            kind=record.submission.kind,
            query=record.submission.query,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            attempt_count=record.attempt_count,
            claimed_by=record.claimed_by,
            claimed_at=record.claimed_at,
            quotes=record.quotes,
            failure=record.failure,
            reused_from_task_id=record.reused_from_task_id,
            reuse_age_seconds=record.reuse_age_seconds,
            inflight_coalesced=record.inflight_coalesce_count > 0,
        )

    def _fair_provider_batch(
        self,
        queued: list[_TaskRecord],
        limit: int,
        *,
        qunar_lodging_slots: int,
    ) -> tuple[_TaskRecord, ...]:
        """Build a stable provider-balanced batch for real browser concurrency.

        Qunar's overseas-lodging result endpoint self-throttles under same-domain
        concurrency. Only lease one such task per claim batch so waiting work
        remains queued without burning its absolute browser lease deadline.
        """
        by_provider = {
            provider: [record for record in queued if record.submission.provider == provider]
            for provider in BrowserProvider
        }
        selected: list[_TaskRecord] = []
        qunar_lodging_claims = 0
        while len(selected) < limit:
            made_progress = False
            for provider in BrowserProvider:
                records = by_provider[provider]
                if not records:
                    continue
                record_index = next(
                    (
                        index
                        for index, record in enumerate(records)
                        if not (
                            provider == BrowserProvider.QUNAR
                            and record.submission.kind == BrowserVertical.LODGING
                            and qunar_lodging_claims >= qunar_lodging_slots
                        )
                    ),
                    None,
                )
                if record_index is None:
                    continue
                record = records.pop(record_index)
                selected.append(record)
                if (
                    provider == BrowserProvider.QUNAR
                    and record.submission.kind == BrowserVertical.LODGING
                ):
                    qunar_lodging_claims += 1
                made_progress = True
                if len(selected) == limit:
                    break
            if not made_progress:
                break
        return tuple(selected)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("browser bridge clock must return timezone-aware timestamps")
        return value.astimezone(UTC)


def is_loopback_client(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def create_browser_bridge_app(
    bridge: BrowserTaskBridge | None = None,
    *,
    bridge_token: str,
    control_token: str | None = None,
    allowed_origin_regex: str = (
        r"^(chrome-extension://[a-p]{32}|"
        r"http://(?:127\.0\.0\.1|localhost)(?::\d+)?)$"
    ),
    source_authority: FormalLiveSourceAuthority | None = None,
    icom_provider: Any | None = None,
    formal_activation_failpoint_events: dict[str, asyncio.Event] | None = None,
) -> FastAPI:
    if len(bridge_token) < 32:
        raise ValueError("bridge_token must contain at least 32 characters")
    if control_token is not None and len(control_token) < 32:
        raise ValueError("control_token must contain at least 32 characters")
    if control_token is not None and hmac.compare_digest(control_token, bridge_token):
        raise ValueError("control_token must be distinct from bridge_token")
    formal_source_token = formal_worker_source_token(bridge_token)
    task_bridge = bridge or BrowserTaskBridge()
    if source_authority is not None:
        task_bridge.bind_source_authority(source_authority)
    app = FastAPI(title="TripChord Local Browser Bridge", version="1")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=allowed_origin_regex,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", BRIDGE_TOKEN_HEADER],
    )

    async def authorize(
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> None:
        host = request.client.host if request.client else None
        if not is_loopback_client(host):
            raise HTTPException(
                status_code=403,
                detail="browser bridge accepts loopback clients only",
            )
        if token is None or not hmac.compare_digest(token, bridge_token):
            raise HTTPException(status_code=401, detail="invalid browser bridge token")

    async def authorize_control(
        request: Request,
        bridge_credential: str | None,
        control_credential: str | None,
    ) -> None:
        await authorize(request, bridge_credential)
        if (
            control_token is None
            or control_credential is None
            or not hmac.compare_digest(control_credential, control_token)
        ):
            raise HTTPException(
                status_code=403,
                detail="browser companion control is unavailable or unauthorized",
            )

    async def authorize_formal_worker(
        request: Request,
        token: str | None,
    ) -> None:
        host = request.client.host if request.client else None
        if not is_loopback_client(host):
            raise HTTPException(
                status_code=403,
                detail="browser bridge accepts loopback clients only",
            )
        if token is None or not hmac.compare_digest(token, formal_source_token):
            raise HTTPException(
                status_code=401,
                detail="invalid formal worker source token",
            )

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        host = request.client.host if request.client else None
        if not is_loopback_client(host):
            raise HTTPException(
                status_code=403,
                detail="browser bridge accepts loopback clients only",
            )
        return {"status": "ok", "scope": "local-read-only-browser"}

    @app.post("/v1/tasks", response_model=SubmitBrowserTasksResponse)
    async def submit_tasks(
        payload: SubmitBrowserTasksRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> SubmitBrowserTasksResponse:
        await authorize(request, token)
        try:
            tasks = await task_bridge.submit_many(payload.tasks)
        except BrowserBridgeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return SubmitBrowserTasksResponse(tasks=tasks)

    @app.post(
        "/v1/formal/tasks",
        response_model=SubmitBrowserTasksResponse,
    )
    async def submit_formal_tasks(
        payload: SubmitFormalBrowserTasksRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> SubmitBrowserTasksResponse:
        """Attach worker-produced tasks to the parent authority's signed scope."""

        await authorize_formal_worker(request, token)
        if source_authority is None:
            raise HTTPException(status_code=409, detail="formal source is unavailable")
        try:
            with source_authority.execution_scope(payload.execution_capability):
                tasks = await task_bridge.submit_many(payload.tasks)
        except (BrowserBridgeError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SubmitBrowserTasksResponse(tasks=tasks)

    @app.post(
        "/v1/formal/tasks/cancel",
        response_model=SubmitBrowserTasksResponse,
    )
    async def cancel_formal_tasks(
        payload: CancelFormalBrowserTasksRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> SubmitBrowserTasksResponse:
        await authorize_formal_worker(request, token)
        if source_authority is None:
            raise HTTPException(status_code=409, detail="formal source is unavailable")
        try:
            with source_authority.execution_scope(payload.execution_capability):
                checked_capability = source_authority.current_execution_capability()
                if checked_capability is None:
                    raise ValueError("formal browser cancellation has no capability")
                attached = tuple(
                    await asyncio.gather(
                        *(
                            task_bridge.formal_execution_capability(task_id)
                            for task_id in payload.task_ids
                        )
                    )
                )
                if any(item != checked_capability for item in attached):
                    raise ValueError(
                        "formal browser cancellation crosses execution capabilities"
                    )
                tasks = await task_bridge.cancel_many(
                    payload.task_ids,
                    reason=payload.reason,
                )
        except (BrowserBridgeError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SubmitBrowserTasksResponse(tasks=tasks)

    @app.post(
        "/v1/formal/tasks/snapshots",
        response_model=SubmitBrowserTasksResponse,
    )
    async def read_formal_tasks(
        payload: ReadFormalBrowserTasksRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> SubmitBrowserTasksResponse:
        """Read only tasks attached to this exact signed worker capability."""

        await authorize_formal_worker(request, token)
        if source_authority is None:
            raise HTTPException(status_code=409, detail="formal source is unavailable")
        try:
            with source_authority.execution_scope(payload.execution_capability):
                checked_capability = source_authority.current_execution_capability()
                if checked_capability is None:
                    raise ValueError("formal browser snapshot has no capability")
                attached = tuple(
                    await asyncio.gather(
                        *(
                            task_bridge.formal_execution_capability(task_id)
                            for task_id in payload.task_ids
                        )
                    )
                )
                if any(item != checked_capability for item in attached):
                    raise ValueError(
                        "formal browser snapshot crosses execution capabilities"
                    )
                tasks = tuple(
                    await asyncio.gather(
                        *(task_bridge.get(task_id) for task_id in payload.task_ids)
                    )
                )
        except (BrowserBridgeError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return SubmitBrowserTasksResponse(tasks=tasks)

    @app.post("/v1/formal/icom/search")
    async def search_formal_icom(
        payload: FormalIComSearchRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> dict[str, Any]:
        """Execute the real parent-owned iCom adapter for one worker query."""

        await authorize_formal_worker(request, token)
        if source_authority is None or icom_provider is None:
            raise HTTPException(status_code=409, detail="formal iCom source is unavailable")
        from tripchord.providers.icom_transfer import IComTransferQuery

        try:
            query = IComTransferQuery.model_validate(payload.query)
            with source_authority.execution_scope(payload.execution_capability):
                result = await icom_provider.search(
                    query,
                    query_task_id=payload.query_task_id,
                )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return cast(dict[str, Any], result.model_dump(mode="json"))

    @app.post(
        "/v1/tasks/claim",
        response_model=ClaimBrowserTasksResponse,
        response_model_exclude_none=True,
    )
    async def claim_tasks(
        payload: ClaimBrowserTasksRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> ClaimBrowserTasksResponse:
        await authorize(request, token)
        try:
            response = await task_bridge.claim_response(
                payload.companion_id,
                providers=payload.providers,
                limit=payload.limit,
                authorized_scope_keys=payload.authorized_scope_keys,
                adapter_version=payload.adapter_version,
                contract_version=payload.contract_version,
                build_identity=payload.build_identity,
                runtime_instance_id=payload.runtime_instance_id,
                reload_receipt=payload.reload_receipt,
            )
            if source_authority is not None and response.leases:
                scoped_leases: list[
                    tuple[BrowserTaskLease, dict[str, object]]
                ] = []
                for lease in response.leases:
                    capability = await task_bridge.formal_execution_capability(
                        lease.task_id
                    )
                    if capability is not None:
                        scoped_leases.append((lease, capability))
                for capability_id in dict.fromkeys(
                    str(capability["capability_id"])
                    for _lease, capability in scoped_leases
                ):
                    group = [
                        lease
                        for lease, capability in scoped_leases
                        if capability["capability_id"] == capability_id
                    ]
                    capability = next(
                        capability
                        for _lease, capability in scoped_leases
                        if capability["capability_id"] == capability_id
                    )
                    formal_leases: list[dict[str, object]] = []
                    with source_authority.execution_scope(capability):
                        if source_authority.snapshot()["last_heartbeat"] is None:
                            raise ValueError(
                                "formal browser claim has no acknowledged Companion heartbeat"
                            )
                        for lease in group:
                            lease_payload = lease.model_dump(
                                mode="json", exclude={"claim_token"}
                            )
                            lease_payload["formal_query"] = (
                                source_authority.formal_browser_query(
                                    task_id=lease.task_id,
                                    provider=lease.provider.value,
                                    kind=lease.kind.value,
                                    query=lease.query.model_dump(mode="json"),
                                )
                            )
                            formal_leases.append(lease_payload)
                        source_authority.record_browser_http(
                            "browser_claim",
                            subject_ids=tuple(lease.task_id for lease in group),
                            details={
                                "request": payload.model_dump(
                                    mode="json",
                                    exclude={"reload_receipt"},
                                ),
                                "leases": formal_leases,
                            },
                        )
            pending_activation = (
                source_authority.pending_activation_request()
                if source_authority is not None
                else None
            )
            return response.model_copy(
                update={
                    "formal_activation_request": (
                        FormalBrowserActivationRequest.model_validate(
                            pending_activation
                        )
                        if pending_activation is not None
                        else None
                    )
                }
            )
        except BrowserCompanionReloadNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BrowserCompanionControlError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get("/v1/companions/status", response_model=BrowserCompanionStatusResponse)
    async def companion_status(
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> BrowserCompanionStatusResponse:
        await authorize(request, token)
        return await task_bridge.companion_status()

    @app.post(
        "/v1/companions/heartbeat",
        response_model=BrowserCompanionHeartbeatResponse,
    )
    async def companion_heartbeat(
        payload: BrowserCompanionHeartbeatRequest,
        request: Request,
        background_tasks: BackgroundTasks,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> BrowserCompanionHeartbeatResponse:
        await authorize(request, token)
        try:
            acknowledgment = (
                payload.formal_activation_ack.model_dump(mode="json")
                if payload.formal_activation_ack is not None
                else None
            )
            pending_before = (
                source_authority.pending_activation_request()
                if source_authority is not None
                else None
            )
            if acknowledgment is not None and acknowledgment != pending_before:
                raise ValueError(
                    "formal activation heartbeat acknowledgment is not the pending job"
                )
            request_details = payload.model_dump(
                mode="json",
                exclude={"formal_activation_ack"},
            )
            if source_authority is not None and acknowledgment is not None:
                source_authority.validate_activation_heartbeat_request(
                    acknowledgment=acknowledgment,
                    request_details=request_details,
                )
            heartbeat = await task_bridge.heartbeat(
                payload.companion_id,
                providers=payload.providers,
                authorized_scope_keys=payload.authorized_scope_keys,
                adapter_version=payload.adapter_version,
                contract_version=payload.contract_version,
                build_identity=payload.build_identity,
                runtime_instance_id=payload.runtime_instance_id,
            )
            if source_authority is not None and acknowledgment is not None:
                source_authority.record_activation_heartbeat(
                    acknowledgment=acknowledgment,
                    request_details=request_details,
                    heartbeat=heartbeat.model_dump(mode="json"),
                )
            failpoint_event = (
                formal_activation_failpoint_events.get(str(acknowledgment["job_id"]))
                if formal_activation_failpoint_events is not None
                and acknowledgment is not None
                else None
            )
            if failpoint_event is not None:
                try:
                    await asyncio.wait_for(failpoint_event.wait(), timeout=10.0)
                except TimeoutError as exc:
                    raise RuntimeError(
                        "formal activation failpoint did not reach durable dispatch"
                    ) from exc

                async def exit_after_ack_response() -> None:
                    # Starlette runs response background tasks only after the
                    # final ASGI body frame.  The short yield lets uvicorn flush
                    # that already-written 200 response to the loopback client
                    # before reproducing the intended process interruption.
                    await asyncio.sleep(_FORMAL_ACTIVATION_FAILPOINT_ACK_FLUSH_SECONDS)
                    os._exit(86)

                background_tasks.add_task(exit_after_ack_response)
            pending = (
                source_authority.pending_activation_request()
                if source_authority is not None
                else None
            )
            return BrowserCompanionHeartbeatResponse(
                **heartbeat.model_dump(mode="python"),
                formal_activation_request=pending,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/companions/{companion_id}/reload-requests",
        response_model=BrowserCompanionReloadRequestSnapshot,
    )
    async def request_companion_reload(
        companion_id: str,
        payload: BrowserCompanionReloadRequestBody,
        request: Request,
        bridge_credential: Annotated[
            str | None,
            Header(alias=BRIDGE_TOKEN_HEADER),
        ] = None,
        control_credential: Annotated[
            str | None,
            Header(alias=CONTROL_TOKEN_HEADER),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Header(alias=IDEMPOTENCY_KEY_HEADER),
        ] = None,
    ) -> BrowserCompanionReloadRequestSnapshot:
        await authorize_control(request, bridge_credential, control_credential)
        if idempotency_key is None:
            raise HTTPException(status_code=400, detail="Idempotency-Key is required")
        try:
            return await task_bridge.request_reload(
                companion_id,
                idempotency_key=idempotency_key,
                request=payload,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BrowserCompanionControlError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get(
        "/v1/companions/{companion_id}/reload-requests/{request_id}",
        response_model=BrowserCompanionReloadRequestSnapshot,
    )
    async def get_companion_reload_request(
        companion_id: str,
        request_id: str,
        request: Request,
        bridge_credential: Annotated[
            str | None,
            Header(alias=BRIDGE_TOKEN_HEADER),
        ] = None,
        control_credential: Annotated[
            str | None,
            Header(alias=CONTROL_TOKEN_HEADER),
        ] = None,
    ) -> BrowserCompanionReloadRequestSnapshot:
        await authorize_control(request, bridge_credential, control_credential)
        try:
            return await task_bridge.get_reload_request(companion_id, request_id)
        except BrowserCompanionReloadNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/v1/companions/control/receipt",
        response_model=BrowserCompanionReloadRequestSnapshot,
    )
    async def record_companion_reload_receipt(
        payload: BrowserCompanionReloadReceipt,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> BrowserCompanionReloadRequestSnapshot:
        await authorize(request, token)
        try:
            return await task_bridge.record_reload_receipt(payload)
        except BrowserCompanionReloadNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BrowserCompanionControlError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc

    @app.get("/v1/tasks/{task_id}", response_model=BrowserTaskSnapshot)
    async def get_task(
        task_id: str,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> BrowserTaskSnapshot:
        await authorize(request, token)
        try:
            return await task_bridge.get(task_id)
        except BrowserTaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/tasks/{task_id}/complete", response_model=BrowserTaskSnapshot)
    async def complete_task(
        task_id: str,
        payload: CompleteBrowserTaskRequest,
        request: Request,
        token: Annotated[str | None, Header(alias=BRIDGE_TOKEN_HEADER)] = None,
    ) -> BrowserTaskSnapshot:
        await authorize(request, token)
        try:
            snapshot = await task_bridge.complete(
                task_id,
                payload.claim_token,
                payload.completion,
                payload.source_execution_attestation,
            )
            return snapshot
        except BrowserTaskNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except BrowserClaimError as exc:
            logger.warning(
                "browser task completion rejected for %s: %s",
                task_id,
                exc,
            )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    app.state.browser_task_bridge = task_bridge
    return app


def create_browser_bridge_app_from_env() -> FastAPI:
    token = os.environ.get("TRIPCHORD_BROWSER_BRIDGE_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError(
            "set TRIPCHORD_BROWSER_BRIDGE_TOKEN to a random value of at least 32 characters"
        )
    origin_regex = os.environ.get(
        "TRIPCHORD_BROWSER_BRIDGE_ALLOWED_ORIGIN_REGEX",
        r"^(chrome-extension://[a-p]{32}|"
        r"http://(?:127\.0\.0\.1|localhost)(?::\d+)?)$",
    )
    return create_browser_bridge_app(
        bridge_token=token,
        control_token=(
            os.environ.get("TRIPCHORD_BROWSER_BRIDGE_CONTROL_TOKEN") or None
        ),
        allowed_origin_regex=origin_regex,
    )
