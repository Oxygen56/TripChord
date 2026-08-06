"""v0.8 secret-redaction contract tests (secrets never enter logs/telemetry)."""

from __future__ import annotations

from tripchord.security.secrets import (
    SecretRedactionPolicy,
    contains_secret,
    redact_secrets,
)


def test_redact_secrets_replaces_api_key_values() -> None:
    sample = "call with model_api_key=sk-8ea0d6bb45344b7fb904c92148251f75"
    redacted = redact_secrets(sample)
    assert "sk-8ea0d6bb45344b7fb904c92148251f75" not in redacted
    assert not contains_secret(redacted)


def test_redact_secrets_leaves_readable_diagnostics() -> None:
    sample = "source ctrip returned 3 quotes; barrier released"
    assert redact_secrets(sample) == sample
    assert not contains_secret(sample)


def test_redact_secrets_handles_key_value_assignments() -> None:
    sample = "authorization token=sk-abc123def456ghijklmnop"
    redacted = redact_secrets(sample)
    assert contains_secret(redacted) is False


def test_secret_redaction_policy_validate() -> None:
    policy = SecretRedactionPolicy()
    assert policy.validate_redaction(
        "provider error; api_key=sk-abc123def456ghijklmnop was rejected"
    )
    assert policy.validate_redaction("all sources terminal; no secret")
