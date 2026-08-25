"""Adapters that project iCom's public ferry schedules into the complex graph."""

from __future__ import annotations

import asyncio
from datetime import UTC
from decimal import Decimal
from typing import Any

from tripchord.planning.complex_trip import (
    PriceContract,
    SourceState,
    SourceStatus,
    TransportOffer,
    TravelIntent,
)
from tripchord.providers.fx_reference import (
    UsdCnyReferenceRate,
    fetch_usd_cny_reference_rate,
)
from tripchord.providers.icom_transfer import (
    IComLocation,
    IComTransferProvider,
    IComTransferQuery,
    IComTransferSearchResult,
)


class IComCurrentTransportSource:
    """Map only Airport <-> Maafushi legs; other legs remain unqueried."""

    def __init__(self, provider: IComTransferProvider | None = None) -> None:
        self._provider = provider

    async def catalog_for(
        self,
        intent: TravelIntent,
        *,
        reference_rate: UsdCnyReferenceRate | None = None,
        fetch_reference: bool = True,
    ) -> tuple[
        tuple[TransportOffer, ...],
        tuple[PriceContract, ...],
        tuple[SourceStatus, ...],
        tuple[str, ...],
    ]:
        supported: list[tuple[Any, IComLocation, IComLocation]] = []
        for requirement in intent.route_legs:
            origin = _icom_location(requirement.origin_place_id)
            destination = _icom_location(requirement.destination_place_id)
            if origin is None or destination is None or origin == destination:
                continue
            departure_date = requirement.departure_date
            if departure_date is None:
                continue
            supported.append((requirement, origin, destination))
        if not supported:
            return (), (), (), ()
        provider = self._provider or IComTransferProvider()
        owns_provider = self._provider is None
        if reference_rate is None and fetch_reference:
            try:
                reference_rate = await fetch_usd_cny_reference_rate()
            except Exception:
                # The USD source remains usable as an explicitly unpriced
                # observation when the optional display-only FX reference is
                # unavailable.
                reference_rate = None
        try:
            results = await asyncio.gather(
                *(
                    provider.search(
                        IComTransferQuery(
                            travel_date=requirement.departure_date,
                            origin=origin,
                            destination=destination,
                            adults=len(requirement.participant_ids) or intent.travelers,
                        ),
                        query_task_id=f"icom:complex:{requirement.id}",
                    )
                    for requirement, origin, destination in supported
                )
            )
            converted = await asyncio.gather(
                *(
                    self._convert_result(
                        intent,
                        requirement,
                        result,
                        reference_rate=reference_rate,
                    )
                    for (requirement, _, _), result in zip(supported, results, strict=True)
                )
            )
            return (
                tuple(item for batch in converted for item in batch[0]),
                tuple(item for batch in converted for item in batch[1]),
                tuple(batch[2] for batch in converted),
                tuple(batch[3] for batch in converted),
            )
        finally:
            if owns_provider:
                await provider.aclose()

    async def _convert_result(
        self,
        intent: TravelIntent,
        requirement: Any,
        result: IComTransferSearchResult,
        reference_rate: UsdCnyReferenceRate | None,
    ) -> tuple[tuple[TransportOffer, ...], tuple[PriceContract, ...], SourceStatus, str]:
        task_id = f"icom:complex:{requirement.id}"
        captured_at = result.searched_at.astimezone(UTC)
        adults = len(requirement.participant_ids) or intent.travelers
        options = tuple(
            item
            for item in result.options
            if item.eligible_for_party and item.remaining_capacity >= adults
        )
        if not options:
            return (
                (),
                (),
                SourceStatus(
                    source_id=f"icom:complex:{requirement.id}",
                    provider="icom-public-transfer",
                    state=SourceState.FAILED,
                    detail="iCom当前轮渡没有返回满足同行人数的余位",
                    query_task_ids=(task_id,),
                    captured_at=captured_at,
                ),
                task_id,
            )
        offers: list[TransportOffer] = []
        contracts: list[PriceContract] = []
        participant_ids = _participant_ids(requirement, intent)
        for option in options:
            offer_id = f"icom:ferry:{option.trip_id}:{option.schedule_id}:{adults}a"
            fare_cents_usd = int(
                (option.fare.amount * Decimal(100) * adults).quantize(Decimal("1"))
            )
            if reference_rate is None:
                contract_id = f"unpriced:{offer_id}"
                cny_total = 0
                basis = "unknown"
                boundary = (
                    f"公开基础价USD {option.fare.amount}/人 × {adults}，"
                    "当前未取得可审计人民币参考汇率，未计入整趟总价"
                )
            else:
                cny_total = int(
                    (Decimal(fare_cents_usd) * reference_rate.usd_to_cny).quantize(
                        Decimal("1")
                    )
                )
                contract_id = f"reference:{offer_id}:{reference_rate.response_sha256[:12]}"
                basis = "reference"
                boundary = (
                    f"公开基础价USD {option.fare.amount}/人 × {adults}，按ECB "
                    f"{reference_rate.rate_date}参考汇率换算；税费未确认、未锁库存"
                )
            contracts.append(
                PriceContract(
                    id=contract_id,
                    currency="CNY",
                    total_for_party_cents=cny_total,
                    component_ids=(offer_id,),
                    covered_traveler_ids=participant_ids,
                    shared_between_travelers=adults > 1,
                    taxes_and_fees_included=False,
                    source="current:icom-public-transfer",
                    price_basis=basis,
                    original_currency="USD",
                    original_total_for_party_cents=fare_cents_usd,
                    price_boundary=boundary,
                )
            )
            offers.append(
                TransportOffer(
                    id=offer_id,
                    provider="icom-public-transfer",
                    origin_place_id=requirement.origin_place_id,
                    destination_place_id=requirement.destination_place_id,
                    departure=option.departure_at,
                    arrival=option.arrival_at,
                    price_contract_id=contract_id,
                    detail_url=option.source_url,
                    label=(
                        f"{option.origin.value}→{option.destination.value} "
                        f"{option.vessel_name} {option.departure_at.strftime('%H:%M')}-"
                        f"{option.arrival_at.strftime('%H:%M')}（轮渡）"
                    ),
                    participant_ids=participant_ids,
                    party_capacity_confirmed=True,
                    available_units=option.remaining_capacity,
                    mode="ferry",
                )
            )
        status_detail = (
            f"iCom官方当前轮渡返回{len(offers)}个班次；"
            "公开基础价为USD，税费未确认，人民币仅参考换算"
        )
        return (
            tuple(offers),
            tuple(contracts),
            SourceStatus(
                source_id=f"icom:complex:{requirement.id}",
                provider="icom-public-transfer",
                state=SourceState.SUCCEEDED,
                detail=status_detail,
                query_task_ids=(task_id,),
                captured_at=captured_at,
            ),
            task_id,
        )


def _icom_location(place_id: str) -> IComLocation | None:
    normalized = place_id.strip().lower()
    if normalized in {
        "mle",
        "male",
        "malé",
        "马累机场",
        "维拉纳国际机场",
        "velana international airport",
        "机场",
    }:
        return IComLocation.AIRPORT
    if normalized in {"maafushi", "马富施", "马富士"}:
        return IComLocation.MAAFUSHI
    return None


def _participant_ids(requirement: Any, intent: TravelIntent) -> tuple[str, ...]:
    if requirement.participant_ids:
        return tuple(requirement.participant_ids)
    if intent.traveler_profiles:
        return tuple(item.id for item in intent.traveler_profiles)
    return tuple(f"traveler:{index + 1}" for index in range(intent.travelers))


__all__ = ["IComCurrentTransportSource"]
