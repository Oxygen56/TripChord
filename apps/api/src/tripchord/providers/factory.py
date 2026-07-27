from pathlib import Path

from tripchord.config import Settings
from tripchord.domain.source import SourceMode
from tripchord.providers.amadeus import AmadeusConfig, AmadeusFlightProvider
from tripchord.providers.amap import AmapConfig, AmapTravelDataProvider
from tripchord.providers.base import OfferProvider, ProviderRegistry
from tripchord.providers.booking import BookingAccommodationProvider, BookingConfig
from tripchord.providers.replay import ReplayOfferProvider


def build_provider_registry(settings: Settings, root: Path) -> ProviderRegistry:
    providers: list[OfferProvider] = [
        ReplayOfferProvider(root / "data" / "replay" / "offers.json")
    ]

    if settings.amadeus_client_id and settings.amadeus_client_secret:
        production = settings.amadeus_environment == "production"
        providers.append(
            AmadeusFlightProvider(
                AmadeusConfig(
                    client_id=settings.amadeus_client_id,
                    client_secret=settings.amadeus_client_secret,
                    base_url=(
                        "https://api.amadeus.com"
                        if production
                        else "https://test.api.amadeus.com"
                    ),
                    source_mode=SourceMode.PRODUCTION if production else SourceMode.SANDBOX,
                )
            )
        )

    if settings.booking_api_token and settings.booking_affiliate_id:
        production = settings.booking_environment == "production"
        providers.append(
            BookingAccommodationProvider(
                BookingConfig(
                    api_token=settings.booking_api_token,
                    affiliate_id=settings.booking_affiliate_id,
                    base_url=(
                        "https://demandapi.booking.com/3.2"
                        if production
                        else "https://demandapi-sandbox.booking.com/3.2"
                    ),
                    source_mode=SourceMode.PRODUCTION if production else SourceMode.SANDBOX,
                )
            )
        )

    return ProviderRegistry(providers)


def build_amap_provider(settings: Settings) -> AmapTravelDataProvider | None:
    if not settings.amap_api_key:
        return None
    return AmapTravelDataProvider(AmapConfig(api_key=settings.amap_api_key))
