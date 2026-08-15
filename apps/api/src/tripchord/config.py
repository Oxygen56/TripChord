from __future__ import annotations

import re
from functools import lru_cache
from typing import TYPE_CHECKING

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from tripchord.agents.model_gateway import ModelClientConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="TRIPCHORD_",
        extra="ignore",
        populate_by_name=True,
    )

    env: str = "development"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    database_url: str = "sqlite+aiosqlite:///./tripchord.db"
    redis_url: str | None = None
    rate_limit_requests: int = Field(default=30, ge=1, le=10000)
    rate_limit_window_seconds: int = Field(default=60, ge=1, le=3600)
    log_level: str = "INFO"
    auth_required: bool = False
    auth_tokens: dict[str, str] = Field(default_factory=dict)
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
    browser_bridge_enabled: bool = False
    browser_bridge_token: str | None = Field(default=None, repr=False)
    browser_bridge_control_token: str | None = Field(default=None, repr=False)
    browser_companion_auto_reload_enabled: bool = False
    browser_bridge_allowed_origin_regex: str = (
        r"^(chrome-extension://[a-p]{32}|"
        r"http://(?:127\.0\.0\.1|localhost)(?::\d+)?)$"
    )
    browser_bridge_require_all_providers: bool = True
    browser_bridge_task_timeout_seconds: int = Field(default=180, ge=30, le=300)
    # A strict three-date live-v4 run executes date pairs serially so that
    # receipts stay coherent within the six global browser leases.  The old
    # 20-minute ceiling could expire after only two pairs when a provider used
    # its bounded retry.  Keep the outer guard finite, but large enough for the
    # frozen three-pair search plus publication refresh.
    browser_bridge_flexible_timeout_seconds: int = Field(default=3600, ge=60, le=3600)
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(
        default="https://api.anthropic.com",
        validation_alias="ANTHROPIC_BASE_URL",
    )
    anthropic_model: str = Field(
        default="claude-sonnet-4-5",
        validation_alias="ANTHROPIC_MODEL",
    )
    anthropic_small_fast_model: str | None = Field(
        default=None,
        validation_alias="ANTHROPIC_SMALL_FAST_MODEL",
    )
    model_provider: str = Field(default="none", validation_alias="MODEL_PROVIDER")
    model_api_key: str | None = Field(default=None, validation_alias="MODEL_API_KEY")
    model_base_url: str | None = Field(default=None, validation_alias="MODEL_BASE_URL")
    model_name: str | None = Field(default=None, validation_alias="MODEL_NAME")
    model_fast_name: str | None = Field(default=None, validation_alias="MODEL_FAST_NAME")
    model_timeout_seconds: float = Field(
        default=45,
        gt=0,
        le=300,
        validation_alias="MODEL_TIMEOUT_SECONDS",
    )
    model_max_attempts: int = Field(
        default=3,
        ge=1,
        le=8,
        validation_alias="MODEL_MAX_ATTEMPTS",
    )
    model_retry_base_delay_seconds: float = Field(
        default=0.25,
        ge=0,
        le=30,
        validation_alias="MODEL_RETRY_BASE_DELAY_SECONDS",
    )
    model_retry_max_delay_seconds: float = Field(
        default=4,
        ge=0,
        le=60,
        validation_alias="MODEL_RETRY_MAX_DELAY_SECONDS",
    )
    model_input_usd_per_million_tokens: float = Field(
        default=0,
        ge=0,
        validation_alias="MODEL_INPUT_USD_PER_MILLION_TOKENS",
    )
    model_output_usd_per_million_tokens: float = Field(
        default=0,
        ge=0,
        validation_alias="MODEL_OUTPUT_USD_PER_MILLION_TOKENS",
    )
    model_agents_required: bool = Field(
        default=False,
        validation_alias="MODEL_AGENTS_REQUIRED",
    )
    model_response_format_mode: str = Field(
        default="auto",
        validation_alias="MODEL_RESPONSE_FORMAT_MODE",
    )
    model_thinking_mode: str = Field(
        default="auto",
        validation_alias="MODEL_THINKING_MODE",
    )
    adaptive_agent_scaling_enabled: bool = Field(
        default=True,
        validation_alias="ADAPTIVE_AGENT_SCALING_ENABLED",
    )
    model_http2_enabled: bool = Field(
        default=False,
        validation_alias="MODEL_HTTP2_ENABLED",
    )
    model_http_max_connections: int = Field(
        default=12,
        ge=1,
        le=128,
        validation_alias="MODEL_HTTP_MAX_CONNECTIONS",
    )
    model_http_max_keepalive_connections: int = Field(
        default=12,
        ge=0,
        le=128,
        validation_alias="MODEL_HTTP_MAX_KEEPALIVE_CONNECTIONS",
    )
    model_http_max_in_flight: int = Field(
        default=12,
        ge=1,
        le=12,
        validation_alias="MODEL_HTTP_MAX_IN_FLIGHT",
    )
    memory_state_path: str | None = Field(
        default=".runtime/agent-memory.json",
        validation_alias="MEMORY_STATE_PATH",
    )
    memory_corruption_policy: str = Field(
        default="fail_closed",
        validation_alias="MEMORY_CORRUPTION_POLICY",
    )
    memory_persist_sensitive: bool = Field(
        default=False,
        validation_alias="MEMORY_PERSIST_SENSITIVE",
    )
    live_run_cache_state_path: str | None = ".runtime/live-run-cache.json"
    live_run_cache_corruption_policy: str = "fail_closed"
    live_planning_job_registry_state_path: str | None = None

    @model_validator(mode="after")
    def validate_security(self) -> Settings:
        if self.auth_required and not self.auth_tokens:
            raise ValueError("auth_tokens must be configured when auth_required is true")
        if self.auth_required and "*" in self.cors_origins:
            raise ValueError("wildcard CORS is forbidden when authentication is required")
        if self.browser_bridge_enabled and (
            self.browser_bridge_token is None or len(self.browser_bridge_token) < 32
        ):
            raise ValueError(
                "browser_bridge_token must contain at least 32 characters "
                "when the local browser bridge is enabled"
            )
        if (
            self.browser_bridge_control_token is not None
            and len(self.browser_bridge_control_token) < 32
        ):
            raise ValueError(
                "browser_bridge_control_token must contain at least 32 characters"
            )
        if (
            self.browser_bridge_token is not None
            and self.browser_bridge_control_token is not None
            and self.browser_bridge_token == self.browser_bridge_control_token
        ):
            raise ValueError(
                "browser bridge and browser companion control tokens must be distinct"
            )
        if self.browser_companion_auto_reload_enabled and not self.browser_bridge_enabled:
            raise ValueError(
                "browser_companion_auto_reload_enabled requires browser_bridge_enabled"
            )
        try:
            re.compile(self.browser_bridge_allowed_origin_regex)
        except re.error as exc:
            raise ValueError("browser_bridge_allowed_origin_regex must be valid") from exc
        if self.model_provider not in {"none", "anthropic", "openai_compatible"}:
            raise ValueError("model_provider must be none, anthropic, or openai_compatible")
        if self.model_provider == "anthropic" and not (
            self.model_api_key or self.anthropic_api_key
        ):
            raise ValueError("Anthropic model provider requires an API key")
        if self.model_agents_required and self.resolved_model_provider == "none":
            raise ValueError("MODEL_AGENTS_REQUIRED requires a configured model provider")
        if self.model_response_format_mode not in {
            "auto",
            "json_schema",
            "json_object",
            "prompt_only",
        }:
            raise ValueError(
                "model_response_format_mode must be auto, json_schema, json_object, or prompt_only"
            )
        if self.model_thinking_mode not in {"auto", "disabled", "enabled"}:
            raise ValueError(
                "model_thinking_mode must be auto, disabled, or enabled"
            )
        if self.model_http_max_keepalive_connections > self.model_http_max_connections:
            raise ValueError(
                "model_http_max_keepalive_connections cannot exceed "
                "model_http_max_connections"
            )
        if self.memory_corruption_policy not in {"fail_closed", "quarantine"}:
            raise ValueError("memory_corruption_policy must be fail_closed or quarantine")
        if self.live_run_cache_corruption_policy not in {"fail_closed", "quarantine"}:
            raise ValueError(
                "live_run_cache_corruption_policy must be fail_closed or quarantine"
            )
        return self

    @property
    def resolved_model_provider(self) -> str:
        # A credential appearing in a parent shell must never silently enable
        # paid model calls (especially in tests). Legacy Anthropic fields remain
        # valid only after MODEL_PROVIDER=anthropic is selected explicitly.
        return self.model_provider

    def model_client_config(self, *, fast: bool = False) -> ModelClientConfig | None:
        """Return a gateway config lazily to keep Settings free of import cycles."""

        from tripchord.agents.model_gateway import (
            ModelClientConfig,
            ModelPricing,
            ModelProviderName,
            ModelResponseFormatMode,
            ModelRetryPolicy,
            ModelThinkingMode,
        )

        provider = self.resolved_model_provider
        if provider == "none":
            return None
        api_key: str | None
        base_url: str | None
        model: str
        if provider == "anthropic":
            api_key = self.model_api_key or self.anthropic_api_key
            base_url = self.model_base_url or self.anthropic_base_url
            model = (
                self.model_fast_name
                or self.anthropic_small_fast_model
                or self.model_name
                or self.anthropic_model
                if fast
                else self.model_name or self.anthropic_model
            )
        else:
            api_key = self.model_api_key
            base_url = self.model_base_url
            selected_model = (
                self.model_fast_name if fast and self.model_fast_name else self.model_name
            )
            if not selected_model:
                raise ValueError("OpenAI-compatible model provider requires MODEL_NAME")
            model = selected_model
        return ModelClientConfig(
            provider=ModelProviderName(provider),
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=self.model_timeout_seconds,
            retry=ModelRetryPolicy(
                max_attempts=self.model_max_attempts,
                base_delay_seconds=self.model_retry_base_delay_seconds,
                max_delay_seconds=self.model_retry_max_delay_seconds,
            ),
            pricing=ModelPricing(
                input_usd_per_million_tokens=self.model_input_usd_per_million_tokens,
                output_usd_per_million_tokens=self.model_output_usd_per_million_tokens,
            ),
            response_format_mode=ModelResponseFormatMode(self.model_response_format_mode),
            thinking_mode=ModelThinkingMode(self.model_thinking_mode),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
