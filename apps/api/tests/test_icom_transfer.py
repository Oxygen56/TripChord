from __future__ import annotations

import asyncio
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from tripchord.planning.package import (
    PackageArea,
    PackagePlaceKey,
    TransferPriceGuarantee,
)
from tripchord.providers.base import ProviderError
from tripchord.providers.icom_transfer import (
    IComAvailabilityStatus,
    IComLocation,
    IComTransferConfig,
    IComTransferOption,
    IComTransferProvider,
    IComTransferQuery,
    to_package_transfer_option,
)

TRAVEL_DATE = date(2026, 8, 10)


def _meta(message: str = "Success") -> dict[str, object]:
    return {
        "timestamp": "2026-07-30T12:51:28+00:00",
        "apiVersion": "v1",
        "status": "success",
        "message": message,
    }


def _schedule_payload(
    *,
    cancelled: bool = False,
    remaining: int = 45,
) -> dict[str, object]:
    return {
        "meta": _meta("Schedules retrieved successfully"),
        "data": [
            {
                "id": 3445,
                "tripDate": TRAVEL_DATE.isoformat(),
                "departureTime": "07:30",
                "arrivalTime": "08:15",
                "capacity": 45,
                "remainingCapacity": remaining,
                "cancelledAt": "2026-08-09T10:00:00Z" if cancelled else None,
                "isCancelled": cancelled,
                "isDeparted": False,
                "scheduleId": 2,
                "vesselId": 3,
                "stops": 1,
                "ferryName": "Ferry 02 07:45",
                "baseFare": None,
                "vessel": {
                    "id": 3,
                    "name": "iCom Brown",
                    "totalCapacity": 45,
                },
                "origin": {
                    "id": 3,
                    "name": "Airport",
                    "departureLocation": "Near Taxi Queue",
                },
                "destination": {"id": 1, "name": "Maafushi"},
            },
            {
                "id": 3446,
                "tripDate": TRAVEL_DATE.isoformat(),
                "departureTime": "07:45",
                "arrivalTime": "08:15",
                "capacity": 45,
                "remainingCapacity": 45,
                "cancelledAt": None,
                "isCancelled": False,
                "isDeparted": False,
                "scheduleId": 2,
                "vesselId": 3,
                "stops": 0,
                "ferryName": "Ferry 02 07:45",
                "baseFare": None,
                "vessel": {
                    "id": 3,
                    "name": "iCom Brown",
                    "totalCapacity": 45,
                },
                "origin": {"id": 2, "name": "Malé"},
                "destination": {"id": 1, "name": "Maafushi"},
            },
        ],
    }


def _fare_payload() -> dict[str, object]:
    return {
        "meta": _meta(),
        "data": {"amount": 30, "currencyCode": "USD"},
    }


def _policy_payload() -> dict[str, object]:
    return {
        "meta": _meta("Policy sections retrieved successfully"),
        "data": [
            {
                "id": 1,
                "title": "Payments",
                "sortOrder": 1,
                "richtext": {
                    "blocks": [
                        {
                            "type": "paragraph",
                            "data": {
                                "text": (
                                    "All prices are displayed and charged in US Dollars (USD) "
                                    "unless otherwise specified."
                                )
                            },
                        }
                    ]
                },
                "isActive": True,
            }
        ],
    }


def _handler(
    *,
    schedules: dict[str, object] | None = None,
    fare: dict[str, object] | None = None,
    policy: dict[str, object] | None = None,
    requests: list[httpx.Request] | None = None,
) -> Any:
    schedule_response = schedules if schedules is not None else _schedule_payload()
    fare_response = fare if fare is not None else _fare_payload()
    policy_response = policy if policy is not None else _policy_payload()

    def handle(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "sfs-api.icomtours.com"
        if request.url.path == "/api/v1/public/trips/schedules":
            assert request.url.params["date"] == TRAVEL_DATE.isoformat()
            return httpx.Response(200, json=schedule_response)
        if request.url.path == "/api/v1/public/ferry-fares/schedule-base-price":
            assert not request.url.query
            return httpx.Response(200, json=fare_response)
        if request.url.path == "/api/v1/public/policy-sections":
            assert not request.url.query
            return httpx.Response(200, json=policy_response)
        raise AssertionError(f"unexpected iCom URL: {request.url}")

    return handle


def _query(*, adults: int = 2) -> IComTransferQuery:
    return IComTransferQuery(
        travel_date=TRAVEL_DATE,
        origin=IComLocation.AIRPORT,
        destination=IComLocation.MAAFUSHI,
        adults=adults,
    )


async def _available_option() -> IComTransferOption:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        result = await IComTransferProvider(client=client).search(_query())
    return result.options[0]


@pytest.mark.asyncio
async def test_success_normalizes_official_public_transfer_with_field_evidence() -> None:
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(requests=requests))
    ) as client:
        result = await IComTransferProvider(client=client).search(_query())

    assert len(requests) == 3
    assert {request.url.path for request in requests} == {
        "/api/v1/public/trips/schedules",
        "/api/v1/public/ferry-fares/schedule-base-price",
        "/api/v1/public/policy-sections",
    }
    assert len(result.options) == 1
    option = result.options[0]
    assert option.trip_id == 3445
    assert option.operator == "iCom Tours"
    assert option.vessel_name == "iCom Brown"
    assert option.route == "Airport -> Maafushi"
    assert option.departure_at.isoformat() == "2026-08-10T07:30:00+05:00"
    assert option.arrival_at.isoformat() == "2026-08-10T08:15:00+05:00"
    assert option.departure_at.utcoffset() == timedelta(hours=5)
    assert option.capacity == 45
    assert option.remaining_capacity == 45
    assert option.availability_status == IComAvailabilityStatus.AVAILABLE
    assert option.eligible_for_party is True
    assert option.fare.kind == "published_base_fare"
    assert option.fare.amount == Decimal("30")
    assert option.fare.currency == "USD"
    assert option.fare.basis == "per_person"
    assert option.fare.taxes_included is None
    assert option.source_url.endswith("/api/v1/public/trips/schedules?date=2026-08-10")
    assert option.captured_at.utcoffset() == timedelta(0)
    assert option.currency_policy_evidence is not None
    assert "displayed and charged" in option.currency_policy_evidence.statement
    assert option.currency_policy_evidence.tax_inclusion_confirmed is None
    evidence = {item.normalized_field: item for item in option.evidence}
    assert evidence["departure_at"].json_paths == (
        "$.data[0].tripDate",
        "$.data[0].departureTime",
    )
    assert len(evidence["departure_at"].value_sha256) == 64
    assert len(evidence["departure_at"].response_sha256) == 64

    package_option = to_package_transfer_option(option, adults=2)
    assert package_option is not None
    assert package_option == to_package_transfer_option(option, adults=2)
    assert package_option.provider == "icom-public-transfer"
    assert package_option.origin_area == PackageArea.AIRPORT
    assert package_option.destination_area == PackageArea.DESTINATION_ISLAND
    assert package_option.origin_place_key == PackagePlaceKey.VELANA_AIRPORT
    assert package_option.destination_place_key == PackagePlaceKey.MAAFUSHI
    assert package_option.adults == 2
    assert package_option.currency == "USD"
    assert package_option.total_for_party_cents == 6_000
    assert package_option.price_guarantee == TransferPriceGuarantee.PUBLISHED_BASE_FARE
    assert package_option.taxes_and_fees_included is None
    assert package_option.depart_at == option.departure_at
    assert package_option.arrive_at == option.arrival_at
    assert package_option.service_date == TRAVEL_DATE
    assert package_option.expires_at <= option.departure_at
    assert "USD 30.00/人 × 2人 = USD 60.00" in package_option.contract_evidence_text
    assert "税费未确认" in package_option.contract_evidence_text
    assert "未锁库存" in package_option.contract_evidence_text
    assert package_option.evidence_refs


@pytest.mark.asyncio
async def test_cancelled_schedule_is_retained_but_not_eligible() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(schedules=_schedule_payload(cancelled=True)))
    ) as client:
        result = await IComTransferProvider(client=client).search(_query())

    option = result.options[0]
    assert option.is_cancelled is True
    assert option.availability_status == IComAvailabilityStatus.CANCELLED
    assert option.eligible_for_party is False
    assert to_package_transfer_option(option, adults=2) is None


@pytest.mark.asyncio
async def test_insufficient_remaining_capacity_is_not_eligible_for_party() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(schedules=_schedule_payload(remaining=1)))
    ) as client:
        result = await IComTransferProvider(client=client).search(_query(adults=2))

    option = result.options[0]
    assert option.is_cancelled is False
    assert option.remaining_capacity == 1
    assert option.availability_status == IComAvailabilityStatus.INSUFFICIENT_REMAINING
    assert option.eligible_for_party is False
    assert to_package_transfer_option(option, adults=2) is None


@pytest.mark.asyncio
async def test_bad_json_contract_is_reported_as_typed_schema_drift() -> None:
    bad_schedules = _schedule_payload()
    first = bad_schedules["data"][0]  # type: ignore[index]
    first["remainingCapacity"] = "45"  # type: ignore[index]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(schedules=bad_schedules))
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await IComTransferProvider(client=client).search(_query())

    assert caught.value.provider == "icom-public-transfer"
    assert caught.value.code == "schema_drift"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_redirect_to_privileged_booking_path_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/public/trips/schedules":
            return httpx.Response(
                302,
                headers={"location": "/api/v1/public/ferry-bookings"},
            )
        payload = _fare_payload() if "fare" in request.url.path else _policy_payload()
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            await IComTransferProvider(client=client).search(_query())

    assert caught.value.code == "redirect_boundary"
    assert caught.value.retryable is False


@pytest.mark.asyncio
async def test_usd_policy_never_upgrades_base_fare_to_tax_inclusive() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        result = await IComTransferProvider(client=client).search(_query())

    option = result.options[0]
    assert option.currency_policy_evidence is not None
    assert option.currency_policy_evidence.meaning == "prices_displayed_and_charged_in_usd"
    assert option.currency_policy_evidence.tax_inclusion_confirmed is None
    assert option.fare.taxes_included is None
    tax_evidence = next(
        item for item in option.fare.evidence if item.normalized_field == "fare.taxes_included"
    )
    assert tax_evidence.derivation == "not_asserted"
    assert tax_evidence.json_paths == ()


@pytest.mark.asyncio
async def test_non_2xx_is_a_retryable_typed_http_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/public/ferry-fares/schedule-base-price":
            return httpx.Response(503, json={"message": "unavailable"})
        if request.url.path == "/api/v1/public/trips/schedules":
            return httpx.Response(200, json=_schedule_payload())
        return httpx.Response(200, json=_policy_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            await IComTransferProvider(client=client).search(_query())

    assert caught.value.code == "http_status"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_raw_response_size_cap_is_enforced() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler())) as client:
        provider = IComTransferProvider(
            IComTransferConfig(max_response_bytes=128),
            client=client,
        )
        with pytest.raises(ProviderError) as caught:
            await provider.search(_query())

    assert caught.value.code == "response_too_large"


@pytest.mark.asyncio
async def test_provider_deadline_is_a_retryable_typed_timeout() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=_schedule_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = IComTransferProvider(
            IComTransferConfig(timeout_seconds=0.01),
            client=client,
        )
        with pytest.raises(ProviderError) as caught:
            await provider.search(_query())

    assert caught.value.code == "timeout"
    assert caught.value.retryable is True


@pytest.mark.asyncio
async def test_package_contract_id_binds_direction_date_schedule_and_party() -> None:
    option = await _available_option()
    two_adults = to_package_transfer_option(option, adults=2)
    one_adult = to_package_transfer_option(option, adults=1)
    assert two_adults is not None
    assert one_adult is not None
    assert two_adults.price_contract_id != one_adult.price_contract_id
    assert one_adult.total_for_party_cents == 3_000

    reverse_departure = option.departure_at.replace(hour=12, minute=0)
    reverse = option.model_copy(
        update={
            "trip_id": option.trip_id + 1,
            "origin": IComLocation.MAAFUSHI,
            "destination": IComLocation.AIRPORT,
            "route": "Maafushi -> Airport",
            "departure_at": reverse_departure,
            "arrival_at": reverse_departure + timedelta(minutes=45),
        }
    )
    reverse_package = to_package_transfer_option(reverse, adults=2)
    assert reverse_package is not None
    assert reverse_package.origin_area == PackageArea.DESTINATION_ISLAND
    assert reverse_package.destination_area == PackageArea.AIRPORT
    assert reverse_package.origin_place_key == PackagePlaceKey.MAAFUSHI
    assert reverse_package.destination_place_key == PackagePlaceKey.VELANA_AIRPORT
    assert reverse_package.price_contract_id != two_adults.price_contract_id

    next_day = option.model_copy(
        update={
            "departure_at": option.departure_at + timedelta(days=1),
            "arrival_at": option.arrival_at + timedelta(days=1),
        }
    )
    next_day_package = to_package_transfer_option(next_day, adults=2)
    assert next_day_package is not None
    assert next_day_package.price_contract_id != two_adults.price_contract_id

    other_schedule = option.model_copy(update={"schedule_id": option.schedule_id + 1})
    other_schedule_package = to_package_transfer_option(other_schedule, adults=2)
    assert other_schedule_package is not None
    assert other_schedule_package.price_contract_id != two_adults.price_contract_id

    changed_evidence = option.model_copy(
        update={
            "evidence": (
                option.evidence[0].model_copy(update={"value_sha256": "0" * 64}),
                *option.evidence[1:],
            )
        }
    )
    changed_evidence_package = to_package_transfer_option(changed_evidence, adults=2)
    assert changed_evidence_package is not None
    assert changed_evidence_package.price_contract_id != two_adults.price_contract_id


@pytest.mark.asyncio
async def test_package_conversion_rejects_unrepresentable_or_expired_evidence() -> None:
    option = await _available_option()
    fractional_cent = option.model_copy(
        update={"fare": option.fare.model_copy(update={"amount": Decimal("30.001")})}
    )
    with pytest.raises(ValueError, match="integer cents"):
        to_package_transfer_option(fractional_cent, adults=2)

    fractional_minute = option.model_copy(
        update={"arrival_at": option.arrival_at + timedelta(seconds=30)}
    )
    with pytest.raises(ValueError, match="whole-minute"):
        to_package_transfer_option(fractional_minute, adults=2)

    already_departed = option.model_copy(update={"captured_at": option.departure_at})
    assert to_package_transfer_option(already_departed, adults=2) is None

    with pytest.raises(ValueError, match="between 1 and 9"):
        to_package_transfer_option(option, adults=10)
