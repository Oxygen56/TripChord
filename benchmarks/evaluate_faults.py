from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import Any

from tripchord.domain.offers import OfferKind, TravelOffer
from tripchord.providers.base import (
    OfferSearchQuery,
    ProviderError,
    ProviderRegistry,
)
from tripchord.providers.replay import ReplayOfferProvider

ROOT = Path(__file__).resolve().parents[1]


class FailingProvider:
    name = "injected-failure"
    supported_kinds = frozenset({OfferKind.FLIGHT})

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        raise ProviderError(self.name, "injected", "deterministic injected failure")

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        raise ProviderError(self.name, "injected", "deterministic injected failure")


class HangingProvider:
    name = "injected-timeout"
    supported_kinds = frozenset({OfferKind.FLIGHT})

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        await asyncio.sleep(60)
        return ()

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        await asyncio.sleep(60)
        return offer


async def evaluate_async(query_count: int = 100) -> dict[str, Any]:
    registry = ProviderRegistry(
        (
            ReplayOfferProvider(ROOT / "data" / "replay" / "offers.json"),
            FailingProvider(),
            HangingProvider(),
        ),
        provider_timeout_seconds=0.02,
    )
    query = OfferSearchQuery(
        kind=OfferKind.FLIGHT,
        origin="上海",
        destination="北京",
        start_date=date(2026, 10, 1),
    )
    started = perf_counter()
    results = await asyncio.gather(*(registry.search(query) for _ in range(query_count)))
    wall_seconds = perf_counter() - started
    return {
        "query_count": query_count,
        "partial_success_rate": mean(bool(result.offers) for result in results),
        "failure_isolation_rate": mean(len(result.failures) == 2 for result in results),
        "timeout_classification_rate": mean(
            any(
                failure.code == "provider_timeout" and failure.retryable
                for failure in result.failures
            )
            for result in results
        ),
        "wall_seconds": wall_seconds,
        "queries_per_second": query_count / wall_seconds,
    }


def evaluate(query_count: int = 100) -> dict[str, Any]:
    return asyncio.run(evaluate_async(query_count))


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
