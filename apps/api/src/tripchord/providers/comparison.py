from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from tripchord.domain.offers import TravelOffer


@dataclass(frozen=True)
class ComparableOfferGroup:
    comparison_key: str
    currency: str
    offers: tuple[TravelOffer, ...]

    @property
    def lowest_total(self) -> Decimal:
        return self.offers[0].price.total.amount


def group_comparable_offers(offers: tuple[TravelOffer, ...]) -> tuple[ComparableOfferGroup, ...]:
    groups: dict[tuple[str, str], list[TravelOffer]] = defaultdict(list)
    for offer in offers:
        groups[(offer.comparison_key, offer.price.total.currency)].append(offer)
    result: list[ComparableOfferGroup] = []
    for (comparison_key, currency), candidates in groups.items():
        ordered = tuple(sorted(candidates, key=lambda offer: offer.price.total.amount))
        result.append(
            ComparableOfferGroup(
                comparison_key=comparison_key,
                currency=currency,
                offers=ordered,
            )
        )
    return tuple(sorted(result, key=lambda group: (group.currency, group.lowest_total)))
