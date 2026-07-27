from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRIPCHORD_",
        extra="ignore",
    )

    env: str = "development"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    database_url: str = "sqlite+aiosqlite:///./tripchord.db"
    redis_url: str | None = None
    amadeus_client_id: str | None = Field(default=None, validation_alias="AMADEUS_CLIENT_ID")
    amadeus_client_secret: str | None = Field(
        default=None,
        validation_alias="AMADEUS_CLIENT_SECRET",
    )
    amadeus_environment: str = Field(default="test", validation_alias="AMADEUS_ENVIRONMENT")
    booking_api_token: str | None = Field(default=None, validation_alias="BOOKING_API_TOKEN")
    booking_affiliate_id: str | None = Field(
        default=None,
        validation_alias="BOOKING_AFFILIATE_ID",
    )
    booking_environment: str = Field(default="sandbox", validation_alias="BOOKING_ENVIRONMENT")
    amap_api_key: str | None = Field(default=None, validation_alias="AMAP_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
