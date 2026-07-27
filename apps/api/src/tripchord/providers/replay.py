from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter

from tripchord.domain.offers import OfferKind, PriceState, TravelOffer
from tripchord.domain.source import SourceMode
from tripchord.providers.base import OfferSearchQuery, ProviderError


class ReplayOfferProvider:
    """Deterministic provider for tests and offline benchmark runs."""

    name = "replay"
    supported_kinds = frozenset(OfferKind)

    def __init__(self, fixture_path: Path) -> None:
        self._offers = TypeAdapter(tuple[TravelOffer, ...]).validate_json(fixture_path.read_text())

    async def search(self, query: OfferSearchQuery) -> tuple[TravelOffer, ...]:
        return tuple(
            offer
            for offer in self._offers
            if offer.kind == query.kind
            and str(offer.metadata.get("destination", "")) == query.destination
        )

    async def revalidate(self, offer: TravelOffer) -> TravelOffer:
        if offer.source.mode != SourceMode.REPLAY:
            raise ProviderError(
                self.name,
                "unsupported_source",
                "replay can only handle replay offers",
            )
        matched = next((candidate for candidate in self._offers if candidate.id == offer.id), None)
        if matched is None:
            raise ProviderError(self.name, "offer_not_found", "replay offer does not exist")
        metadata = dict(matched.metadata)
        metadata["replay_checked_at"] = datetime.now(UTC).isoformat()
        return matched.model_copy(
            update={"price_state": PriceState.ESTIMATED, "metadata": metadata}
        )
