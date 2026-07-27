from datetime import UTC, datetime
from enum import StrEnum

from pydantic import field_validator, model_validator

from tripchord.domain.common import DomainModel


class SourceMode(StrEnum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"
    REPLAY = "replay"
    USER_SNAPSHOT = "user_snapshot"


class SourceRecord(DomainModel):
    provider: str
    mode: SourceMode
    request_id: str | None = None
    captured_at: datetime
    expires_at: datetime | None = None

    @field_validator("captured_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_expiry(self) -> "SourceRecord":
        if self.expires_at is not None and self.expires_at <= self.captured_at:
            raise ValueError("expires_at must be after captured_at")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.expires_at is None or reference < self.expires_at

