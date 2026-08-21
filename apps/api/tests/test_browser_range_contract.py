import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_browser_bridge import quote as legacy_quote
from tripchord.providers.browser_bridge import (
    BrowserDateRangeQuery,
    BrowserProvider,
    BrowserRangeCapabilityStatus,
    BrowserRangeCell,
    BrowserRangeCompletion,
    BrowserRangeParty,
    BrowserRangePriceFinality,
    BrowserVertical,
    CompleteBrowserTaskRequest,
    QuotePriceBasis,
    RangeCapabilityEvidence,
    browser_range_receipt_sha256,
    exact_cell_binding_error,
    range_completion_fallback_pairs,
)

PAIR_A = (date(2026, 8, 23), date(2026, 8, 30))
PAIR_B = (date(2026, 8, 24), date(2026, 8, 31))
CAPTURED_AT = datetime.now(UTC)


def query() -> BrowserDateRangeQuery:
    return BrowserDateRangeQuery(
        provider=BrowserProvider.CTRIP,
        kind=BrowserVertical.FLIGHT,
        origin="杭州",
        destination="马累",
        origin_code="HGH",
        destination_code="MLE",
        requested_pairs=(PAIR_A, PAIR_B),
        party=BrowserRangeParty(adults=2),
        currency="cny",
        tenant_partition_sha256="a" * 64,
        contract_version="range-contract-v1",
        parser_version="tripchord-visible-dom-v3",
    )


def cell(
    pair: tuple[date, date],
    *,
    finality: BrowserRangePriceFinality = BrowserRangePriceFinality.EXACT,
) -> BrowserRangeCell:
    bound_quote = legacy_quote(BrowserProvider.CTRIP, BrowserVertical.FLIGHT)
    details = dict(bound_quote.details)
    query_details = dict(details["query"])
    query_details.update(
        {"start_date": pair[0].isoformat(), "end_date": pair[1].isoformat()}
    )
    details["query"] = query_details
    bound_quote = bound_quote.model_copy(
        update={
            "amount": Decimal("4692"),
            "price_basis": QuotePriceBasis.TOTAL_PARTY,
            "evidence_sha256": hashlib.sha256(
                bound_quote.visible_evidence.encode("utf-8")
            ).hexdigest(),
            "details": details,
        }
    )
    return BrowserRangeCell(
        start_date=pair[0],
        end_date=pair[1],
        party=BrowserRangeParty(adults=2),
        currency="CNY",
        amount=Decimal("4692"),
        price_basis="total_for_party",
        party_total_known=True,
        taxes_and_fees_included=True,
        product_identity="ctrip-fixture-outbound-return",
        quote=bound_quote if finality == BrowserRangePriceFinality.EXACT else None,
        price_finality=finality,
        evidence_sha256=hashlib.sha256(
            bound_quote.visible_evidence.encode("utf-8")
        ).hexdigest(),
        captured_at=CAPTURED_AT,
    )


def completion(
    status: BrowserRangeCapabilityStatus,
    cells: tuple[BrowserRangeCell, ...],
    *,
    expires_seconds: int | None = 300,
    query_override: BrowserDateRangeQuery | None = None,
) -> BrowserRangeCompletion:
    requested_query = query_override or query()
    capability = RangeCapabilityEvidence(
        status=status,
        provider=BrowserProvider.CTRIP,
        contract_version="range-contract-v1",
        parser_version="tripchord-visible-dom-v3",
        evidence_sha256="c" * 64,
        captured_at=CAPTURED_AT,
        query_fingerprint_sha256=requested_query.fingerprint_sha256,
        task_id="task-range-1",
        lease_id="lease-range-1",
        evidence_type="visible_dom",
        source_url="https://flights.ctrip.com/search/results",
        response_shape_sha256="d" * 64,
    )
    payload = {
        "schema_version": "tripchord-browser-range-receipt-v1",
        "query": requested_query.model_dump(mode="python"),
        "capability": capability.model_dump(mode="python"),
        "cells": [item.model_dump(mode="python") for item in cells],
        "expires_at": (
            CAPTURED_AT + timedelta(seconds=expires_seconds)
            if expires_seconds is not None
            else None
        ),
    }
    return BrowserRangeCompletion(
        **payload,
        receipt_sha256=browser_range_receipt_sha256(payload),
    )


def test_receipt_hash_is_canonical_and_bound_to_requested_range() -> None:
    receipt = completion(BrowserRangeCapabilityStatus.CONFIRMED, (cell(PAIR_A), cell(PAIR_B)))
    assert receipt.complete_coverage
    assert receipt.receipt_sha256 == browser_range_receipt_sha256(receipt)
    assert receipt.query.fingerprint_sha256 != query().model_copy(
        update={"currency": "USD"}
    ).fingerprint_sha256

    with pytest.raises(ValidationError, match="receipt_sha256"):
        invalid = receipt.model_dump(mode="python")
        invalid["receipt_sha256"] = "d" * 64
        BrowserRangeCompletion(**invalid)


def _js_canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    ("captured_at", "expires_at"),
    (
        ("2026-08-22T00:00:00.123Z", "2026-08-22T00:05:00.123Z"),
        ("2026-08-22T00:00:00.000Z", "2026-08-22T00:05:00.000Z"),
    ),
)
def test_complete_request_accepts_js_millisecond_range_receipt(
    captured_at: str,
    expires_at: str,
) -> None:
    range_query = {
        "provider": "ctrip",
        "kind": "flight",
        "origin": "杭州",
        "destination": "马累",
        "origin_code": "HGH",
        "destination_code": "MLE",
        "requested_pairs": [
            ["2026-09-03", "2026-09-09"],
            ["2026-09-04", "2026-09-10"],
        ],
        "party": {
            "adults": 2,
            "children": 0,
            "children_ages": [],
            "infants": 0,
            "rooms": 1,
        },
        "currency": "CNY",
        "tenant_partition_sha256": "a" * 64,
        "contract_version": "range-contract-v1",
        "parser_version": "tripchord-visible-dom-v3",
    }
    capability = {
        "status": "inconclusive",
        "provider": "ctrip",
        "contract_version": range_query["contract_version"],
        "parser_version": range_query["parser_version"],
        "evidence_sha256": "b" * 64,
        "captured_at": captured_at,
        "query_fingerprint_sha256": BrowserDateRangeQuery.model_validate(
            range_query
        ).fingerprint_sha256,
        "task_id": None,
        "lease_id": None,
        "evidence_type": "visible_dom",
        "source_url": (
            "https://flights.ctrip.com/international/search/round-hgh-mle"
            "?depdate=2026-09-03_2026-09-09&cabin=y_s&adult=2&child=0&infant=0"
        ),
        "response_shape_sha256": "c" * 64,
        "reason": "bounded_date_shards_completed_with_partial_or_non_exact_cells",
    }
    cells = [
        {
            "start_date": start_date,
            "end_date": end_date,
            "party": range_query["party"],
            "currency": "CNY",
            "amount": None,
            "price_basis": "unknown",
            "party_total_known": False,
            "taxes_and_fees_included": None,
            "product_identity": None,
            "quote": None,
            "price_finality": "unknown",
            "evidence_sha256": str(index) * 64,
            "captured_at": captured_at,
        }
        for index, (start_date, end_date) in enumerate(
            range_query["requested_pairs"],
            start=1,
        )
    ]
    range_completion = {
        "schema_version": "tripchord-browser-range-receipt-v1",
        "query": range_query,
        "capability": capability,
        "cells": cells,
        "receipt_sha256": "",
        "expires_at": expires_at,
    }
    range_completion["receipt_sha256"] = _js_canonical_sha256(
        {key: value for key, value in range_completion.items() if key != "receipt_sha256"}
    )
    request = CompleteBrowserTaskRequest.model_validate(
        {
            "claim_token": "x" * 16,
            "completion": {
                "state": "failed",
                "quotes": [],
                "failure": {
                    "code": "no_inventory",
                    "message": "批量日期任务未取得可比较的真实价格单元",
                    "retryable": True,
                    "page_url": None,
                    "captured_at": captured_at,
                    "details": {
                        "executor": "native_range",
                        "cell_budget_ms": 71000,
                        "failures": [],
                    },
                },
                "range_completion": range_completion,
            },
            "source_execution_attestation": {
                "schema_version": "tripchord-browser-source-execution-attestation-v1",
                "task_id": "browser-task-range-wire",
                "provider": "ctrip",
                "kind": "flight",
                "companion_id": "tripchord-browser-companion",
                "runtime_instance_id": "runtime-instance-0001",
                "build_identity": {
                    "protocol_version": "tripchord-companion-control-v1",
                    "manifest_version": "0.1.16",
                    "build_sha256": "d" * 64,
                    "content_runtime_version": "content-1",
                },
                "execution_environment": "chrome_extension_service_worker",
                "parser_version": "tripchord-visible-dom-v3",
                "query_sha256": "e" * 64,
                "source_observation_sha256": "f" * 64,
                "completed_at": captured_at,
            },
        }
    )

    assert request.completion.range_completion is not None
    assert (
        browser_range_receipt_sha256(request.completion.range_completion)
        == range_completion["receipt_sha256"]
    )


@pytest.mark.parametrize(
    "status",
    (BrowserRangeCapabilityStatus.REJECTED, BrowserRangeCapabilityStatus.INCONCLUSIVE),
)
def test_rejected_or_inconclusive_range_always_falls_back(
    status: BrowserRangeCapabilityStatus,
) -> None:
    receipt = completion(status, ())
    assert receipt.complete_coverage is False
    assert range_completion_fallback_pairs(receipt) == (PAIR_A, PAIR_B)


def test_missing_cell_is_not_complete_and_starting_price_cannot_be_confirmed() -> None:
    partial = completion(BrowserRangeCapabilityStatus.CONFIRMED, (cell(PAIR_A),))
    assert partial.requires_single_date_fallback
    assert range_completion_fallback_pairs(partial) == (PAIR_B,)

    starting = completion(
        BrowserRangeCapabilityStatus.CONFIRMED,
        (cell(PAIR_A, finality=BrowserRangePriceFinality.STARTING), cell(PAIR_B)),
    )
    assert not starting.complete_coverage
    assert range_completion_fallback_pairs(starting) == (PAIR_A,)


def test_exact_requires_quote_and_short_freshness_window() -> None:
    no_quote = cell(PAIR_A).model_copy(update={"quote": None})
    with pytest.raises(ValidationError, match="bound BrowserQuote"):
        completion(BrowserRangeCapabilityStatus.CONFIRMED, (no_quote, cell(PAIR_B)))

    with pytest.raises(ValidationError, match="TTL"):
        completion(
            BrowserRangeCapabilityStatus.CONFIRMED,
            (cell(PAIR_A), cell(PAIR_B)),
            expires_seconds=601,
        )

    with pytest.raises(ValidationError, match="does not match exact"):
        completion(
            BrowserRangeCapabilityStatus.CONFIRMED,
            (cell(PAIR_A).model_copy(update={"amount": Decimal("1")}), cell(PAIR_B)),
        )


def test_exact_binding_rejects_pseudo_hash_evil_source_blank_lineage_and_missing_iata() -> None:
    receipt = completion(BrowserRangeCapabilityStatus.CONFIRMED, (cell(PAIR_A), cell(PAIR_B)))
    original = cell(PAIR_A)
    bad_quote = original.quote.model_copy(update={"evidence_sha256": "a" * 64})
    bad_cell = original.model_copy(
        update={"evidence_sha256": "a" * 64, "quote": bad_quote}
    )
    assert exact_cell_binding_error(
        receipt.query, receipt.capability, bad_cell, receipt.expires_at, CAPTURED_AT
    ) is not None

    evil = receipt.capability.model_copy(update={"source_url": "https://evil.example/range"})
    assert exact_cell_binding_error(
        receipt.query, evil, original, receipt.expires_at, CAPTURED_AT
    ) is not None
    blank = receipt.capability.model_copy(update={"task_id": " ", "lease_id": " "})
    assert exact_cell_binding_error(
        receipt.query, blank, original, receipt.expires_at, CAPTURED_AT
    ) is not None

    with pytest.raises(ValidationError, match="audited IATA"):
        BrowserDateRangeQuery(
            provider=BrowserProvider.CTRIP,
            kind=BrowserVertical.FLIGHT,
            origin="杭州",
            destination="马累",
            requested_pairs=(PAIR_A,),
            party=BrowserRangeParty(adults=2),
            currency="CNY",
            tenant_partition_sha256="a" * 64,
            contract_version="range-contract-v1",
            parser_version="tripchord-visible-dom-v3",
        )


def test_completion_without_expiry_falls_back_all_requested_pairs() -> None:
    receipt = completion(
        BrowserRangeCapabilityStatus.CONFIRMED,
        (
            cell(PAIR_A, finality=BrowserRangePriceFinality.STARTING),
            cell(PAIR_B, finality=BrowserRangePriceFinality.STARTING),
        ),
        expires_seconds=None,
    )
    assert receipt.usable_exact_pairs == set()
    assert range_completion_fallback_pairs(receipt) == (PAIR_A, PAIR_B)


def test_old_quote_with_new_capability_is_rejected_at_completion_creation() -> None:
    old_quote = cell(PAIR_A).quote.model_copy(
        update={"captured_at": datetime(2020, 1, 1, tzinfo=UTC)}
    )
    old_cell = cell(PAIR_A).model_copy(update={"quote": old_quote})
    with pytest.raises(ValidationError, match="stale"):
        completion(BrowserRangeCapabilityStatus.CONFIRMED, (old_cell, cell(PAIR_B)))


def test_future_dated_receipt_cannot_create_freshness() -> None:
    receipt = completion(BrowserRangeCapabilityStatus.CONFIRMED, (cell(PAIR_A), cell(PAIR_B)))
    future = datetime.now(UTC) + timedelta(days=365)
    payload = receipt.model_dump(mode="python")
    payload["capability"]["captured_at"] = future
    payload["expires_at"] = future + timedelta(seconds=300)
    for item in payload["cells"]:
        item["captured_at"] = future
        item["quote"]["captured_at"] = future
    payload["receipt_sha256"] = browser_range_receipt_sha256(payload)

    with pytest.raises(ValidationError, match="future-dated"):
        BrowserRangeCompletion(**payload)


def test_tokyo_london_route_and_iata_mismatch_is_rejected() -> None:
    tokyo_london = query().model_copy(
        update={
            "origin": "东京",
            "destination": "伦敦",
            "origin_code": "NRT",
            "destination_code": "LHR",
        }
    )
    with pytest.raises(ValidationError, match="route"):
        completion(
            BrowserRangeCapabilityStatus.CONFIRMED,
            (cell(PAIR_A), cell(PAIR_B)),
            query_override=tokyo_london,
        )


def test_mixed_party_with_child_age_and_infant_is_not_usable() -> None:
    mixed_party = BrowserRangeParty(
        adults=2,
        children=1,
        children_ages=(6,),
        infants=1,
    )
    mixed_query = query().model_copy(update={"party": mixed_party})
    mixed_a = cell(PAIR_A).model_copy(update={"party": mixed_party})
    mixed_b = cell(PAIR_B).model_copy(update={"party": mixed_party})
    with pytest.raises(ValidationError, match="mixed party"):
        completion(
            BrowserRangeCapabilityStatus.CONFIRMED,
            (mixed_a, mixed_b),
            query_override=mixed_query,
        )
