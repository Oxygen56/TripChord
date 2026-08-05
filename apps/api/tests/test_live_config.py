from __future__ import annotations

import pytest
from pydantic import ValidationError
from tripchord.agents.model_gateway import ModelThinkingMode
from tripchord.config import Settings


def test_browser_bridge_requires_long_pairing_token_when_enabled() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(
            _env_file=None,
            browser_bridge_enabled=True,
            browser_bridge_token="too-short",
        )


def test_live_model_and_bridge_settings_accept_explicit_local_configuration() -> None:
    settings = Settings(
        _env_file=None,
        browser_bridge_enabled=True,
        browser_bridge_token="x" * 32,
        browser_bridge_control_token="y" * 32,
        anthropic_api_key="test-only",
        anthropic_model="model-strong",
        anthropic_small_fast_model="model-fast",
    )

    assert settings.browser_bridge_require_all_providers is True
    assert settings.browser_bridge_control_token == "y" * 32
    assert "chrome-extension" in settings.browser_bridge_allowed_origin_regex
    assert settings.browser_bridge_task_timeout_seconds == 180
    assert settings.browser_bridge_flexible_timeout_seconds == 3600
    assert settings.anthropic_model == "model-strong"
    assert settings.anthropic_small_fast_model == "model-fast"
    assert settings.adaptive_agent_scaling_enabled is True
    assert settings.model_client_config() is None


def test_browser_bridge_rejects_short_control_token() -> None:
    with pytest.raises(ValidationError, match="browser_bridge_control_token"):
        Settings(
            _env_file=None,
            browser_bridge_control_token="too-short",
        )


def test_browser_bridge_secrets_are_repr_safe_and_must_be_distinct() -> None:
    bridge_token = "bridge-secret-used-only-by-test-0001"
    control_token = "control-secret-used-only-by-test-0001"
    settings = Settings(
        _env_file=None,
        browser_bridge_enabled=True,
        browser_bridge_token=bridge_token,
        browser_bridge_control_token=control_token,
    )

    rendered = repr(settings)
    assert bridge_token not in rendered
    assert control_token not in rendered
    with pytest.raises(ValidationError, match="must be distinct"):
        Settings(
            _env_file=None,
            browser_bridge_enabled=True,
            browser_bridge_token=bridge_token,
            browser_bridge_control_token=bridge_token,
        )


def test_companion_auto_reload_requires_an_explicit_enabled_bridge() -> None:
    with pytest.raises(ValidationError, match="requires browser_bridge_enabled"):
        Settings(
            _env_file=None,
            browser_companion_auto_reload_enabled=True,
        )

    settings = Settings(
        _env_file=None,
        browser_bridge_enabled=True,
        browser_bridge_token="bridge-secret-used-only-by-test-0002",
        browser_companion_auto_reload_enabled=True,
    )
    assert settings.browser_companion_auto_reload_enabled is True
    assert settings.browser_bridge_control_token is None


def test_model_provider_must_be_explicit_and_required_agents_fail_closed() -> None:
    inherited_key_only = Settings(
        _env_file=None,
        anthropic_api_key="test-only",
    )
    assert inherited_key_only.resolved_model_provider == "none"
    assert inherited_key_only.model_client_config() is None

    configured = Settings(
        _env_file=None,
        model_provider="anthropic",
        anthropic_api_key="test-only",
        model_name="model-strong",
        model_fast_name="model-fast",
        model_agents_required=True,
    )
    primary = configured.model_client_config()
    fast = configured.model_client_config(fast=True)
    assert primary is not None and primary.model == "model-strong"
    assert fast is not None and fast.model == "model-fast"

    with pytest.raises(ValidationError, match="MODEL_AGENTS_REQUIRED"):
        Settings(_env_file=None, model_agents_required=True)


def test_openai_compatible_thinking_mode_is_explicit_and_closed_enum() -> None:
    configured = Settings(
        _env_file=None,
        model_provider="openai_compatible",
        model_api_key="test-only",
        model_base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
    )
    enabled = configured.model_copy(update={"model_thinking_mode": "enabled"})

    default_config = configured.model_client_config()
    enabled_config = enabled.model_client_config()
    assert default_config is not None
    assert default_config.thinking_mode == ModelThinkingMode.AUTO
    assert enabled_config is not None
    assert enabled_config.thinking_mode == ModelThinkingMode.ENABLED

    with pytest.raises(ValidationError, match="model_thinking_mode"):
        Settings(_env_file=None, model_thinking_mode="implicit-provider-default")


def test_memory_corruption_policy_is_closed_enum() -> None:
    with pytest.raises(ValidationError, match="fail_closed or quarantine"):
        Settings(_env_file=None, memory_corruption_policy="ignore-and-continue")


def test_live_run_cache_persistence_defaults_on_and_can_be_disabled() -> None:
    configured = Settings(_env_file=None)
    disabled = Settings(_env_file=None, live_run_cache_state_path=None)

    assert configured.live_run_cache_state_path == ".runtime/live-run-cache.json"
    assert configured.live_run_cache_corruption_policy == "fail_closed"
    assert disabled.live_run_cache_state_path is None

    with pytest.raises(ValidationError, match="fail_closed or quarantine"):
        Settings(
            _env_file=None,
            live_run_cache_corruption_policy="ignore-and-continue",
        )


def test_live_timeout_settings_have_separate_bounded_ranges() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            browser_bridge_task_timeout_seconds=301,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            browser_bridge_flexible_timeout_seconds=59,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            browser_bridge_flexible_timeout_seconds=3601,
        )
