"""Current bounded multi-city catalog from price-complete public sources."""

from __future__ import annotations

import asyncio

from tripchord.planning.complex_trip import OfferCatalog, PriceContract, TravelIntent
from tripchord.providers.browser_bridge import BrowserTaskBridge
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
    ) -> None:
        # ``_bridge`` remains accepted while application composition shares one
        # construction site with browser-backed sources. This vertical slice
        # itself uses public read-only HTTP contracts only.
        self._rail_source = rail_source or Rail12306CurrentCatalogSource(
            max_candidates_per_leg=12
        )
        self._lodging_source = lodging_source or TripComCurrentLodgingSource()

    async def catalog_for(
        self,
        intent: TravelIntent,
    ) -> tuple[OfferCatalog, tuple[PriceContract, ...]]:
        rail, lodging = await asyncio.gather(
            self._rail_source.catalog_for(intent),
            self._lodging_source.catalog_for(intent),
        )
        contracts = (*rail.contracts, *lodging.contracts)
        return (
            OfferCatalog(
                transports=rail.transports,
                stays=lodging.stays,
                query_tasks=(*rail.query_task_ids, *lodging.query_task_ids),
                source_statuses=(*rail.source_statuses, *lodging.source_statuses),
                source_mode="current",
            ),
            contracts,
        )

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
