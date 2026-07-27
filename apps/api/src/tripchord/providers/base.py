from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.domain.offers import OfferKind, TravelOffer


class CabinClass(StrEnum):
    ECONOMY = "ECONOMY"
    PREMIUM_ECONOMY = "PREMIUM_ECONOMY"
    BUSINESS = "BUSINESS"
    FIRST = "FIRST"


class OfferSearchQuery(DomainModel):
    kind: OfferKind
    origin: str | None = None
    destination: str
    start_date: date
    end_date: date | None = None
    adults: int = Field(default=1, ge=1, le=9)
    children_ages: tuple[int, ...] = ()
    rooms: int = Field(default=1, ge=1, le=8)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    cabin_class: CabinClass = CabinClass.ECONOMY
    booker_country: str = Field(default="cn", min_length=2, max_length=2)
    provider_location_ids: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_by_kind(self) -> OfferSearchQuery:
        if self.kind in {OfferKind.FLIGHT, OfferKind.RAIL} and not self.origin:
            raise ValueError("transport searches require an origin")
        if self.kind == OfferKind.LODGING and self.end_date is None:
            raise ValueError("lodging searches require a checkout date")
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class ProviderFailure(DomainModel):
    provider: str
    code: str
    message: str
    retryable: bool = False


class OfferSearchResult(DomainModel):
    query: OfferSearchQuery
    searched_at: datetime
    offers: tuple[TravelOffer, ...]
    failures: tuple[ProviderFailure, ...] = ()


class OfferProvider(Protocol):
    name: str
    supported_kinds: frozenset[OfferKind]

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]: ...

    async def revalidate(self, offer: TravelOffer) -> TravelOffer: ...


class ProviderError(RuntimeError):
    def __init__(self, provider: str, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable


class ProviderRegistry:
    def __init__(
        self,
        providers: Iterable[OfferProvider] = (),
        *,
        provider_timeout_seconds: float = 8,
    ) -> None:
        self._providers = {provider.name: provider for provider in providers}
        self._provider_timeout_seconds = provider_timeout_seconds

    def register(self, provider: OfferProvider) -> None:
        if provider.name in self._providers:
            raise ValueError(f"provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    async def search(self, query: OfferSearchQuery) -> OfferSearchResult:
        providers = [
            provider
            for provider in self._providers.values()
            if query.kind in provider.supported_kinds
        ]
        settled = await asyncio.gather(
            *(self._search_one(provider, query) for provider in providers),
            return_exceptions=True,
        )
        offers: list[TravelOffer] = []
        failures: list[ProviderFailure] = []
        for provider, result in zip(providers, settled, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, ProviderError):
                    failures.append(
                        ProviderFailure(
                            provider=provider.name,
                            code=result.code,
                            message=str(result),
                            retryable=result.retryable,
                        )
                    )
                else:
                    failures.append(
                        ProviderFailure(
                            provider=provider.name,
                            code="unexpected_error",
                            message=str(result),
                        )
                    )
            else:
                offers.extend(result)
        return OfferSearchResult(
            query=query,
            searched_at=datetime.now(UTC),
            offers=tuple(offers),
            failures=tuple(failures),
        )

    async def _search_one(
        self,
        provider: OfferProvider,
        query: OfferSearchQuery,
    ) -> tuple[TravelOffer, ...]:
        try:
            return await asyncio.wait_for(
                provider.search(query),
                timeout=self._provider_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ProviderError(
                provider.name,
                "provider_timeout",
                f"provider exceeded {self._provider_timeout_seconds:g}s deadline",
                retryable=True,
            ) from exc

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        provider = self._providers.get(offer.source.provider)
        if provider is None:
            raise ProviderError(
                offer.source.provider,
                "provider_not_registered",
                "the offer provider is not registered",
            )
        return await provider.revalidate(offer)
