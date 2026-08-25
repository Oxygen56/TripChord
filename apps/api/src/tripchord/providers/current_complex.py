"""Current bounded multi-city catalog from price-complete public sources."""

from __future__ import annotations

import asyncio

from tripchord.planning.complex_trip import OfferCatalog, PriceContract, TravelIntent
from tripchord.providers.browser_bridge import BrowserTaskBridge
from tripchord.providers.fx_reference import UsdCnyReferenceRate, fetch_usd_cny_reference_rate
from tripchord.providers.icom_activity import IComCurrentActivitySource
from tripchord.providers.icom_complex import IComCurrentTransportSource
from tripchord.providers.rail_12306 import Rail12306CurrentCatalogSource
from tripchord.providers.tripcom_lodging import TripComCurrentLodgingSource


class CurrentComplexOfferProvider:
    """Compose current transport and lodging observations without guessing."""

    def __init__(
        self,
        _bridge: BrowserTaskBridge | None = None,
        *,
        rail_source: Rail12306CurrentCatalogSource | None = None,
        lodging_source: TripComCurrentLodgingSource | None = None,
        ferry_source: IComCurrentTransportSource | None = None,
        activity_source: IComCurrentActivitySource | None = None,
    ) -> None:
        # ``_bridge`` remains accepted while application composition shares one
        # construction site with browser-backed sources. This vertical slice
        # itself uses public read-only HTTP contracts only.
        self._rail_source = rail_source or Rail12306CurrentCatalogSource(
            max_candidates_per_leg=12
        )
        self._lodging_source = lodging_source or TripComCurrentLodgingSource()
        self._ferry_source = ferry_source or IComCurrentTransportSource()
        self._activity_source = activity_source or IComCurrentActivitySource()

    async def catalog_for(
        self,
        intent: TravelIntent,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        # Ferry and activity adapters both use the same display-only USD/CNY
        # reference.  Fetch it once per catalog rather than once per leg or
        # candidate; a failed FX read is still an honest unpriced boundary.
        shared_reference_rate = await self._shared_reference_rate(intent)
        rail, lodging, ferry, activities = await asyncio.gather(
            self._rail_source.catalog_for(intent),
            self._lodging_source.catalog_for(intent),
            self._ferry_source.catalog_for(
                intent,
                reference_rate=shared_reference_rate,
                fetch_reference=False,
            ),
            self._activity_source.catalog_for(
                intent,
                reference_rate=shared_reference_rate,
                fetch_reference=False,
            ),
        )
        contracts = (
            *rail.contracts,
            *lodging.contracts,
            *ferry[1],
            *activities.contracts,
        )
        return (
            OfferCatalog(
                transports=(*rail.transports, *ferry[0]),
                stays=lodging.stays,
                activities=activities.activities,
                query_tasks=(
                    *rail.query_task_ids,
                    *lodging.query_task_ids,
                    *ferry[3],
                    *activities.query_task_ids,
                ),
                source_statuses=(
                    *rail.source_statuses,
                    *lodging.source_statuses,
                    *ferry[2],
                    *activities.source_statuses,
                ),
                source_mode="current",
            ),
            contracts,
        )

    async def _shared_reference_rate(
        self,
        intent: TravelIntent,
    ) -> UsdCnyReferenceRate | None:
        if not _needs_icom_reference(intent):
            return None
        try:
            return await fetch_usd_cny_reference_rate()
        except Exception:
            return None

    async def catalog_for_stays(
        self,
        intent: TravelIntent,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        """Return current lodging observations without querying transport sources."""

        lodging = await self._lodging_source.catalog_for(intent)
        return (
            OfferCatalog(
                stays=lodging.stays,
                query_tasks=lodging.query_task_ids,
                source_statuses=lodging.source_statuses,
                source_mode="current",
            ),
            lodging.contracts,
        )


__all__ = ["CurrentComplexOfferProvider"]


def _needs_icom_reference(intent: TravelIntent) -> bool:
    known_places = {
        "mle",
        "male",
        "malé",
        "马累机场",
        "维拉纳国际机场",
        "velana international airport",
        "机场",
        "maafushi",
        "马富施",
        "马富士",
    }
    if any(
        "maafushi" in requirement.place_id.lower()
        or "马富施" in requirement.place_id
        or "马富士" in requirement.place_id
        for requirement in intent.activity_requirements
    ):
        return True
    return any(
        leg.origin_place_id.strip().lower() in known_places
        and leg.destination_place_id.strip().lower() in known_places
        for leg in intent.route_legs
    )
