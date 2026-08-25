from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar, cast
from urllib.parse import parse_qs, parse_qsl, urlparse

from pydantic import Field, JsonValue, model_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.package import (
    FlightGroundTransferContract,
    LodgingLocationConvenience,
    NormalizedFlightQuote,
    NormalizedFlightSegment,
    NormalizedLodgingQuote,
    PackageArea,
    PackagePlaceKey,
    QuoteAvailability,
    TransferOption,
    TransferPriceScope,
    TransferPurchaseScope,
    TransferScheduleMode,
    lodging_non_remote_evidence_confirmed,
)
from tripchord.providers.browser_bridge import (
    PRODUCTION_VISIBLE_DOM_PARSER_VERSION,
    QUNAR_CURRENT_DETAIL_FALLBACK_SUMMARY_VERSION,
    QUNAR_DETAIL_SEED_SELECTION_POLICY,
    BrowserProvider,
    BrowserQuote,
    BrowserSearchQuery,
    BrowserTaskSnapshot,
    BrowserTaskState,
    BrowserVertical,
    LodgingInventoryReceipt,
    LodgingInventoryReceiptState,
    QuotePriceBasis,
    TrustedSearchUrlContract,
    lodging_inventory_receipt_sha256,
    qunar_detail_seed_selection,
    trusted_search_url_contract,
)


class QuoteNormalizationStatus(StrEnum):
    USABLE = "usable"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"


class QuoteNormalizationCode(StrEnum):
    KIND_MISMATCH = "kind_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    TAXES_INCOMPLETE = "taxes_incomplete"
    UNSUPPORTED_PRICE_BASIS = "unsupported_price_basis"
    NON_INTEGRAL_CENTS = "non_integral_cents"
    MISSING_FIELD = "missing_field"
    INVALID_FIELD = "invalid_field"
    QUERY_CONTEXT_MISMATCH = "query_context_mismatch"
    INVALID_TRANSFER = "invalid_transfer"
    INCOMPLETE_ROUND_TRIP = "incomplete_round_trip"
    UNSAFE_BROWSER_ACTION = "unsafe_browser_action"


class QuoteNormalizationIssue(DomainModel):
    code: QuoteNormalizationCode
    message: str = Field(min_length=1)
    field: str | None = None
    scope: str = "quote"


type NormalizedPrimaryQuote = NormalizedFlightQuote | NormalizedLodgingQuote


class NormalizedBrowserQuoteResult(DomainModel):
    provider: str
    kind: BrowserVertical
    status: QuoteNormalizationStatus
    quote: NormalizedPrimaryQuote | None = None
    transfers: tuple[TransferOption, ...] = ()
    issues: tuple[QuoteNormalizationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_result_shape(self) -> NormalizedBrowserQuoteResult:
        if self.status == QuoteNormalizationStatus.REJECTED:
            if self.quote is not None:
                raise ValueError("rejected normalization cannot expose a primary quote")
            if not self.issues:
                raise ValueError("rejected normalization requires a typed issue")
        elif self.quote is None:
            raise ValueError("normalized result requires a primary quote")
        return self

    @property
    def usable(self) -> bool:
        return self.status == QuoteNormalizationStatus.USABLE and self.quote is not None


class FlightPartyPriceObservation(DomainModel):
    """One immutable browser observation used by the server-owned 1/N proof."""

    task_id: str = Field(min_length=1)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adults: int = Field(ge=1, le=9)
    amount_cents: int = Field(gt=0)
    captured_at: datetime
    expires_at: datetime
    same_product_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    query_and_readback_confirmed: bool = True
    taxes_included: bool = True
    available_for_requested_adults: bool = True

    @model_validator(mode="after")
    def validate_observation(self) -> FlightPartyPriceObservation:
        if self.expires_at <= self.captured_at:
            raise ValueError("party-price observation expiry must follow capture")
        if not self.query_and_readback_confirmed:
            raise ValueError("party-price observation requires query/readback confirmation")
        if not self.taxes_included:
            raise ValueError("party-price observation requires tax-inclusive evidence")
        return self


class FlightPartyComparisonReceipt(DomainModel):
    """Provider-agnostic, server-owned proof derived from exact 1/N searches.

    The receipt never mutates either browser quote.  It is a separate,
    hash-addressed comparison artifact and deliberately describes a comparison
    amount rather than a settlement lock or held inventory.
    """

    schema_version: str = Field(
        default="tripchord.flight_party_comparison.v2",
        pattern=r"^tripchord\.flight_party_comparison\.v2$",
        validation_alias="schema",
        serialization_alias="schema",
    )
    verification: str = Field(
        default="server_owned_same_product",
        pattern=r"^server_owned_same_product$",
    )
    provider: BrowserProvider
    currency: str = Field(min_length=3, max_length=3)
    origin_code: str = Field(pattern=r"^[A-Z]{3}$")
    destination_code: str = Field(pattern=r"^[A-Z]{3}$")
    start_date: date
    end_date: date
    requested_adults: int = Field(ge=2, le=9)
    same_product_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    outbound_flight_numbers: tuple[str, ...] = Field(min_length=1)
    return_flight_numbers: tuple[str, ...] = Field(min_length=1)
    outbound_times: tuple[datetime, datetime]
    return_times: tuple[datetime, datetime]
    price_basis: str = Field(pattern=r"^(?:per_person|total_party)$")
    derivation_method: str = Field(
        pattern=r"^(?:equal_display_amounts_imply_per_adult|explicit_n_party_total)$"
    )
    display_amount_cents: int = Field(gt=0)
    total_for_party_cents: int = Field(gt=0)
    capture_skew_seconds: float = Field(ge=0)
    validity_overlap_start: datetime
    validity_overlap_end: datetime
    one_adult: FlightPartyPriceObservation
    requested_party: FlightPartyPriceObservation
    settlement_locked: bool = False
    inventory_locked: bool = False
    comparison_only: bool = False

    @model_validator(mode="after")
    def validate_receipt(self) -> FlightPartyComparisonReceipt:
        if self.end_date < self.start_date:
            raise ValueError("party-price comparison dates are inverted")
        if self.validity_overlap_end <= self.validity_overlap_start:
            raise ValueError("party-price comparison freshness windows do not overlap")
        if self.one_adult.adults != 1:
            raise ValueError("party-price comparison baseline must contain one adult")
        if self.requested_party.adults != self.requested_adults:
            raise ValueError("party-price comparison requested-party count differs")
        if any(
            item.same_product_fingerprint != self.same_product_fingerprint
            for item in (self.one_adult, self.requested_party)
        ):
            raise ValueError("party-price comparison observations are not the same product")
        if self.display_amount_cents != self.requested_party.amount_cents:
            raise ValueError("display amount must bind the requested-party observation")
        if self.price_basis == "per_person":
            if self.derivation_method != "equal_display_amounts_imply_per_adult":
                raise ValueError("per-person proof requires equal-display derivation")
            if self.one_adult.amount_cents != self.requested_party.amount_cents:
                raise ValueError("per-person proof requires equal 1/N display amounts")
            if self.total_for_party_cents != (
                self.one_adult.amount_cents * self.requested_adults
            ):
                raise ValueError("per-person proof total does not equal amount times adults")
        else:
            if self.derivation_method != "explicit_n_party_total":
                raise ValueError("party-total proof requires explicit-total derivation")
            if self.requested_party.amount_cents != (
                self.one_adult.amount_cents * self.requested_adults
            ):
                raise ValueError("party-total proof does not equal the 1-adult multiple")
            if self.total_for_party_cents != self.requested_party.amount_cents:
                raise ValueError("party-total proof must use the requested-party amount")
        if self.settlement_locked or self.inventory_locked:
            raise ValueError("comparison receipt cannot claim settlement or inventory lock")
        if not self.comparison_only and any(
            not item.available_for_requested_adults
            for item in (self.one_adult, self.requested_party)
        ):
            raise ValueError("non-comparison receipt requires requested-party availability")
        return self


def flight_party_comparison_receipt_sha256(
    receipt: FlightPartyComparisonReceipt,
) -> str:
    canonical = json.dumps(
        receipt.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


class _RejectNormalization(ValueError):
    def __init__(
        self,
        code: QuoteNormalizationCode,
        message: str,
        *,
        field: str | None = None,
        scope: str = "quote",
    ) -> None:
        super().__init__(message)
        self.issue = QuoteNormalizationIssue(
            code=code,
            message=message,
            field=field,
            scope=scope,
        )


class BrowserQuoteNormalizer:
    """Deterministically converts browser evidence into integer-cent package quotes."""

    _BROWSER_OMITTED_ORCHESTRATION_OPTION_KEYS = frozenset(
        {
            "__tripchord_allow_recent_quote_reuse",
            "__tripchord_ledger_terminal_retention",
            "__tripchord_reuse_exact_result_tab",
            "__tripchord_browser_wait_source_count",
            "__tripchord_skip_flight_providers",
            "__tripchord_full_window_flight_screen",
            "gateway_destination",
            "stay_area_search_profile",
            "stay_plan_candidate_set",
        }
    )
    _BROWSER_QUERY_EVIDENCE_KEYS = (
        "origin",
        "destination",
        "start_date",
        "end_date",
        "adults",
        "children",
        "children_ages",
        "infants",
        "party_shape_supported",
        "party_shape_failure",
        "rooms",
        "currency",
        "origin_code",
        "destination_code",
        "search_url",
    )
    _AUDITED_CTRIP_PROPERTY_SEEDS: ClassVar[
        dict[str, tuple[str, str, frozenset[str]]]
    ] = {
        "maafushi": ("35851", "Maafushi", frozenset({"47330536", "131576087"})),
        "hulhumale": ("705784", "Hulhumale", frozenset({"29935473", "1948695"})),
    }
    _AUDITED_QUNAR_MAAFUSHI_DETAILS: ClassVar[dict[str, tuple[str, str]]] = {
        "2112": ("i-ka_maafushi_2112", "Kaani Palm Beach"),
        "2055": ("i-ka_maafushi_2055", "Kaani Grand Seaview"),
        "2071": ("i-ka_maafushi_2071", "Maafushi View"),
        "2072": ("i-ka_maafushi_2072", "Maafushi Village"),
        "2075": ("i-ka_maafushi_2075", "Maafushi Veli"),
        "2142": ("i-ka_maafushi_2142", "SEASUNBEACH"),
    }

    def __init__(self, *, quote_ttl_seconds: int = 600) -> None:
        if quote_ttl_seconds < 1:
            raise ValueError("quote_ttl_seconds must be positive")
        self._quote_ttl = timedelta(seconds=quote_ttl_seconds)

    def normalize(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
        *,
        party_price_comparisons: tuple[FlightPartyComparisonReceipt, ...] = (),
    ) -> NormalizedBrowserQuoteResult:
        primary: NormalizedPrimaryQuote
        try:
            search_url_contract = self._validate_common(quote, query)
            if quote.kind == BrowserVertical.FLIGHT:
                primary = self._flight(
                    quote,
                    query,
                    search_url_contract,
                    party_price_comparisons=party_price_comparisons,
                )
                transfers: tuple[TransferOption, ...] = ()
                transfer_issues: tuple[QuoteNormalizationIssue, ...] = ()
            elif quote.kind == BrowserVertical.LODGING:
                primary = self._lodging(quote, query)
                transfers, transfer_issues = self._transfers(quote, query)
            else:  # pragma: no cover - BrowserVertical is currently exhaustive
                raise _RejectNormalization(
                    QuoteNormalizationCode.KIND_MISMATCH,
                    f"unsupported browser vertical: {quote.kind}",
                )
        except _RejectNormalization as exc:
            return NormalizedBrowserQuoteResult(
                provider=quote.provider.value,
                kind=quote.kind,
                status=QuoteNormalizationStatus.REJECTED,
                issues=(exc.issue,),
            )

        status = (
            QuoteNormalizationStatus.USABLE
            if primary.availability == QuoteAvailability.AVAILABLE
            else QuoteNormalizationStatus.UNAVAILABLE
        )
        return NormalizedBrowserQuoteResult(
            provider=quote.provider.value,
            kind=quote.kind,
            status=status,
            quote=primary,
            transfers=transfers,
            issues=transfer_issues,
        )

    def normalize_many(
        self,
        quotes: tuple[BrowserQuote, ...],
        query: BrowserSearchQuery,
        *,
        party_price_comparisons: tuple[FlightPartyComparisonReceipt, ...] = (),
    ) -> tuple[NormalizedBrowserQuoteResult, ...]:
        return tuple(
            self.normalize(
                quote,
                query,
                party_price_comparisons=party_price_comparisons,
            )
            for quote in quotes
        )

    def _validate_common(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
    ) -> TrustedSearchUrlContract | None:
        self._validate_visible_evidence(quote)
        self._validate_round_trip_protocol(quote)
        search_url_contract = self._validate_query_evidence(quote, query)
        if quote.currency != query.currency:
            raise _RejectNormalization(
                QuoteNormalizationCode.CURRENCY_MISMATCH,
                "browser quote currency does not match the requested comparison currency",
                field="currency",
            )
        if quote.taxes_included is not True:
            raise _RejectNormalization(
                QuoteNormalizationCode.TAXES_INCOMPLETE,
                "taxes and mandatory fees are not confirmed as included",
                field="taxes_included",
            )
        return search_url_contract

    def _validate_round_trip_protocol(self, quote: BrowserQuote) -> None:
        if quote.kind != BrowserVertical.FLIGHT:
            return
        details = quote.details
        expected_workflow = {
            BrowserProvider.CTRIP: "staged_outbound_return",
            BrowserProvider.FLIGGY: "staged_outbound_return",
            BrowserProvider.QUNAR: "combined_roundtrip_card",
            BrowserProvider.TONGCHENG: "staged_outbound_return",
        }[quote.provider]
        expected_party_statuses = {
            BrowserProvider.CTRIP: {"confirmed_for_party", "observed_party_context"},
            BrowserProvider.FLIGGY: {"comparison_only"},
            BrowserProvider.QUNAR: {"confirmed_for_party", "observed_party_context"},
            BrowserProvider.TONGCHENG: {"confirmed_for_party", "observed_party_context"},
        }[quote.provider]
        fixed_fields = {
            "workflow_kind": expected_workflow,
            "combination_status": "round_trip_complete",
            "journey_price_scope": "round_trip",
            "price_finality": "final_for_combination",
        }
        for field, expected in fixed_fields.items():
            if details.get(field) != expected:
                raise _RejectNormalization(
                    QuoteNormalizationCode.INCOMPLETE_ROUND_TRIP,
                    f"flight {field} must be {expected}",
                    field=field,
                )
        if details.get("party_availability_status") not in expected_party_statuses:
            raise _RejectNormalization(
                QuoteNormalizationCode.INCOMPLETE_ROUND_TRIP,
                "flight party context is not an audited observed or confirmed state",
                field="party_availability_status",
            )
        for field in (
            "combination_id",
            "price_basis_evidence",
            "tax_evidence",
            "selection_evidence",
            "availability_evidence",
        ):
            value = details.get(field)
            if not isinstance(value, str) or not value.strip():
                raise _RejectNormalization(
                    QuoteNormalizationCode.INCOMPLETE_ROUND_TRIP,
                    f"flight {field} must be a non-empty string",
                    field=field,
                )

        self._validate_flight_price_evidence(quote)
        tax_evidence = self._str(details, "tax_evidence")
        if re.search(
            r"未含税|不含税|税费另付|另付税|tax(?:es)?\s+(?:not\s+included|excluded)|excludes?\s+tax(?:es)?",
            tax_evidence,
            re.IGNORECASE,
        ) or not re.search(
            r"含税|税费已含|含税及服务费|税费全包|tax(?:es)?\s+included|all\s+tax(?:es)?",
            tax_evidence,
            re.IGNORECASE,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.TAXES_INCOMPLETE,
                "flight tax inclusion is not backed by unambiguous visible text",
                field="tax_evidence",
            )
        if details.get("availability") != QuoteAvailability.AVAILABLE.value:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "successful flight quote requires explicit available status",
                field="availability",
            )
        availability_evidence = self._str(details, "availability_evidence")
        qunar_read_only_result_evidence = (
            quote.provider == BrowserProvider.QUNAR
            and details.get("workflow_kind") == "combined_roundtrip_card"
            and details.get("combination_status") == "round_trip_complete"
            and details.get("party_availability_status") in {
                "confirmed_for_party",
                "observed_party_context",
            }
            and "exact_trusted_url_party_context:" in availability_evidence
            and "visible_result_card" in availability_evidence
            and "inventory_not_locked" in availability_evidence
        )
        if re.search(
            r"售罄|无票|不可预订|无法预订|已下架|sold\s*out|unavailable|not\s+available",
            availability_evidence,
            re.IGNORECASE,
        ) or not (
            qunar_read_only_result_evidence
            or re.search(
                r"选为返程|选择返程|选择航班|查看航班|预订|立即预订|book|select",
                availability_evidence,
                re.IGNORECASE,
            )
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "flight availability lacks an audited visible result or enabled control",
                field="availability_evidence",
            )

        outbound_departure = self._datetime(details, "outbound_departure_at")
        outbound_arrival = self._datetime(details, "outbound_arrival_at")
        return_departure = self._datetime(details, "return_departure_at")
        return_arrival = self._datetime(details, "return_arrival_at")
        if (
            outbound_arrival <= outbound_departure
            or return_departure <= outbound_arrival
            or return_arrival <= return_departure
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INCOMPLETE_ROUND_TRIP,
                "flight legs do not form a chronological complete round trip",
                field="flight_times",
            )

        trace = details.get("action_trace")
        if not isinstance(trace, list) or not trace or len(trace) > 8:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "flight action_trace must contain between one and eight actions",
                field="action_trace",
            )
        allowed = {
            "search",
            "filter",
            "select_outbound",
            "reselect_outbound",
            "provider_auto_selected_outbound",
            "select_return",
        }
        forbidden = (
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
        actions: list[str] = []
        for index, entry in enumerate(trace):
            if not isinstance(entry, dict):
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                    f"flight action_trace[{index}] must be an object",
                    field="action_trace",
                )
            action = entry.get("action")
            serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True).lower()
            if (
                not isinstance(action, str)
                or action not in allowed
                or any(marker in serialized for marker in forbidden)
            ):
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                    f"flight action_trace[{index}] is outside the read-only action contract",
                    field="action_trace",
                )
            actions.append(action)
        if not actions or actions[0] != "search":
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "flight action_trace must begin with search",
                field="action_trace",
            )
        if expected_workflow == "staged_outbound_return":
            valid_workflow_actions = any(
                action in {"select_outbound", "provider_auto_selected_outbound"}
                for action in actions
            )
        else:
            valid_workflow_actions = not any(
                action in {
                    "select_outbound",
                    "reselect_outbound",
                    "provider_auto_selected_outbound",
                }
                for action in actions
            )
        if not valid_workflow_actions:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "flight action_trace does not match the provider workflow",
                field="action_trace",
            )

    def _validate_flight_price_evidence(self, quote: BrowserQuote) -> None:
        price_text = self._str(quote.details, "price_text")
        price_basis_evidence = self._str(quote.details, "price_basis_evidence")
        if price_text != price_basis_evidence:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "flight price basis evidence must be the same atomic visible price text",
                field="price_basis_evidence",
            )
        if re.search(
            r"起价|最低价|参考价|预估(?:价|往返价)?|估算价|\bfrom\b|\bstarting\s+(?:at|from)\b|\bestimated(?:\s+price)?\b|\breference\s+price\b",
            price_text,
            re.IGNORECASE,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "flight price is starting, estimated, or reference-only",
                field="price_text",
            )
        matches = list(
            re.finditer(
                r"(¥|￥|CNY|RMB|USD|\$)\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,6}))?",
                price_text,
                re.IGNORECASE,
            )
        )
        if len(matches) != 1:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "flight price must contain exactly one atomic currency-and-amount value",
                field="price_text",
            )
        match = matches[0]
        amount = Decimal(
            f"{match.group(2).replace(',', '')}{f'.{match.group(3)}' if match.group(3) else ''}"
        )
        if amount != quote.amount:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "flight atomic price amount disagrees with the structured quote",
                field="amount",
            )
        currency = "USD" if match.group(1).upper() in {"USD", "$"} else "CNY"
        if currency != quote.currency:
            raise _RejectNormalization(
                QuoteNormalizationCode.CURRENCY_MISMATCH,
                "flight visible price currency disagrees with the structured quote",
                field="price_text",
            )
        basis_pattern = (
            r"人均|每人|/人|起/人|per\s+(?:person|adult)"
            if quote.price_basis == QuotePriceBasis.PER_PERSON
            else r"总价|合计|total(?:\s+(?:price|trip|party))?"
        )
        if (
            not re.search(basis_pattern, price_text, re.IGNORECASE)
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "flight visible price does not explicitly establish its basis",
                field="price_basis",
            )

    def _validate_visible_evidence(self, quote: BrowserQuote) -> None:
        if quote.parser_version != PRODUCTION_VISIBLE_DOM_PARSER_VERSION:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "quote parser marker is not an allowed production visible-DOM parser",
                field="parser_version",
            )
        try:
            payload = json.loads(quote.visible_evidence)
        except json.JSONDecodeError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "visible evidence must be canonical JSON",
                field="visible_evidence",
            ) from exc
        if not isinstance(payload, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "visible evidence payload must be an object",
                field="visible_evidence",
            )
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if canonical != quote.visible_evidence:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "visible evidence payload is not canonical",
                field="visible_evidence",
            )
        recomputed = hashlib.sha256(quote.visible_evidence.encode()).hexdigest()
        if recomputed != quote.evidence_sha256:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "visible evidence digest does not match its canonical payload",
                field="evidence_sha256",
            )
        expected = {
            "amount": self._canonical_decimal(quote.amount),
            "currency": quote.currency,
            "details": quote.details,
            "kind": quote.kind.value,
            "page_url": quote.page_url,
            "price_basis": quote.price_basis.value,
            "provider": quote.provider.value,
            "taxes_included": quote.taxes_included,
            "title": quote.title,
        }
        if payload != expected:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "visible evidence payload disagrees with structured quote fields",
                field="visible_evidence",
            )

    def _validate_query_evidence(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
    ) -> TrustedSearchUrlContract | None:
        snapshot = quote.details.get("query")
        if not isinstance(snapshot, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "quote requires a submitted-query snapshot",
                field="query",
            )
        full_expected_snapshot = query.model_dump(mode="json")
        # Mirror parser.js safeQuery(): bind every browser-driving party and
        # route field while keeping planner-only orchestration objects outside
        # the extension trust boundary.
        expected_snapshot = {
            key: full_expected_snapshot.get(key)
            for key in self._BROWSER_QUERY_EVIDENCE_KEYS
        }
        raw_options = full_expected_snapshot.get("options")
        expected_options = (
            json.loads(json.dumps(raw_options))
            if isinstance(raw_options, dict)
            else {}
        )
        for key in self._BROWSER_OMITTED_ORCHESTRATION_OPTION_KEYS:
            expected_options.pop(key, None)
        expected_snapshot["options"] = expected_options
        allowed_snapshots = [full_expected_snapshot, expected_snapshot]
        # Builds before the mixed-party fields were added emitted the same
        # minimal snapshot without zero/default party-shape values.  That shape
        # is equivalent only for the two-adult/default case; mixed parties stay
        # fail-closed.
        if (
            query.children == 0
            and not query.children_ages
            and query.infants == 0
            and query.party_shape_supported
            and query.party_shape_failure is None
        ):
            legacy_snapshot = dict(expected_snapshot)
            for key in (
                "children",
                "children_ages",
                "infants",
                "party_shape_supported",
                "party_shape_failure",
            ):
                legacy_snapshot.pop(key, None)
            allowed_snapshots.append(legacy_snapshot)
        if snapshot not in allowed_snapshots:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "quote query snapshot differs from the submitted search",
                field="query",
            )
        driver = quote.details.get("driver")
        if not isinstance(driver, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "quote requires browser driver evidence",
                field="driver",
            )
        if query.search_url is not None:
            try:
                contract = trusted_search_url_contract(quote.provider, quote.kind, query)
            except ValueError as exc:
                raise _RejectNormalization(
                    QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                    str(exc),
                    field="query.search_url",
                ) from exc
            if contract is None:  # pragma: no cover - guarded by query.search_url
                raise _RejectNormalization(
                    QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                    "trusted search URL contract was not resolved",
                    field="query.search_url",
                )
            self._validate_trusted_search_url_driver(quote, snapshot, driver, contract)
            return contract
        driver_mode = driver.get("mode")
        if driver_mode == "captured_read_only_detail":
            self._validate_read_only_lodging_detail_driver(quote, driver)
        elif driver_mode == "audited_property_seed_detail_fallback":
            self._validate_audited_property_seed_detail_driver(
                quote,
                snapshot,
                driver,
            )
        allowed_scope = (
            "confirmed_visible_seed_detail"
            if driver_mode == "audited_property_seed_detail_fallback"
            else "confirmed_visible_search"
        )
        if (
            driver_mode
            not in {
                "visible_form",
                "captured_read_only_detail",
                "audited_property_seed_detail_fallback",
            }
            or driver.get("triggered") is not True
            or driver.get("provider") != quote.provider.value
            or driver.get("vertical") != quote.kind.value
            or driver.get("confirmation_scope") != allowed_scope
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "quote is not backed by a confirmed production visible-form search",
                field="driver",
            )
        confirmed = driver.get("confirmed_query")
        readback = driver.get("readback_query")
        if not isinstance(confirmed, dict) or not isinstance(readback, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "visible-form evidence requires canonical and raw readback query fields",
                field="driver.confirmed_query",
            )
        required = (
            ("origin", "destination", "start_date", "end_date", "adults")
            if quote.kind == BrowserVertical.FLIGHT
            else ("destination", "start_date", "end_date", "adults", "rooms")
        )
        if set(confirmed) != set(required) or set(readback) != set(required):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "visible-form query evidence has missing or unexpected fields",
                field="driver.confirmed_query",
            )
        for field in required:
            if confirmed.get(field) != snapshot.get(field):
                raise _RejectNormalization(
                    QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                    f"confirmed visible field {field} differs from the submitted search",
                    field=f"driver.confirmed_query.{field}",
                )
            raw = readback.get(field)
            if field in {"adults", "rooms"}:
                if raw != snapshot.get(field):
                    raise _RejectNormalization(
                        QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                        f"visible count field {field} was not read back exactly",
                        field=f"driver.readback_query.{field}",
                    )
            elif not isinstance(raw, str) or not (
                self._visible_readback_matches(
                    field,
                    raw,
                    str(snapshot.get(field) or ""),
                )
                or (
                    field == "destination"
                    and quote.kind == BrowserVertical.LODGING
                    and (
                        self._audited_lodging_destination_alias_matches(raw, snapshot)
                        or self._audited_tongcheng_city_id_matches(
                            quote,
                            raw,
                            snapshot,
                            driver,
                        )
                    )
                )
            ):
                raise _RejectNormalization(
                    QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                    f"visible field {field} raw readback differs from the submitted search",
                    field=f"driver.readback_query.{field}",
                )
        return None

    def _audited_lodging_destination_alias_matches(
        self,
        raw: str,
        snapshot: dict[str, JsonValue],
    ) -> bool:
        options = snapshot.get("options")
        if not isinstance(options, dict):
            return False
        place_key = options.get("expected_lodging_place_key")
        if not isinstance(place_key, str):
            return False
        aliases = {
            "maafushi": {"maafushi", "马富施", "马富士"},
            "hulhumale": {
                "hulhumale",
                "hulhumalé",
                "胡鲁马累",
                "胡鲁马累岛",
            },
        }.get(place_key)
        if aliases is None:
            return False
        normalized_raw = "".join(character.lower() for character in raw if character.isalnum())
        normalized_aliases = {
            "".join(character.lower() for character in alias if character.isalnum())
            for alias in aliases
        }
        return normalized_raw in normalized_aliases

    def _audited_tongcheng_city_id_matches(
        self,
        quote: BrowserQuote,
        raw: str,
        snapshot: dict[str, JsonValue],
        driver: dict[str, JsonValue],
    ) -> bool:
        if (
            quote.provider != BrowserProvider.TONGCHENG
            or driver.get("destination_confirmation_scope")
            != "prefrozen_overseas_city_id_with_audited_party_url"
        ):
            return False
        options = snapshot.get("options")
        strategy = driver.get("lodging_search_strategy")
        if not isinstance(options, dict) or not isinstance(strategy, dict):
            return False
        place_key = options.get("expected_lodging_place_key")
        if not isinstance(place_key, str):
            return False
        expected_city_id = {
            "maafushi": "110018575",
            "hulhumale": "110018578",
        }.get(place_key)
        return (
            expected_city_id is not None
            and raw == f"audited-city-id:{expected_city_id}"
            and strategy.get("provider_destination_id") == expected_city_id
            and strategy.get("evidence_scope")
            == "provider_audited_exact_overseas_city_id_then_place_revalidation"
        )

    def _validate_read_only_lodging_detail_driver(
        self,
        quote: BrowserQuote,
        driver: dict[str, JsonValue],
    ) -> None:
        if quote.kind != BrowserVertical.LODGING:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "read-only detail capture is supported only for lodging quotes",
                field="driver.mode",
            )
        capture_field = (
            "qunar_detail_capture"
            if quote.provider == BrowserProvider.QUNAR
            else "detail_capture"
        )
        detail_capture = driver.get(capture_field)
        if not isinstance(detail_capture, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "read-only lodging detail capture requires a typed detail receipt",
                field=f"driver.{capture_field}",
            )
        parsed = urlparse(quote.page_url)
        query = parse_qs(parsed.query)
        if quote.provider == BrowserProvider.CTRIP:
            hotel_id = detail_capture.get("hotel_id")
            if (
                parsed.hostname != "hotels.ctrip.com"
                or parsed.path.rstrip("/") != "/hotels/detail"
                or detail_capture.get("source") != "ctrip_visible_exact_view_details"
                or not isinstance(hotel_id, str)
                or not hotel_id.isdigit()
                or query.get("hotelId") != [hotel_id]
                or detail_capture.get("popup_opened") is not False
                or detail_capture.get("preview_place_match") != "exact"
            ):
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                    "Ctrip lodging detail receipt is outside the audited read-only contract",
                    field="driver.detail_capture",
                )
        elif quote.provider == BrowserProvider.FLIGGY:
            property_id = detail_capture.get("property_id")
            if (
                parsed.hostname != "hotel.fliggy.com"
                or parsed.path.rstrip("/") != "/hotel_detail2.htm"
                or detail_capture.get("source") != "fliggy_visible_hotel_detail_link"
                or not isinstance(property_id, str)
                or not property_id.isdigit()
                or query.get("shid") != [property_id]
                or detail_capture.get("clicked_booking") is not False
            ):
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                    "Fliggy lodging detail receipt is outside the audited read-only contract",
                    field="driver.detail_capture",
                )
        elif quote.provider == BrowserProvider.QUNAR:
            self._validate_qunar_read_only_lodging_detail_driver(
                quote,
                driver,
                detail_capture,
                parsed,
            )
        else:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "provider has no audited read-only lodging detail contract",
                field=f"driver.{capture_field}",
            )

    def _validate_qunar_read_only_lodging_detail_driver(
        self,
        quote: BrowserQuote,
        driver: dict[str, JsonValue],
        detail_capture: dict[str, JsonValue],
        parsed: Any,
    ) -> None:
        """Fail closed around Qunar's audited, read-only Maafushi detail URLs."""

        rejected_field = "driver.qunar_detail_capture"
        path_match = re.fullmatch(
            r"/city/(i-ka_maafushi)/dt-([1-9]\d*)/",
            parsed.path,
        )
        property_id = path_match.group(2) if path_match is not None else None
        audited_property = (
            self._AUDITED_QUNAR_MAAFUSHI_DETAILS.get(property_id)
            if property_id is not None
            else None
        )
        try:
            fragment_pairs = parse_qsl(
                parsed.fragment,
                keep_blank_values=True,
                strict_parsing=True,
            )
            port = parsed.port
        except ValueError:
            fragment_pairs = []
            port = -1

        details = quote.details
        snapshot = details.get("query")
        if not isinstance(snapshot, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "Qunar lodging detail requires its exact submitted-query snapshot",
                field="query",
            )
        expected_fragment = {
            "fromDate": str(snapshot.get("start_date") or ""),
            "toDate": str(snapshot.get("end_date") or ""),
            "q": "",
            "showMap": "0",
        }
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hotel.qunar.com"
            or port is not None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query != ""
            or "/?#" not in quote.page_url
            or path_match is None
            or audited_property is None
            or len(fragment_pairs) != len(expected_fragment)
            or set(fragment_pairs) != set(expected_fragment.items())
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "Qunar lodging detail URL is outside the exact audited read-only allowlist",
                field=rejected_field,
            )

        expected_hotel_seq, expected_property_name = audited_property
        inventory_observation_state = detail_capture.get(
            "inventory_observation_state"
        )
        inventory_observation_count = detail_capture.get(
            "inventory_observation_count"
        )
        inventory_observation_duration_ms = detail_capture.get(
            "inventory_observation_duration_ms"
        )
        raw_inventory_receipt = detail_capture.get("list_inventory_receipt")
        inventory_receipt_sha256 = detail_capture.get(
            "list_inventory_receipt_sha256"
        )
        receipt: LodgingInventoryReceipt | None = None
        receipt_hash_valid = False
        if isinstance(raw_inventory_receipt, dict) and isinstance(
            inventory_receipt_sha256, str
        ):
            try:
                receipt = LodgingInventoryReceipt.model_validate(raw_inventory_receipt)
            except ValueError:
                receipt = None
            receipt_hash_valid = (
                receipt is not None
                and lodging_inventory_receipt_sha256(raw_inventory_receipt)
                == inventory_receipt_sha256
            )
        snapshot_options = snapshot.get("options")
        expected_receipt_options = (
            {
                key: snapshot_options.get(key)
                for key in (
                    "expected_lodging_place_key",
                    "expected_package_area",
                    "segment",
                )
            }
            if isinstance(snapshot_options, dict)
            else {}
        )
        exact_receipt_query = bool(
            receipt is not None
            and receipt.provider == BrowserProvider.QUNAR
            and receipt.confirmation_scope.value == "confirmed_visible_search"
            and receipt.scan_limit == 12
            and receipt.scanned_count == 0
            and receipt.confirmed_query.destination == snapshot.get("destination")
            and receipt.confirmed_query.start_date.isoformat()
            == snapshot.get("start_date")
            and receipt.confirmed_query.end_date.isoformat() == snapshot.get("end_date")
            and receipt.confirmed_query.adults == snapshot.get("adults")
            and receipt.confirmed_query.rooms == snapshot.get("rooms")
            and len(expected_receipt_options) == 3
            and all(
                isinstance(value, str) and value
                for value in expected_receipt_options.values()
            )
            and receipt.confirmed_query.options == expected_receipt_options
            and receipt.page_url
            == "https://hotel.qunar.com/city/i-ka_maafushi/"
        )
        seed_selection_valid = False
        expected_seed_offset: int | None = None
        expected_seed_property_ids: tuple[str, str] = ("", "")
        if receipt is not None and exact_receipt_query:
            expected_seed_offset, expected_seed_property_ids = (
                qunar_detail_seed_selection(receipt.confirmed_query)
            )
            expected_hotel_seqs = tuple(
                f"i-ka_maafushi_{item}" for item in expected_seed_property_ids
            )
            seed_selection_valid = bool(
                detail_capture.get("seed_selection_policy")
                == QUNAR_DETAIL_SEED_SELECTION_POLICY
                and detail_capture.get("seed_selection_offset")
                == expected_seed_offset
                and detail_capture.get("target_property_ids")
                == list(expected_seed_property_ids)
                and property_id in expected_seed_property_ids
                and detail_capture.get("hotel_seq")
                in expected_hotel_seqs
                and detail_capture.get("hotel_seq")
                == f"i-ka_maafushi_{property_id}"
            )
        confirmed_empty_observation = False
        bounded_pending_observation = False
        if receipt is not None and exact_receipt_query and receipt_hash_valid:
            if (
                receipt.state == LodgingInventoryReceiptState.CONFIRMED_EMPTY
                and receipt.observation_chain is not None
            ):
                chain = receipt.observation_chain
                matching_result = next(
                    (
                        item
                        for item in chain.detail_fallback.observed_results
                        if item.property_id == property_id
                    ),
                    None,
                )
                confirmed_empty_observation = bool(
                    inventory_observation_state == "confirmed_empty"
                    and type(inventory_observation_count) is int
                    and inventory_observation_count == 2
                    and type(inventory_observation_duration_ms) is int
                    and inventory_observation_duration_ms
                    == chain.observed_interval_ms
                    and detail_capture.get("list_inventory_receipt_schema_version")
                    == "tripchord-lodging-inventory-receipt-v2"
                    and detail_capture.get(
                        "inventory_observation_chain_schema_version"
                    )
                    == chain.schema_version
                    and chain.detail_fallback.contract_version
                    == QUNAR_CURRENT_DETAIL_FALLBACK_SUMMARY_VERSION
                    and chain.detail_fallback.seed_selection_policy
                    == QUNAR_DETAIL_SEED_SELECTION_POLICY
                    and chain.detail_fallback.seed_selection_offset
                    == expected_seed_offset
                    and chain.detail_fallback.target_property_ids
                    == expected_seed_property_ids
                    and chain.detail_fallback.verified_quote_count > 0
                    and matching_result is not None
                    and matching_result.verified_quote_count > 0
                )
            elif (
                receipt.state
                == LodgingInventoryReceiptState.BOUNDED_PROVIDER_PENDING
                and receipt.provider_pending_evidence is not None
            ):
                bounded_pending_observation = bool(
                    inventory_observation_state == "bounded_provider_pending"
                    and type(inventory_observation_count) is int
                    and inventory_observation_count == 1
                    and type(inventory_observation_duration_ms) is int
                    and inventory_observation_duration_ms
                    == receipt.provider_pending_evidence.observed_duration_ms
                    and detail_capture.get("list_inventory_receipt_schema_version")
                    == "tripchord-lodging-inventory-receipt-v1"
                    and detail_capture.get(
                        "inventory_observation_chain_schema_version"
                    )
                    is None
                )
        if (
            detail_capture.get("source")
            != "qunar_audited_read_only_lodging_detail"
            or detail_capture.get("contract_scope")
            != "audited_qunar_exact_detail_url"
            or detail_capture.get("clicked_booking") is not False
            or detail_capture.get("same_controlled_tab") is not True
            or detail_capture.get("city_slug") != "i-ka_maafushi"
            or detail_capture.get("hotel_seq") != expected_hotel_seq
            or detail_capture.get("property_id") != property_id
            or detail_capture.get("property_name") != expected_property_name
            or not receipt_hash_valid
            or not seed_selection_valid
            or not (
                confirmed_empty_observation or bounded_pending_observation
            )
            or driver.get("result_query_readback_confirmed") is not True
            or driver.get("result_query_readback_scope")
            != "qunar_visible_result_form_fields"
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "Qunar lodging detail receipt is outside the audited read-only contract",
                field=rejected_field,
            )

        lineage = driver.get("result_query_readback_evidence")
        expected_lineage_fields = {
            "provider_destination_id",
            "result_path",
            "destination_text",
            "start_date_text",
            "end_date_text",
            "occupancy_text",
            "room_scope",
        }
        if (
            not isinstance(lineage, dict)
            or set(lineage) != expected_lineage_fields
            or lineage.get("provider_destination_id") != "i-ka_maafushi"
            or lineage.get("result_path") != "/city/i-ka_maafushi"
            or lineage.get("room_scope")
            != "audited_qunar_single_room_search_surface"
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "Qunar detail quote is not descended from the exact audited result-list readback",
                field="driver.result_query_readback_evidence",
            )
        destination_text = lineage.get("destination_text")
        start_date_text = lineage.get("start_date_text")
        end_date_text = lineage.get("end_date_text")
        occupancy_text = lineage.get("occupancy_text")
        if (
            not isinstance(destination_text, str)
            or not self._audited_lodging_destination_alias_matches(
                destination_text,
                snapshot,
            )
            or not isinstance(start_date_text, str)
            or not self._visible_readback_matches(
                "start_date",
                start_date_text,
                str(snapshot.get("start_date") or ""),
            )
            or not isinstance(end_date_text, str)
            or not self._visible_readback_matches(
                "end_date",
                end_date_text,
                str(snapshot.get("end_date") or ""),
            )
            or not isinstance(occupancy_text, str)
            or not re.search(
                r"(?:2\s*(?:名|位|个)?\s*成人|2\s*adults?)",
                occupancy_text,
                re.IGNORECASE,
            )
            or not re.search(
                r"(?:0\s*(?:名|位|个)?\s*儿童|0\s*children)",
                occupancy_text,
                re.IGNORECASE,
            )
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "Qunar result-list lineage does not prove the exact destination, dates and party",
                field="driver.result_query_readback_evidence",
            )

        location_evidence = details.get("location_evidence")
        exact_maafushi_location = bool(
            isinstance(location_evidence, str)
            and re.search(
                r"(?:马富施|马富士|\bmaafushi\b)",
                location_evidence,
                re.IGNORECASE,
            )
        )
        exact_kaafu_location = bool(
            isinstance(location_evidence, str)
            and re.search(
                r"(?:卡夫环礁|\bkaafu\s+atoll\b)",
                location_evidence,
                re.IGNORECASE,
            )
        )
        conflicting_location = bool(
            isinstance(location_evidence, str)
            and re.search(
                r"(?:胡鲁马累|hulhumal[eé]|班度士|\bbandos\b)",
                location_evidence,
                re.IGNORECASE,
            )
        )
        required_non_empty_detail_fields = (
            "property_name",
            "room_text",
            "rate_text",
            "availability_text",
            "tax_evidence",
            "price_text",
            "price_unit_evidence",
        )
        if (
            snapshot.get("destination") != "Maafushi"
            or snapshot.get("adults") != 2
            or snapshot.get("rooms") != 1
            or details.get("extraction") != "visible_dom_qunar_lodging_detail"
            or details.get("city_slug") != "i-ka_maafushi"
            or details.get("hotel_seq") != expected_hotel_seq
            or details.get("property_id") != property_id
            or details.get("property_name") != expected_property_name
            or quote.title != expected_property_name
            or details.get("page_url") != quote.page_url
            or details.get("expected_lodging_place_key") != "maafushi"
            or details.get("observed_lodging_place_key") != "maafushi"
            or details.get("lodging_place_matches_expected") is not True
            or details.get("area_matches_expected") is not True
            or details.get("kaafu_area_confirmed") is not True
            or details.get("check_in") != snapshot.get("start_date")
            or details.get("check_out") != snapshot.get("end_date")
            or details.get("adults") != 2
            or details.get("rooms") != 1
            or details.get("clicked_booking") is not False
            or details.get("availability") != QuoteAvailability.AVAILABLE.value
            or details.get("price_basis_source")
            != "audited_qunar_lodging_detail_rate_contract"
            or details.get("price_finality") != "final_for_rate"
            or not exact_maafushi_location
            or not exact_kaafu_location
            or conflicting_location
            or any(
                not isinstance(details.get(field), str)
                or not str(details[field]).strip()
                for field in required_non_empty_detail_fields
            )
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "Qunar lodging detail does not prove the allowlisted property, "
                "exact stay and final rate",
                field="details",
            )

    def _validate_audited_property_seed_detail_driver(
        self,
        quote: BrowserQuote,
        snapshot: dict[str, JsonValue],
        driver: dict[str, JsonValue],
    ) -> None:
        """Validate the bounded Ctrip fallback against the mirrored seed allowlist.

        The seed supplies only a public property identity.  The quote itself
        still has to come from a freshly rendered, read-only detail page whose
        dates, party, room count, currency and exact place are revalidated by
        the normalizer; no booking action is permitted.
        """

        if (
            quote.kind != BrowserVertical.LODGING
            or quote.provider != BrowserProvider.CTRIP
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "audited property seed detail is supported only for Ctrip lodging",
                field="driver.mode",
            )
        detail_capture = driver.get("detail_capture")
        options = snapshot.get("options")
        if not isinstance(detail_capture, dict) or not isinstance(options, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "audited property seed detail requires typed seed and place receipts",
                field="driver.detail_capture",
            )
        place_key = options.get("expected_lodging_place_key")
        seed = (
            self._AUDITED_CTRIP_PROPERTY_SEEDS.get(place_key)
            if isinstance(place_key, str)
            else None
        )
        hotel_id = detail_capture.get("hotel_id")
        parsed = urlparse(quote.page_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        required_query_values = {
            "cityId": seed[0] if seed is not None else None,
            "cityEnName": seed[1] if seed is not None else None,
            "hotelId": hotel_id,
            "checkIn": snapshot.get("start_date"),
            "checkOut": snapshot.get("end_date"),
            "adult": str(snapshot.get("adults")),
            "children": "0",
            "crn": str(snapshot.get("rooms")),
            "curr": snapshot.get("currency"),
        }
        if (
            seed is None
            or parsed.hostname != "hotels.ctrip.com"
            or parsed.path.rstrip("/") != "/hotels/detail"
            or detail_capture.get("source") != "public_audited_property_id"
            or not isinstance(hotel_id, str)
            or hotel_id not in seed[2]
            or detail_capture.get("clicked_booking") is not False
            or quote.details.get("expected_lodging_place_key") != place_key
            or quote.details.get("observed_lodging_place_key") != place_key
            or quote.details.get("lodging_place_matches_expected") is not True
            or quote.details.get("area_matches_expected") is not True
            or any(
                not isinstance(expected, str)
                or query.get(field) != [expected]
                for field, expected in required_query_values.items()
            )
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSAFE_BROWSER_ACTION,
                "Ctrip property seed detail is outside the audited read-only contract",
                field="driver.detail_capture",
            )

    def _validate_trusted_search_url_driver(
        self,
        quote: BrowserQuote,
        snapshot: dict[str, JsonValue],
        driver: dict[str, JsonValue],
        contract: TrustedSearchUrlContract,
    ) -> None:
        if (
            driver.get("mode") != "search_url"
            or driver.get("triggered") is not True
            or driver.get("provider") != quote.provider.value
            or driver.get("vertical") != quote.kind.value
            or driver.get("confirmation_scope") != "trusted_exact_search_url"
            or driver.get("party_availability_confirmed")
            is not contract.party_availability_confirmed
            or driver.get("pricing_context") != contract.pricing_context
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "quote driver does not match the audited trusted-search-url contract",
                field="driver",
            )
        confirmed = driver.get("confirmed_query")
        readback = driver.get("readback_query")
        if not isinstance(confirmed, dict) or not isinstance(readback, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "trusted-search-url evidence requires canonical context and URL field readback",
                field="driver.confirmed_query",
            )
        canonical_fields = ("origin", "destination", "start_date", "end_date", "adults")
        if set(confirmed) != set(canonical_fields):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "trusted-search-url canonical context has missing or unexpected fields",
                field="driver.confirmed_query",
            )
        for field in canonical_fields:
            if confirmed.get(field) != snapshot.get(field):
                raise _RejectNormalization(
                    QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                    f"trusted-search-url context {field} differs from the submitted search",
                    field=f"driver.confirmed_query.{field}",
                )
        url_fields = dict(contract.url_readback)
        if readback != url_fields:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "trusted-search-url readback differs from fields encoded in the audited URL",
                field="driver.readback_query",
            )
        explicit_fields = driver.get("url_confirmed_fields")
        if explicit_fields != list(url_fields):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "trusted-search-url confirmed field names disagree with URL readback",
                field="driver.url_confirmed_fields",
            )
        if (
            contract.provider == BrowserProvider.FLIGGY
            and quote.price_basis != QuotePriceBasis.PER_PERSON
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "Fliggy trusted URL can only normalize a visible per-person fare",
                field="price_basis",
            )

    def _visible_readback_matches(
        self,
        field: str,
        raw: str,
        expected: str,
    ) -> bool:
        actual_key = "".join(character.lower() for character in raw if character.isalnum())
        expected_key = "".join(character.lower() for character in expected if character.isalnum())
        if not actual_key or not expected_key:
            return False
        if field in {"start_date", "end_date"}:
            try:
                expected_date = date.fromisoformat(expected)
            except ValueError:
                return False
            visible_date = re.search(
                r"(?<!\d)(\d{4})\D{1,4}(\d{1,2})\D{1,4}(\d{1,2})(?!\d)",
                raw,
            )
            if visible_date is not None:
                return (
                    int(visible_date.group(1)),
                    int(visible_date.group(2)),
                    int(visible_date.group(3)),
                ) == (expected_date.year, expected_date.month, expected_date.day)
            actual_digits = "".join(character for character in raw if character.isdigit())
            expected_digits = "".join(character for character in expected if character.isdigit())
            return bool(expected_digits and expected_digits in actual_digits)
        return actual_key in expected_key or expected_key in actual_key

    def _canonical_decimal(self, value: Decimal) -> str:
        normalized = format(value, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return normalized or "0"

    def _flight(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
        search_url_contract: TrustedSearchUrlContract | None,
        *,
        party_price_comparisons: tuple[FlightPartyComparisonReceipt, ...],
    ) -> NormalizedFlightQuote:
        if query.origin is None or query.end_date is None:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "round-trip flight normalization requires origin and return date",
                field="query",
            )
        if quote.price_basis not in {
            QuotePriceBasis.PER_PERSON,
            QuotePriceBasis.TOTAL_PARTY,
        }:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "flight quote must be per-person or total-party",
                field="price_basis",
            )
        outbound_depart = self._datetime(quote.details, "outbound_departure_at")
        outbound_arrive = self._datetime(quote.details, "outbound_arrival_at")
        return_depart = self._datetime(quote.details, "return_departure_at")
        return_arrive = self._datetime(quote.details, "return_arrival_at")
        if outbound_depart.date() != query.start_date or return_depart.date() != query.end_date:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight departure dates do not match the submitted search",
                field="flight_dates",
            )
        adults = self._context_int(quote.details, "adults", query.adults)
        children = self._context_int(quote.details, "children", query.children)
        infants = self._context_int(quote.details, "infants", query.infants)
        children_ages = self._context_ages(quote.details, query.children_ages)
        if (adults, children, infants) != (
            query.adults,
            query.children,
            query.infants,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight quote traveller shape differs from the submitted search",
                field="party",
            )
        if children_ages != query.children_ages:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight child ages differ from the submitted search",
                field="children_ages",
            )
        if (children or infants) and quote.price_basis != QuotePriceBasis.TOTAL_PARTY:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "mixed-party flight pricing requires an explicit total-party amount",
                field="price_basis",
            )
        self._flight_route_evidence(
            quote.details,
            "outbound_route_evidence",
            direction="outbound",
            expected_departure_name=query.origin,
            expected_departure_code=query.origin_code,
            expected_arrival_name=query.destination,
            expected_arrival_code=query.destination_code,
        )
        self._flight_route_evidence(
            quote.details,
            "return_route_evidence",
            direction="return",
            expected_departure_name=query.destination,
            expected_departure_code=query.destination_code,
            expected_arrival_name=query.origin,
            expected_arrival_code=query.origin_code,
        )
        price_text = self._str(quote.details, "price_text")
        qunar_party_comparison_payload = quote.details.get("party_price_comparison")
        qunar_party_comparison = (
            quote.provider == BrowserProvider.QUNAR
            and self._valid_qunar_party_comparison(
                qunar_party_comparison_payload,
                query=query,
                quote=quote,
            )
        )
        party_comparison = next(
            (
                receipt
                for receipt in party_price_comparisons
                if self._valid_party_comparison_receipt(
                    receipt,
                    query=query,
                    quote=quote,
                )
            ),
            None,
        )
        provider_has_explicit_basis = quote.provider not in {
            BrowserProvider.QUNAR,
            BrowserProvider.TONGCHENG,
        } and quote.price_basis in {
            QuotePriceBasis.PER_PERSON,
            QuotePriceBasis.TOTAL_PARTY,
        }
        single_adult_total = adults == 1 and children == 0 and infants == 0
        party_total_known = bool(
            provider_has_explicit_basis
            or qunar_party_comparison
            or party_comparison is not None
            or single_adult_total
        )
        requires_party_total_label = quote.price_basis == QuotePriceBasis.TOTAL_PARTY
        # A visible "含税总价" on a Qunar/Tongcheng round-trip card is useful
        # route evidence, but it does not identify whether the amount is for
        # one traveller or the requested party.  Preserve that observation
        # and downgrade it instead of dropping the complete itinerary.  Other
        # providers keep the strict explicit-party contract.
        if requires_party_total_label and quote.provider not in {
            BrowserProvider.QUNAR,
            BrowserProvider.TONGCHENG,
        }:
            party_pattern = (
                rf"(?:全部|全体|所有|订单|旅客|乘客|{adults}\s*(?:名|位)?成人|"
                rf"{adults}\s*人)"
            )
            if not (
                re.search(
                    rf"{party_pattern}[^¥￥$]{{0,18}}(?:总价|合计)",
                    price_text,
                    re.IGNORECASE,
                )
                or re.search(
                    rf"(?:总价|合计)[^¥￥$]{{0,18}}{party_pattern}",
                    price_text,
                    re.IGNORECASE,
                )
            ):
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                    "flight total price does not explicitly identify the requested party",
                    field="price_basis",
                )
        if provider_has_explicit_basis:
            calculation_price_basis = quote.price_basis
            effective_price_basis = quote.price_basis.value
        elif party_comparison is not None:
            calculation_price_basis = (
                QuotePriceBasis.PER_PERSON
                if party_comparison.price_basis == "per_person"
                else QuotePriceBasis.TOTAL_PARTY
            )
            effective_price_basis = party_comparison.price_basis
        elif single_adult_total:
            calculation_price_basis = QuotePriceBasis.PER_PERSON
            effective_price_basis = QuotePriceBasis.PER_PERSON.value
        elif party_total_known:
            calculation_price_basis = QuotePriceBasis.TOTAL_PARTY
            effective_price_basis = QuotePriceBasis.TOTAL_PARTY.value
        else:
            calculation_price_basis = QuotePriceBasis.PER_PERSON
            effective_price_basis = "comparison_only"
        total: int | None
        if party_comparison is not None:
            total = party_comparison.total_for_party_cents
        elif qunar_party_comparison:
            assert isinstance(qunar_party_comparison_payload, dict)
            total = cast(int, qunar_party_comparison_payload["two_adult_amount"])
        else:
            total = (
                self._total_cents(
                    quote.amount,
                    calculation_price_basis,
                    per_person_multiplier=adults,
                    per_night_multiplier=None,
                )
                if party_total_known
                else None
            )
        baggage = self._optional_int(quote.details, "checked_baggage_per_adult_kg")
        if baggage is not None:
            self._str(quote.details, "baggage_text")
            if not 0 <= baggage <= 100:
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    "checked baggage kilograms must be between 0 and 100",
                    field="checked_baggage_per_adult_kg",
                )
        party_availability_confirmed = (
            self._str(quote.details, "party_availability_status") == "confirmed_for_party"
            or party_comparison is not None
        )
        if (
            search_url_contract is not None
            and not search_url_contract.party_availability_confirmed
            and party_availability_confirmed
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight party availability status disagrees with the trusted URL contract",
                field="party_availability_status",
            )
        evidence_refs = list(self._evidence_refs(quote))
        if party_comparison is not None:
            comparison_sha256 = flight_party_comparison_receipt_sha256(party_comparison)
            evidence_refs.extend(
                (
                    f"flight-party-comparison:sha256:{comparison_sha256}",
                    f"browser-task:{party_comparison.one_adult.task_id}",
                    f"browser:{quote.provider.value}:sha256:"
                    f"{party_comparison.one_adult.evidence_sha256}",
                    f"browser-task:{party_comparison.requested_party.task_id}",
                    "price-scope:derived-comparison-not-settlement-lock",
                    "flight-segments:summary-only-not-expanded",
                )
            )
        return NormalizedFlightQuote(
            id=self._quote_id(quote),
            provider=quote.provider.value,
            provider_offer_id=self._optional_str(quote.details, "provider_offer_id"),
            currency=quote.currency,
            total_for_party_cents=total,
            taxes_and_fees_included=True,
            captured_at=quote.captured_at,
            expires_at=quote.captured_at + self._quote_ttl,
            availability=self._availability(quote.details),
            evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            origin=query.origin,
            destination=query.destination,
            adults=adults,
            children=children,
            children_ages=children_ages,
            infants=infants,
            party_availability_confirmed=party_availability_confirmed,
            party_total_known=party_total_known,
            price_basis=effective_price_basis,
            display_amount_cents=self._total_cents(
                quote.amount,
                QuotePriceBasis.PER_PERSON,
                per_person_multiplier=1,
                per_night_multiplier=None,
            ),
            outbound_depart_at=outbound_depart,
            outbound_arrive_at=outbound_arrive,
            return_depart_at=return_depart,
            return_arrive_at=return_arrive,
            checked_baggage_per_adult_kg=baggage,
            provider_itinerary_id=self._optional_str(
                quote.details,
                "provider_itinerary_id",
            ),
            origin_airport_code=self._optional_iata_code(
                quote.details,
                "origin_airport_code",
            )
            or (party_comparison.origin_code if party_comparison is not None else None),
            destination_airport_code=self._optional_iata_code(
                quote.details,
                "destination_airport_code",
            )
            or (
                party_comparison.destination_code
                if party_comparison is not None
                else None
            ),
            outbound_flight_numbers=self._optional_string_tuple(
                quote.details,
                "outbound_flight_numbers",
            ),
            return_flight_numbers=self._optional_string_tuple(
                quote.details,
                "return_flight_numbers",
            ),
            outbound_segments=self._optional_flight_segments(
                quote.details,
                "outbound_segments",
            ),
            return_segments=self._optional_flight_segments(
                quote.details,
                "return_segments",
            ),
            outbound_ground_transfers=self._optional_ground_transfer_contracts(
                quote.details,
                "outbound_ground_transfers",
            ),
            return_ground_transfers=self._optional_ground_transfer_contracts(
                quote.details,
                "return_ground_transfers",
            ),
            carrier_summary=self._optional_str(quote.details, "carrier_text"),
            cabin_class=self._optional_str(quote.details, "cabin_class"),
            fare_basis_codes=self._optional_string_tuple(
                quote.details,
                "fare_basis_codes",
            ),
            fare_rule_summary=self._optional_str(
                quote.details,
                "fare_rule_summary",
            ),
        )

    def derive_flight_party_comparison_receipts(
        self,
        requested_party_snapshot: BrowserTaskSnapshot,
        one_adult_snapshot: BrowserTaskSnapshot,
    ) -> tuple[FlightPartyComparisonReceipt, ...]:
        """Derive strict same-product 1/N receipts without editing raw evidence."""

        requested_query = requested_party_snapshot.query
        one_query = one_adult_snapshot.query
        if (
            requested_party_snapshot.state != BrowserTaskState.SUCCEEDED
            or one_adult_snapshot.state != BrowserTaskState.SUCCEEDED
            or requested_party_snapshot.kind != BrowserVertical.FLIGHT
            or one_adult_snapshot.kind != BrowserVertical.FLIGHT
            or requested_party_snapshot.provider != one_adult_snapshot.provider
            or requested_query.adults <= 1
            or requested_query.children != 0
            or requested_query.infants != 0
            or one_query.adults != 1
            or one_query.children != 0
            or one_query.infants != 0
            or requested_query.origin != one_query.origin
            or requested_query.destination != one_query.destination
            or requested_query.origin_code is None
            or requested_query.destination_code is None
            or requested_query.origin_code != one_query.origin_code
            or requested_query.destination_code != one_query.destination_code
            or requested_query.start_date != one_query.start_date
            or requested_query.end_date is None
            or requested_query.end_date != one_query.end_date
            or requested_query.currency != one_query.currency
        ):
            return ()

        requested_rows = self._comparison_observation_rows(
            requested_party_snapshot,
        )
        one_rows = self._comparison_observation_rows(one_adult_snapshot)
        receipts: list[FlightPartyComparisonReceipt] = []
        for requested_quote, requested_normalized, requested_fingerprint in requested_rows:
            for one_quote, one_normalized, one_fingerprint in one_rows:
                if requested_fingerprint != one_fingerprint:
                    continue
                overlap_start = max(
                    requested_normalized.captured_at,
                    one_normalized.captured_at,
                )
                overlap_end = min(
                    requested_normalized.expires_at,
                    one_normalized.expires_at,
                )
                if overlap_end <= overlap_start:
                    continue
                requested_amount = self._literal_amount_cents(requested_quote)
                one_amount = self._literal_amount_cents(one_quote)
                if requested_amount is None or one_amount is None:
                    continue
                adults = requested_query.adults
                if requested_amount == one_amount:
                    price_basis = QuotePriceBasis.PER_PERSON.value
                    derivation_method = "equal_display_amounts_imply_per_adult"
                    total = one_amount * adults
                elif (
                    requested_amount == one_amount * adults
                    and self._explicit_requested_party_total_label(
                        requested_quote,
                        adults=adults,
                    )
                ):
                    price_basis = QuotePriceBasis.TOTAL_PARTY.value
                    derivation_method = "explicit_n_party_total"
                    total = requested_amount
                else:
                    continue
                capture_skew = abs(
                    (
                        requested_normalized.captured_at
                        - one_normalized.captured_at
                    ).total_seconds()
                )
                receipt = FlightPartyComparisonReceipt(
                    provider=requested_party_snapshot.provider,
                    currency=requested_query.currency,
                    origin_code=requested_query.origin_code,
                    destination_code=requested_query.destination_code,
                    start_date=requested_query.start_date,
                    end_date=requested_query.end_date,
                    requested_adults=adults,
                    same_product_fingerprint=requested_fingerprint,
                    outbound_flight_numbers=requested_normalized.outbound_flight_numbers,
                    return_flight_numbers=requested_normalized.return_flight_numbers,
                    outbound_times=(
                        requested_normalized.outbound_depart_at,
                        requested_normalized.outbound_arrive_at,
                    ),
                    return_times=(
                        requested_normalized.return_depart_at,
                        requested_normalized.return_arrive_at,
                    ),
                    price_basis=price_basis,
                    derivation_method=derivation_method,
                    display_amount_cents=requested_amount,
                    total_for_party_cents=total,
                    capture_skew_seconds=capture_skew,
                    validity_overlap_start=overlap_start,
                    validity_overlap_end=overlap_end,
                    one_adult=FlightPartyPriceObservation(
                        task_id=one_adult_snapshot.id,
                        evidence_sha256=one_quote.evidence_sha256,
                        adults=1,
                        amount_cents=one_amount,
                        captured_at=one_normalized.captured_at,
                        expires_at=one_normalized.expires_at,
                        same_product_fingerprint=one_fingerprint,
                        available_for_requested_adults=True,
                    ),
                    requested_party=FlightPartyPriceObservation(
                        task_id=requested_party_snapshot.id,
                        evidence_sha256=requested_quote.evidence_sha256,
                        adults=adults,
                        amount_cents=requested_amount,
                        captured_at=requested_normalized.captured_at,
                        expires_at=requested_normalized.expires_at,
                        same_product_fingerprint=requested_fingerprint,
                        available_for_requested_adults=True,
                    ),
                )
                receipts.append(receipt)
        return tuple(
            sorted(
                receipts,
                key=lambda item: (
                    item.total_for_party_cents,
                    item.same_product_fingerprint,
                ),
            )
        )

    def _comparison_observation_rows(
        self,
        snapshot: BrowserTaskSnapshot,
    ) -> tuple[tuple[BrowserQuote, NormalizedFlightQuote, str], ...]:
        rows: list[tuple[BrowserQuote, NormalizedFlightQuote, str]] = []
        for quote in snapshot.quotes:
            normalized_result = self.normalize(quote, snapshot.query)
            normalized = normalized_result.quote
            if (
                not normalized_result.usable
                or not isinstance(normalized, NormalizedFlightQuote)
                or normalized.availability != QuoteAvailability.AVAILABLE
                or quote.taxes_included is not True
                or not self._visible_party_availability_proves_count(
                    quote,
                    adults=snapshot.query.adults,
                )
            ):
                continue
            product_payload = self._same_flight_product_payload(
                quote,
                normalized,
                snapshot.query,
            )
            if product_payload is None:
                continue
            fingerprint = hashlib.sha256(
                json.dumps(
                    product_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            rows.append((quote, normalized, fingerprint))
        return tuple(rows)

    def _same_flight_product_payload(
        self,
        quote: BrowserQuote,
        normalized: NormalizedFlightQuote,
        query: BrowserSearchQuery,
    ) -> dict[str, JsonValue] | None:
        combination_id = quote.details.get("combination_id")
        if (
            not isinstance(combination_id, str)
            or not combination_id.strip()
            or not normalized.outbound_flight_numbers
            or not normalized.return_flight_numbers
            or normalized.outbound_ground_transfers
            or normalized.return_ground_transfers
            or query.origin_code is None
            or query.destination_code is None
            or not self._observed_route_endpoints_match(
                quote,
                query=query,
            )
            or self._observed_ground_transport_or_wrong_station(quote)
        ):
            return None
        optional_identity_fields = (
            "provider_itinerary_id",
            "cabin_class",
            "checked_baggage_per_adult_kg",
            "fare_basis_codes",
            "fare_rule_summary",
            "carrier_text",
            "baggage_text",
        )
        optional_identity: dict[str, JsonValue] = {}
        for field_name in optional_identity_fields:
            value = quote.details.get(field_name)
            if value is not None:
                optional_identity[field_name] = value
        return {
            "provider": quote.provider.value,
            "transport_mode": "flight",
            "origin_airport_code": query.origin_code,
            "destination_airport_code": query.destination_code,
            "combination_id": combination_id,
            "outbound_flight_numbers": list(normalized.outbound_flight_numbers),
            "return_flight_numbers": list(normalized.return_flight_numbers),
            "outbound_depart_at": normalized.outbound_depart_at.isoformat(),
            "outbound_arrive_at": normalized.outbound_arrive_at.isoformat(),
            "return_depart_at": normalized.return_depart_at.isoformat(),
            "return_arrive_at": normalized.return_arrive_at.isoformat(),
            "optional_identity": optional_identity,
        }

    def _visible_party_availability_proves_count(
        self,
        quote: BrowserQuote,
        *,
        adults: int,
    ) -> bool:
        driver = quote.details.get("driver")
        if not isinstance(driver, dict):
            return False
        confirmed = driver.get("confirmed_query")
        readback = driver.get("readback_query")
        if (
            driver.get("party_availability_confirmed") is not True
            or not isinstance(confirmed, dict)
            or not isinstance(readback, dict)
            or confirmed.get("adults") != adults
            or readback.get("adults") != adults
        ):
            return False
        if quote.details.get("party_availability_status") == "confirmed_for_party":
            return True
        availability_text = " ".join(
            self._comparison_visible_text(value)
            for value in (
                quote.details.get("availability_evidence"),
                quote.details.get("selection_evidence"),
                quote.details.get("return_route_evidence"),
            )
        )
        visible_counts = tuple(
            int(match.group(1))
            for match in re.finditer(r"余\s*(\d+)\s*张", availability_text)
        )
        return bool(visible_counts and max(visible_counts) >= adults)

    def _observed_route_endpoints_match(
        self,
        quote: BrowserQuote,
        *,
        query: BrowserSearchQuery,
    ) -> bool:
        if query.origin_code is None or query.destination_code is None:
            return False
        aliases = {
            "HGH": ("HGH", "杭州萧山", "萧山国际机场", "萧山机场"),
            "MLE": ("MLE", "韦拉纳", "维拉纳", "马累"),
        }

        def endpoint_matches(route: object, *, departure: str, arrival: str) -> bool:
            if not isinstance(route, dict):
                return False
            if (
                route.get("departure_matches_requested") is not True
                or route.get("arrival_matches_requested") is not True
                or route.get("direction_order_confirmed") is not True
                or route.get("matches_expected") is not True
            ):
                return False
            for key, expected in (
                ("observed_departure_code", departure),
                ("observed_arrival_code", arrival),
            ):
                observed = route.get(key)
                if observed is not None and observed != expected:
                    return False
            departure_text = " ".join(
                str(route.get(key, ""))
                for key in (
                    "observed_departure_code",
                    "observed_departure_label",
                    "visible_evidence",
                )
            )
            arrival_text = " ".join(
                str(route.get(key, ""))
                for key in (
                    "observed_arrival_code",
                    "observed_arrival_label",
                    "visible_evidence",
                )
            )
            return any(
                alias in departure_text
                for alias in aliases.get(departure, (departure,))
            ) and any(
                alias in arrival_text
                for alias in aliases.get(arrival, (arrival,))
            )

        return endpoint_matches(
            quote.details.get("outbound_route_evidence"),
            departure=query.origin_code,
            arrival=query.destination_code,
        ) and endpoint_matches(
            quote.details.get("return_route_evidence"),
            departure=query.destination_code,
            arrival=query.origin_code,
        )

    def _observed_ground_transport_or_wrong_station(self, quote: BrowserQuote) -> bool:
        scoped_text = " ".join(
            self._comparison_visible_text(value)
            for value in (
                quote.details.get("outbound_route_evidence"),
                quote.details.get("return_route_evidence"),
                quote.details.get("selection_evidence"),
                quote.details.get("connection_text"),
                quote.details.get("outbound_leg"),
                quote.details.get("return_leg"),
                quote.details.get("outbound_segments"),
                quote.details.get("return_segments"),
            )
        )
        return bool(
            re.search(
                r"(?:\bHZD\b|\bHHL\b|巴士|汽车|地面联运|火车|高铁|\bbus\b|\bcoach\b|\brail\b)",
                scoped_text,
                re.IGNORECASE,
            )
        )

    def _comparison_visible_text(self, value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return ""

    def _literal_amount_cents(self, quote: BrowserQuote) -> int | None:
        scaled = quote.amount * Decimal(100)
        integral = scaled.to_integral_value()
        if scaled != integral or integral <= 0:
            return None
        return int(integral)

    def _explicit_requested_party_total_label(
        self,
        quote: BrowserQuote,
        *,
        adults: int,
    ) -> bool:
        price_text = quote.details.get("price_text")
        if not isinstance(price_text, str):
            return False
        party_pattern = rf"(?:{adults}\s*(?:名|位)?成人|{adults}\s*人|全部|全体|所有旅客)"
        return bool(
            re.search(
                rf"{party_pattern}[^\u00a5￥$]{{0,18}}(?:含税)?(?:总价|合计)",
                price_text,
                re.IGNORECASE,
            )
            or re.search(
                rf"(?:含税)?(?:总价|合计)[^\u00a5￥$]{{0,18}}{party_pattern}",
                price_text,
                re.IGNORECASE,
            )
        )

    def _valid_party_comparison_receipt(
        self,
        receipt: FlightPartyComparisonReceipt,
        *,
        query: BrowserSearchQuery,
        quote: BrowserQuote,
    ) -> bool:
        return not (
            query.end_date is None
            or query.children != 0
            or query.infants != 0
            or receipt.provider != quote.provider
            or receipt.currency != quote.currency
            or receipt.origin_code != query.origin_code
            or receipt.destination_code != query.destination_code
            or receipt.start_date != query.start_date
            or receipt.end_date != query.end_date
            or receipt.requested_adults != query.adults
            or receipt.requested_party.evidence_sha256 != quote.evidence_sha256
            or receipt.requested_party.amount_cents != self._literal_amount_cents(quote)
            or receipt.settlement_locked
            or receipt.inventory_locked
        )

    def _valid_qunar_party_comparison(
        self,
        comparison: object,
        *,
        query: BrowserSearchQuery,
        quote: BrowserQuote,
    ) -> bool:
        """Require an independent, same-product 1/2-adult price receipt.

        A search URL's adult query parameter and a visible ``含税总价`` label
        are not enough to establish whether the amount is per traveller or
        for the party.  This contract is deliberately strict so an adapter
        cannot manufacture a party total from its own URL.
        """
        if not isinstance(comparison, dict):
            return False
        if comparison.get("schema") != "tripchord.flight_party_comparison.v1":
            return False
        if comparison.get("verification") != "server_owned_same_product":
            return False
        if comparison.get("provider") != quote.provider.value:
            return False
        if comparison.get("currency") != quote.currency:
            return False
        if comparison.get("start_date") != query.start_date.isoformat():
            return False
        if query.end_date is None or comparison.get("end_date") != query.end_date.isoformat():
            return False
        if comparison.get("origin_code") != query.origin_code:
            return False
        if comparison.get("destination_code") != query.destination_code:
            return False
        product_id = comparison.get("same_product_id")
        if not isinstance(product_id, str) or not product_id.strip():
            return False
        one = comparison.get("one_adult")
        two = comparison.get("two_adults")
        if not isinstance(one, dict) or not isinstance(two, dict):
            return False
        if one.get("adults") != 1 or two.get("adults") != 2:
            return False
        for row in (one, two):
            if not isinstance(row.get("amount"), int) or row["amount"] <= 0:
                return False
            if row.get("same_product_id") != product_id:
                return False
            if row.get("query_hash") != comparison.get("query_hash"):
                return False
        return comparison.get("two_adult_amount") == two.get("amount")

    def _flight_route_evidence(
        self,
        details: dict[str, JsonValue],
        key: str,
        *,
        direction: str,
        expected_departure_name: str,
        expected_departure_code: str | None,
        expected_arrival_name: str,
        expected_arrival_code: str | None,
    ) -> None:
        raw = details.get(key)
        if not isinstance(raw, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight quote lacks structured visible route evidence",
                field=key,
            )
        if (
            raw.get("direction") != direction
            or raw.get("matches_expected") is not True
            or raw.get("departure_matches_requested") is not True
            or raw.get("arrival_matches_requested") is not True
            or raw.get("direction_order_confirmed") is not True
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                f"flight {direction} route evidence does not match the requested direction",
                field=key,
            )
        source_scope = raw.get("source_scope")
        allowed_scopes = (
            {"selected_outbound_summary", "outbound_candidate_card"}
            if direction == "outbound"
            else {"return_card"}
        ) | {"combined_card_leg"}
        if not isinstance(source_scope, str) or source_scope not in allowed_scopes:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight route evidence must come from a visible leg or selected-flight card",
                field=f"{key}.source_scope",
            )
        if (
            raw.get("expected_departure_code") != expected_departure_code
            or raw.get("expected_arrival_code") != expected_arrival_code
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight route evidence codes differ from the submitted search",
                field=key,
            )
        departure_label = raw.get("observed_departure_label")
        arrival_label = raw.get("observed_arrival_label")
        visible_evidence = raw.get("visible_evidence")
        if (
            not isinstance(departure_label, str)
            or not departure_label.strip()
            or not isinstance(arrival_label, str)
            or not arrival_label.strip()
            or not isinstance(visible_evidence, str)
            or not visible_evidence.strip()
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "flight route evidence requires visible departure and arrival labels",
                field=key,
            )
        if not self._flight_location_matches(
            departure_label,
            expected_departure_name,
            expected_departure_code,
        ) or not self._flight_location_matches(
            arrival_label,
            expected_arrival_name,
            expected_arrival_code,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight visible route labels differ from the submitted search",
                field=key,
            )
        normalized_evidence = self._normalized_location_text(visible_evidence)
        if (
            self._normalized_location_text(departure_label) not in normalized_evidence
            or self._normalized_location_text(arrival_label) not in normalized_evidence
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "flight route labels are not present in the sealed visible evidence",
                field=key,
            )

    def _flight_location_matches(
        self,
        observed: str,
        expected_name: str,
        expected_code: str | None,
    ) -> bool:
        observed_key = self._normalized_location_text(observed)
        aliases = {
            self._normalized_location_text(expected_name),
            self._normalized_location_text(expected_code or ""),
        }
        audited = {
            "HGH": {"杭州", "杭州萧山", "萧山", "hangzhou", "hgh"},
            # Chinese OTAs use both transliterations for Velana International
            # Airport. They refer to the same audited MLE endpoint.
            "MLE": {"马累", "维拉纳", "韦拉纳", "male", "mle"},
        }
        aliases.update(
            self._normalized_location_text(value)
            for value in audited.get((expected_code or "").upper(), set())
        )
        aliases.discard("")
        return any(alias in observed_key or observed_key in alias for alias in aliases)

    def _normalized_location_text(self, value: str) -> str:
        return "".join(character.lower() for character in value if character.isalnum())

    def _lodging_location_evidence(
        self,
        *,
        area_text: str,
        area_source: str,
        driver: object,
    ) -> tuple[str | None, tuple[str, ...], LodgingLocationConvenience]:
        """Project only visible address/proximity evidence from the same quote.

        Exact search-place binding is deliberately not consulted: it proves the
        requested island, not whether the property is remote within that island.
        """

        address: str | None = None
        if area_source == "visible_label":
            cleaned_address = re.sub(r"(?:显示|查看)地图\s*$", "", area_text).strip(" ,，")
            if re.search(
                r"\b(?:road|street|avenue|lane|drive|boulevard|highway|magu|hingun)\b|"
                r"(?:路|街|道|大道|巷|弄|号)",
                cleaned_address,
                re.IGNORECASE,
            ):
                address = cleaned_address

        nearby: list[str] = []
        if isinstance(driver, dict):
            detail_capture = driver.get("detail_capture")
            if isinstance(detail_capture, dict):
                raw_evidence = detail_capture.get("preview_location_evidence")
                if isinstance(raw_evidence, list):
                    for item in raw_evidence:
                        if not isinstance(item, str):
                            continue
                        cleaned = re.sub(r"(?:显示|查看)地图\s*$", "", item).strip()
                        if cleaned and cleaned not in nearby and len(cleaned) <= 1000:
                            nearby.append(cleaned)
                        if len(nearby) == 8:
                            break

        nearby_evidence = tuple(nearby)
        convenience = (
            LodgingLocationConvenience.CONFIRMED_NOT_REMOTE
            if lodging_non_remote_evidence_confirmed(address, nearby_evidence)
            else LodgingLocationConvenience.UNKNOWN
        )
        return address, nearby_evidence, convenience

    def _lodging(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
    ) -> NormalizedLodgingQuote:
        if query.end_date is None:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging normalization requires checkout date",
                field="query.end_date",
            )
        if quote.price_basis not in {
            QuotePriceBasis.PER_NIGHT,
            QuotePriceBasis.TOTAL_STAY,
        }:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "lodging quote must be per-night or total-stay",
                field="price_basis",
            )
        check_in = self._optional_date(quote.details, "check_in") or query.start_date
        check_out = self._optional_date(quote.details, "check_out") or query.end_date
        if check_in < query.start_date or check_out > query.end_date or check_out <= check_in:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging stay falls outside the submitted trip window",
                field="stay_dates",
            )
        adults = self._context_int(quote.details, "adults", query.adults)
        children = self._context_int(quote.details, "children", query.children)
        infants = self._context_int(quote.details, "infants", query.infants)
        children_ages = self._context_ages(quote.details, query.children_ages)
        rooms = self._context_int(quote.details, "rooms", query.rooms)
        if (adults, children, infants, rooms) != (
            query.adults,
            query.children,
            query.infants,
            query.rooms,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging occupancy differs from the submitted search",
                field="occupancy",
            )
        if children_ages != query.children_ages:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging child ages differ from the submitted search",
                field="children_ages",
            )
        nights = (check_out - check_in).days
        total = self._total_cents(
            quote.amount,
            quote.price_basis,
            per_person_multiplier=None,
            per_night_multiplier=nights * rooms,
        )
        area = self._area(quote.details, "area")
        area_text = self._str(quote.details, "area_text")
        area_source = self._str(quote.details, "area_source")
        expected_area = self._expected_package_area(query)
        place_key = self._expected_lodging_place_key(query)
        driver = quote.details.get("driver")
        qunar_exact_visible_area = (
            quote.provider == BrowserProvider.QUNAR
            and isinstance(driver, dict)
            and driver.get("mode") == "captured_read_only_detail"
            and quote.details.get("extraction")
            == "visible_dom_qunar_lodging_detail"
            and area_source == "exact_visible_maafushi_kaafu"
        )
        if area_source not in {
            "visible_label",
            "confirmed_exact_search_area",
        } and not qunar_exact_visible_area:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "lodging area must come from visible or confirmed exact-area evidence",
                field="area_source",
            )
        if area_source == "confirmed_exact_search_area":
            self._validate_confirmed_area(
                quote.details,
                query,
                area_text,
                expected_area,
            )
        if expected_area is not None and area != expected_area:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "visible lodging area does not match the task's expected package area",
                field="area",
            )
        breakfast = self._optional_bool(quote.details, "breakfast_included")
        if breakfast is not None:
            self._str(quote.details, "breakfast_text")
        location_address, nearby_location_evidence, location_convenience = (
            self._lodging_location_evidence(
                area_text=area_text,
                area_source=area_source,
                driver=driver,
            )
        )
        return NormalizedLodgingQuote(
            id=self._quote_id(quote),
            provider=quote.provider.value,
            provider_offer_id=self._optional_str(quote.details, "provider_offer_id"),
            currency=quote.currency,
            total_for_party_cents=total,
            taxes_and_fees_included=True,
            captured_at=quote.captured_at,
            expires_at=quote.captured_at + self._quote_ttl,
            availability=self._availability(quote.details),
            evidence_refs=self._evidence_refs(quote),
            property_name=quote.title,
            area=area,
            check_in=check_in,
            check_out=check_out,
            adults=adults,
            children=children,
            children_ages=children_ages,
            infants=infants,
            rooms=rooms,
            breakfast_included=breakfast,
            place_key=place_key,
            provider_property_id=self._optional_str(quote.details, "property_id"),
            provider_room_id=self._optional_str(quote.details, "room_id"),
            provider_rate_plan_id=self._optional_str(quote.details, "rate_plan_id"),
            room_name=self._optional_str(quote.details, "room_text"),
            bed_type=self._optional_str(quote.details, "bed_text"),
            cancellation_policy=self._optional_str(
                quote.details,
                "cancellation_text",
            ),
            payment_policy=self._visible_payment_policy(quote.details),
            location_address=location_address,
            nearby_location_evidence=nearby_location_evidence,
            location_convenience=location_convenience,
        )

    def _transfers(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
    ) -> tuple[tuple[TransferOption, ...], tuple[QuoteNormalizationIssue, ...]]:
        raw = quote.details.get("transfers")
        if raw is None:
            transfer_text = quote.details.get("transfer_text")
            detail_status = quote.details.get("transfer_detail_status")
            if (
                isinstance(transfer_text, str) and transfer_text.strip()
            ) or detail_status == "missing_explicit_contract":
                return (
                    (),
                    (
                        QuoteNormalizationIssue(
                            code=QuoteNormalizationCode.INVALID_TRANSFER,
                            message=(
                                "visible page mentions a transfer but does not expose a "
                                "complete price, tax, direction, and schedule contract"
                            ),
                            field="transfers",
                            scope="transfer",
                        ),
                    ),
                )
            return (), ()
        if not isinstance(raw, list):
            return (
                (),
                (
                    QuoteNormalizationIssue(
                        code=QuoteNormalizationCode.INVALID_TRANSFER,
                        message="lodging transfer details must be a list",
                        field="transfers",
                        scope="transfer",
                    ),
                ),
            )
        if not raw:
            transfer_text = quote.details.get("transfer_text")
            detail_status = quote.details.get("transfer_detail_status")
            if (isinstance(transfer_text, str) and transfer_text.strip()) or (
                isinstance(detail_status, str) and detail_status
            ):
                return (
                    (),
                    (
                        QuoteNormalizationIssue(
                            code=QuoteNormalizationCode.INVALID_TRANSFER,
                            message=(
                                "transfer evidence is incomplete: explicit direction, "
                                "price, taxes, duration, and schedule are required"
                            ),
                            field="transfers",
                            scope="transfer",
                        ),
                    ),
                )
            return (), ()
        transfers: list[TransferOption] = []
        issues: list[QuoteNormalizationIssue] = []
        for index, item in enumerate(raw):
            scope = f"transfer[{index}]"
            if not isinstance(item, dict):
                issues.append(
                    QuoteNormalizationIssue(
                        code=QuoteNormalizationCode.INVALID_TRANSFER,
                        message="transfer entry must be an object",
                        field=scope,
                        scope="transfer",
                    )
                )
                continue
            try:
                transfers.append(self._transfer(quote, query, item, index))
            except _RejectNormalization as exc:
                issues.append(
                    exc.issue.model_copy(
                        update={
                            "code": QuoteNormalizationCode.INVALID_TRANSFER,
                            "scope": scope,
                        }
                    )
                )
        return tuple(transfers), tuple(issues)

    def _transfer(
        self,
        quote: BrowserQuote,
        query: BrowserSearchQuery,
        details: dict[str, JsonValue],
        index: int,
    ) -> TransferOption:
        currency = self._str(details, "currency")
        if currency.upper() != query.currency:
            raise _RejectNormalization(
                QuoteNormalizationCode.CURRENCY_MISMATCH,
                "transfer currency differs from the requested comparison currency",
                field="currency",
                scope="transfer",
            )
        if self._required_bool(details, "taxes_included") is not True:
            raise _RejectNormalization(
                QuoteNormalizationCode.TAXES_INCOMPLETE,
                "transfer taxes and fees are not confirmed as included",
                field="taxes_included",
                scope="transfer",
            )
        tax_evidence = self._str(details, "tax_evidence")
        if re.search(
            r"未含税|不含税|税费另付|tax(?:es)?\s+(?:not\s+included|excluded)",
            tax_evidence,
            re.IGNORECASE,
        ) or not re.search(
            r"含税|税费已含|tax(?:es)?\s+included|all\s+tax(?:es)?",
            tax_evidence,
            re.IGNORECASE,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.TAXES_INCOMPLETE,
                "transfer tax inclusion is not backed by explicit visible text",
                field="tax_evidence",
                scope="transfer",
            )
        basis_text = self._str(details, "price_basis")
        try:
            basis = QuotePriceBasis(basis_text)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "transfer price basis is unknown",
                field="price_basis",
                scope="transfer",
            ) from exc
        if basis not in {QuotePriceBasis.PER_PERSON, QuotePriceBasis.TOTAL_PARTY}:
            raise _RejectNormalization(
                QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                "transfer quote must be per-person or total-party",
                field="price_basis",
                scope="transfer",
            )
        price_scope_text = self._str(details, "price_scope")
        try:
            price_scope = TransferPriceScope(price_scope_text)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer price scope must be one_way or round_trip",
                field="price_scope",
                scope="transfer",
            ) from exc
        price_evidence = self._str(details, "price_evidence")
        direction_evidence = self._str(details, "direction_evidence")
        basis_pattern = (
            r"每人|/人|成人|per\s+(?:person|adult)"
            if basis == QuotePriceBasis.PER_PERSON
            else r"总价|合计|全程|total(?:\s+party)?"
        )
        if not re.search(basis_pattern, price_evidence, re.IGNORECASE):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer price basis is not backed by explicit visible text",
                field="price_evidence",
                scope="transfer",
            )
        if price_scope == TransferPriceScope.ROUND_TRIP:
            direction_pattern = r"往返|双向|round[\s-]?trip|return\s+transfer|↔|⇄"
        else:
            direction_pattern = r"单程|one[\s-]?way|→|->|(?:至|到)"
        if not re.search(direction_pattern, direction_evidence, re.IGNORECASE):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer direction is not backed by explicit visible text",
                field="direction_evidence",
                scope="transfer",
            )
        amount = self._decimal(details, "amount")
        total = self._total_cents(
            amount,
            basis,
            per_person_multiplier=query.adults,
            per_night_multiplier=None,
        )
        price_contract_key = self._str(details, "price_contract_key")
        evidence_text = self._str(details, "evidence_text")
        detail_url = self._transfer_detail_url(quote, details)
        evidence_sha256 = self._sha256(details, "evidence_sha256")
        duration_minutes = self._int(details, "duration_minutes")
        if not 0 < duration_minutes <= 1440:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer duration_minutes must be between 1 and 1440",
                field="duration_minutes",
                scope="transfer",
            )
        service_date = self._date(details, "service_date")
        if (
            query.end_date is None
            or service_date < query.start_date
            or service_date > query.end_date
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "transfer service date falls outside the submitted lodging search",
                field="service_date",
                scope="transfer",
            )
        schedule_mode_text = self._str(details, "schedule_mode")
        try:
            schedule_mode = TransferScheduleMode(schedule_mode_text)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer schedule mode must be exact_departure or service_window",
                field="schedule_mode",
                scope="transfer",
            ) from exc
        schedule_evidence = self._str(details, "schedule_evidence")
        operates_24_hours = self._required_bool(details, "operates_24_hours")
        if operates_24_hours and not re.search(
            r"24\s*(?:小时|h)|24\s*/\s*7|全天",
            schedule_evidence,
            re.IGNORECASE,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "24-hour service is not backed by explicit visible text",
                field="schedule_evidence",
                scope="transfer",
            )
        requires_reservation = self._optional_bool(details, "requires_reservation")
        purchase_scope_text = self._str(details, "purchase_scope")
        try:
            purchase_scope = TransferPurchaseScope(purchase_scope_text)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer purchase scope must be hotel_bound or public_independent",
                field="purchase_scope",
                scope="transfer",
            ) from exc
        purchase_scope_evidence = self._str(details, "purchase_scope_evidence")
        if purchase_scope == TransferPurchaseScope.PUBLIC_INDEPENDENT and not re.search(
            (
                r"可(?:单独|独立)预订|无需入住|非住客可订|公共接驳|"
                r"independent(?:ly)?\s+book|no\s+hotel\s+stay|required\s+stay:\s*no|"
                r"public\s+transfer"
            ),
            purchase_scope_evidence,
            re.IGNORECASE,
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "public transfer scope lacks explicit independent-purchase evidence",
                field="purchase_scope_evidence",
                scope="transfer",
            )
        source_lodging_id = self._quote_id(quote)
        bound_lodging_id = (
            source_lodging_id if purchase_scope == TransferPurchaseScope.HOTEL_BOUND else None
        )
        exact_depart_at: datetime | None = None
        exact_arrive_at: datetime | None = None
        service_window_start_at: datetime | None = None
        service_window_end_at: datetime | None = None
        if schedule_mode == TransferScheduleMode.EXACT_DEPARTURE:
            exact_depart_at = self._datetime(details, "depart_at")
            exact_arrive_at = self._datetime(details, "arrive_at")
        else:
            service_window_start_at = self._datetime(
                details,
                "service_window_start_at",
            )
            service_window_end_at = self._datetime(
                details,
                "service_window_end_at",
            )
        service_identity = (
            f"{query.start_date.isoformat()}<->{query.end_date.isoformat()}"
            if price_scope == TransferPriceScope.ROUND_TRIP
            else service_date.isoformat()
        )
        segment = query.options.get("segment")
        segment_identity = segment if isinstance(segment, str) else ""
        contract_digest = hashlib.sha256(
            (
                f"{quote.provider.value}|{source_lodging_id}|{price_contract_key}|"
                f"{detail_url}|{evidence_sha256}|{price_scope.value}|"
                f"{currency.upper()}|{amount}|{basis.value}|"
                f"{service_identity}|{segment_identity}|{purchase_scope.value}"
            ).encode()
        ).hexdigest()[:20]
        digest = self._component_digest(quote, f"transfer:{index}", details)
        origin_area = self._area(details, "origin_area")
        destination_area = self._area(details, "destination_area")
        origin_place_key, destination_place_key = self._transfer_place_keys(
            query,
            origin_area,
            destination_area,
        )
        try:
            return TransferOption(
                id=f"browser:{quote.provider.value}:transfer:{digest}",
                provider=quote.provider.value,
                currency=currency.upper(),
                total_for_party_cents=total,
                taxes_and_fees_included=True,
                captured_at=quote.captured_at,
                expires_at=quote.captured_at + self._quote_ttl,
                availability=self._availability(details),
                evidence_refs=(
                    *self._evidence_refs(quote),
                    f"browser:{quote.provider.value}:transfer:sha256:{evidence_sha256}",
                    detail_url,
                ),
                origin_area=origin_area,
                destination_area=destination_area,
                origin_place_key=origin_place_key,
                destination_place_key=destination_place_key,
                adults=query.adults,
                children=query.children,
                infants=query.infants,
                service_date=service_date,
                schedule_mode=schedule_mode,
                duration_minutes=duration_minutes,
                depart_at=exact_depart_at,
                arrive_at=exact_arrive_at,
                service_window_start_at=service_window_start_at,
                service_window_end_at=service_window_end_at,
                operates_24_hours=operates_24_hours,
                requires_reservation=requires_reservation,
                price_scope=price_scope,
                price_contract_id=(
                    f"browser:{quote.provider.value}:transfer-price:{contract_digest}"
                ),
                purchase_scope=purchase_scope,
                bound_lodging_id=bound_lodging_id,
                contract_evidence_text=evidence_text,
                detail_url=detail_url,
            )
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"transfer contract is internally inconsistent: {exc}",
                field="transfers",
                scope="transfer",
            ) from exc

    @staticmethod
    def _transfer_place_keys(
        query: BrowserSearchQuery,
        origin_area: PackageArea,
        destination_area: PackageArea,
    ) -> tuple[PackagePlaceKey | None, PackagePlaceKey | None]:
        """Bind a visible route to the exact place frozen by its lodging query.

        ``airport`` is uniquely Velana in the supported package contract.  The
        non-airport endpoint is bound only when it equals both the query's
        frozen package area and frozen lodging place; unrelated route areas stay
        unknown instead of being guessed.
        """

        raw_place = query.options.get("expected_lodging_place_key")
        raw_area = query.options.get("expected_package_area")
        try:
            expected_place = PackagePlaceKey(str(raw_place))
            expected_area = PackageArea(str(raw_area))
        except ValueError:
            return None, None

        canonical_area_by_place = {
            PackagePlaceKey.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
            PackagePlaceKey.HULHUMALE: PackageArea.AIRPORT_ISLAND,
        }
        if canonical_area_by_place.get(expected_place) != expected_area:
            return None, None

        def resolve(area: PackageArea) -> PackagePlaceKey | None:
            if area == PackageArea.AIRPORT:
                return PackagePlaceKey.VELANA_AIRPORT
            if area == expected_area:
                return expected_place
            return None

        return resolve(origin_area), resolve(destination_area)

    def _total_cents(
        self,
        amount: Decimal,
        basis: QuotePriceBasis,
        *,
        per_person_multiplier: int | None,
        per_night_multiplier: int | None,
    ) -> int:
        multiplier = 1
        if basis == QuotePriceBasis.PER_PERSON:
            if per_person_multiplier is None:
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                    "per-person price has no deterministic party multiplier",
                    field="price_basis",
                )
            multiplier = per_person_multiplier
        elif basis == QuotePriceBasis.PER_NIGHT:
            if per_night_multiplier is None:
                raise _RejectNormalization(
                    QuoteNormalizationCode.UNSUPPORTED_PRICE_BASIS,
                    "per-night price has no deterministic night/room multiplier",
                    field="price_basis",
                )
            multiplier = per_night_multiplier
        scaled = amount * Decimal(multiplier) * Decimal(100)
        integral = scaled.to_integral_value()
        if scaled != integral:
            raise _RejectNormalization(
                QuoteNormalizationCode.NON_INTEGRAL_CENTS,
                "price cannot be represented exactly as integer minor units",
                field="amount",
            )
        return int(integral)

    def _quote_id(self, quote: BrowserQuote) -> str:
        digest = self._component_digest(quote, "primary", quote.details)
        return f"browser:{quote.provider.value}:{quote.kind.value}:{digest}"

    def _component_digest(
        self,
        quote: BrowserQuote,
        scope: str,
        details: dict[str, JsonValue],
    ) -> str:
        canonical = json.dumps(
            {
                "provider": quote.provider.value,
                "kind": quote.kind.value,
                "evidence": quote.evidence_sha256,
                "title": quote.title,
                "amount": str(quote.amount),
                "basis": quote.price_basis.value,
                "scope": scope,
                "details": details,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:20]

    def _evidence_refs(self, quote: BrowserQuote) -> tuple[str, ...]:
        return (
            f"browser:{quote.provider.value}:sha256:{quote.evidence_sha256}",
            quote.page_url,
        )

    def _availability(self, details: dict[str, JsonValue]) -> QuoteAvailability:
        if "availability" not in details:
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "availability must be backed by explicit provider evidence",
                field="availability",
            )
        value = details["availability"]
        if not isinstance(value, str):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "availability must be a string",
                field="availability",
            )
        try:
            return QuoteAvailability(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "availability must be available or sold_out",
                field="availability",
            ) from exc

    def _area(self, details: dict[str, JsonValue], key: str) -> PackageArea:
        value = self._str(details, key)
        try:
            return PackageArea(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} is not a supported package area",
                field=key,
            ) from exc

    def _date(self, details: dict[str, JsonValue], key: str) -> date:
        value = self._str(details, key)
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an ISO date",
                field=key,
                scope="transfer",
            ) from exc

    def _sha256(self, details: dict[str, JsonValue], key: str) -> str:
        value = self._str(details, key).lower()
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a lowercase hexadecimal SHA-256 digest",
                field=key,
                scope="transfer",
            )
        return value

    def _transfer_detail_url(
        self,
        quote: BrowserQuote,
        details: dict[str, JsonValue],
    ) -> str:
        value = self._str(details, "detail_url")
        parsed = urlparse(value)
        quote_url = urlparse(quote.page_url)
        forbidden = {"cashier", "checkout", "coupon", "order", "payment"}
        path_segments = {
            segment.lower()
            for segment in parsed.path.replace("-", "/").replace("_", "/").split("/")
            if segment
        }
        query_keys = {pair.partition("=")[0].lower() for pair in parsed.query.split("&") if pair}
        quote_host = (quote_url.hostname or "").lower()
        host = (parsed.hostname or "").lower()
        provider_root = {
            "ctrip": "ctrip.com",
            "fliggy": "fliggy.com",
            "qunar": "qunar.com",
            "tongcheng": "ly.com",
        }[quote.provider.value]
        if (
            parsed.scheme != "https"
            or not host
            or not quote_host
            or parsed.username is not None
            or parsed.password is not None
            or not (host == provider_root or host.endswith(f".{provider_root}"))
            or not (quote_host == provider_root or quote_host.endswith(f".{provider_root}"))
            or path_segments.intersection(forbidden)
            or query_keys.intersection(forbidden)
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "transfer detail URL is outside the read-only provider boundary",
                field="detail_url",
                scope="transfer",
            )
        return value

    def _expected_package_area(
        self,
        query: BrowserSearchQuery,
    ) -> PackageArea | None:
        value = query.options.get("expected_package_area")
        if value is None:
            return None
        if not isinstance(value, str):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "expected package area must be a string",
                field="query.options.expected_package_area",
            )
        try:
            area = PackageArea(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "expected package area is unsupported",
                field="query.options.expected_package_area",
            ) from exc
        if area == PackageArea.AIRPORT:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "lodging cannot use the airport terminal as its expected area",
                field="query.options.expected_package_area",
            )
        return area

    def _expected_lodging_place_key(
        self,
        query: BrowserSearchQuery,
    ) -> PackagePlaceKey | None:
        value = query.options.get("expected_lodging_place_key")
        if value is None:
            return None
        if not isinstance(value, str):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "expected lodging place key must be a string",
                field="query.options.expected_lodging_place_key",
            )
        try:
            place_key = PackagePlaceKey(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "expected lodging place key is unsupported",
                field="query.options.expected_lodging_place_key",
            ) from exc
        expected_area = {
            PackagePlaceKey.MAAFUSHI: PackageArea.DESTINATION_ISLAND,
            PackagePlaceKey.HULHUMALE: PackageArea.AIRPORT_ISLAND,
        }.get(place_key)
        expected_term = {
            PackagePlaceKey.MAAFUSHI: "Maafushi",
            PackagePlaceKey.HULHUMALE: "Hulhumalé",
        }.get(place_key)
        if expected_area is None or expected_term is None:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "lodging place key cannot identify an airport terminal",
                field="query.options.expected_lodging_place_key",
            )
        if self._expected_package_area(query) != expected_area:
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging place key does not match the expected package area",
                field="query.options.expected_lodging_place_key",
            )
        profile = query.options.get("stay_area_search_profile")
        if not isinstance(profile, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "lodging place key requires a structured stay-area search profile",
                field="query.options.stay_area_search_profile",
            )
        profile_term_key = (
            "destination_island_lodging_search_term"
            if place_key == PackagePlaceKey.MAAFUSHI
            else "airport_island_lodging_search_term"
        )
        if (
            profile.get("source") != "system_derived_golden"
            or profile.get(profile_term_key) != expected_term
            or query.destination != expected_term
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "lodging place key is not backed by the trusted exact search-area profile",
                field="query.options.expected_lodging_place_key",
            )
        return place_key

    def _validate_confirmed_area(
        self,
        details: dict[str, JsonValue],
        query: BrowserSearchQuery,
        area_text: str,
        expected_area: PackageArea | None,
    ) -> None:
        driver = details.get("driver")
        if not isinstance(driver, dict):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                "confirmed exact-area evidence requires driver metadata",
                field="driver",
            )
        confirmed = driver.get("confirmed_query")
        confirmed_destination = (
            confirmed.get("destination") if isinstance(confirmed, dict) else None
        )
        confirmation_scope = driver.get("confirmation_scope")
        if (
            expected_area is None
            or driver.get("triggered") is not True
            or not isinstance(confirmed_destination, str)
            or not isinstance(confirmation_scope, str)
            or confirmation_scope != "confirmed_visible_search"
            or not self._same_place(query.destination, confirmed_destination)
            or not self._same_place(area_text, confirmed_destination)
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.QUERY_CONTEXT_MISMATCH,
                "exact-area evidence is not confirmed by the visible search context",
                field="area_source",
            )

    def _same_place(self, left: str, right: str) -> bool:
        def key(value: str) -> str:
            normalized = "".join(character.lower() for character in value if character.isalnum())
            for suffix in ("island", "岛"):
                if normalized.endswith(suffix):
                    normalized = normalized[: -len(suffix)]
            return normalized

        first = key(left)
        second = key(right)
        return bool(
            first
            and second
            and (
                first == second
                or (min(len(first), len(second)) >= 4 and (first in second or second in first))
            )
        )

    def _datetime(self, details: dict[str, JsonValue], key: str) -> datetime:
        value = self._str(details, key)
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an ISO-8601 timestamp",
                field=key,
            ) from exc
        if parsed.tzinfo is None:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must include a timezone",
                field=key,
            )
        return parsed

    def _optional_date(self, details: dict[str, JsonValue], key: str) -> date | None:
        value = details.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an ISO date",
                field=key,
            )
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an ISO date",
                field=key,
            ) from exc

    def _str(self, details: dict[str, JsonValue], key: str) -> str:
        value = details.get(key)
        if not isinstance(value, str) or not value:
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                f"required field {key!r} is missing",
                field=key,
            )
        return value

    def _optional_str(self, details: dict[str, JsonValue], key: str) -> str | None:
        value = details.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a non-empty string",
                field=key,
            )
        return value

    def _optional_string_tuple(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> tuple[str, ...]:
        """Read an optional provider identity vector without inventing values.

        Some adapters expose a single flight/fare identifier while others expose
        one identifier per leg.  Both wire shapes are accepted, but every value
        must be explicit non-empty visible/provider data.
        """

        value = details.get(key)
        if value is None:
            return ()
        raw_values = [value] if isinstance(value, str) else value
        if not isinstance(raw_values, list) or not raw_values:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a non-empty string or list of non-empty strings",
                field=key,
            )
        normalized: list[str] = []
        for item in raw_values:
            if not isinstance(item, str) or not item.strip():
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    f"{key} must contain only non-empty strings",
                    field=key,
                )
            text = item.strip()
            if text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    def _optional_iata_code(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> str | None:
        value = self._optional_str(details, key)
        if value is None:
            return None
        normalized = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", normalized):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a three-letter IATA code",
                field=key,
            )
        return normalized

    def _optional_flight_segments(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> tuple[NormalizedFlightSegment, ...]:
        value = details.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not value:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a non-empty list of airport-level segments",
                field=key,
            )
        segments: list[NormalizedFlightSegment] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    f"{key}[{index}] must be an object",
                    field=key,
                )
            try:
                segment = NormalizedFlightSegment.model_validate(item)
            except ValueError as exc:
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    f"{key}[{index}] is not a complete airport-level segment",
                    field=key,
                ) from exc
            segments.append(segment)
        return tuple(segments)

    def _optional_ground_transfer_contracts(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> tuple[FlightGroundTransferContract, ...]:
        value = details.get(key)
        if value is None:
            return ()
        if not isinstance(value, list) or not value:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a non-empty list of airport-change contracts",
                field=key,
            )
        contracts: list[FlightGroundTransferContract] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    f"{key}[{index}] must be an object",
                    field=key,
                )
            try:
                contract = FlightGroundTransferContract.model_validate(item)
            except ValueError as exc:
                raise _RejectNormalization(
                    QuoteNormalizationCode.INVALID_FIELD,
                    f"{key}[{index}] is not a complete airport-change contract",
                    field=key,
                ) from exc
            contracts.append(contract)
        return tuple(contracts)

    def _visible_payment_policy(self, details: dict[str, JsonValue]) -> str | None:
        explicit = self._optional_str(details, "payment_text")
        if explicit is not None:
            return explicit
        rate_text = details.get("rate_text")
        if not isinstance(rate_text, str):
            return None
        for label, pattern in (
            ("online_prepay", r"在线付|立即付款|预付|prepay|pay\s+online"),
            ("pay_at_property", r"到店付|现付|pay\s+at\s+(?:hotel|property)"),
            ("card_guarantee", r"信用卡担保|card\s+guarantee"),
        ):
            if re.search(pattern, rate_text, re.IGNORECASE):
                return label
        return None

    def _int(self, details: dict[str, JsonValue], key: str) -> int:
        value = details.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                f"required integer field {key!r} is missing",
                field=key,
            )
        return value

    def _optional_int(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> int | None:
        value = details.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an integer or null",
                field=key,
            )
        return value

    def _context_int(
        self,
        details: dict[str, JsonValue],
        key: str,
        default: int,
    ) -> int:
        value = details.get(key)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be an integer",
                field=key,
            )
        return value

    def _context_ages(
        self,
        details: dict[str, JsonValue],
        default: tuple[int, ...],
    ) -> tuple[int, ...]:
        value = details.get("children_ages")
        if value is None:
            return default
        if not isinstance(value, list) or any(
            not isinstance(age, int) or isinstance(age, bool) for age in value
        ):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "children_ages must be a list of integers",
                field="children_ages",
            )
        ages = tuple(cast(list[int], value))
        if any(age < 0 or age > 17 for age in ages):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                "children ages must be between 0 and 17",
                field="children_ages",
            )
        return ages

    def _optional_bool(
        self,
        details: dict[str, JsonValue],
        key: str,
    ) -> bool | None:
        value = details.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be boolean or null",
                field=key,
            )
        return value

    def _required_bool(self, details: dict[str, JsonValue], key: str) -> bool:
        value = details.get(key)
        if not isinstance(value, bool):
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                f"required boolean field {key!r} is missing",
                field=key,
            )
        return value

    def _decimal(self, details: dict[str, JsonValue], key: str) -> Decimal:
        value: Any = details.get(key)
        if isinstance(value, bool) or value is None:
            raise _RejectNormalization(
                QuoteNormalizationCode.MISSING_FIELD,
                f"required decimal field {key!r} is missing",
                field=key,
            )
        if not isinstance(value, (str, int, float)):
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a decimal-compatible value",
                field=key,
            )
        try:
            amount = Decimal(str(value))
        except Exception as exc:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a valid decimal",
                field=key,
            ) from exc
        if not amount.is_finite() or amount < 0:
            raise _RejectNormalization(
                QuoteNormalizationCode.INVALID_FIELD,
                f"{key} must be a finite non-negative decimal",
                field=key,
            )
        return amount
