"""Secret-safe redaction and validation (v0.8 local product experience).

The v0.8 contract requires that secrets never enter logs, telemetry, the
repository, frontend persistence, the travel database, model context or
captured evidence.  This module provides a single deterministic redaction
primitive and a validation gate used by the launcher and the observability
layer.
"""

from __future__ import annotations

import re

from tripchord.domain.common import DomainModel

_SECRET_VALUE_PATTERN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,})\b")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|auth[_-]?token)\b"
    r"\s*=\s*([^\s,;\"']+)"
)
_REDACTED = "<redacted>"


def redact_secrets(text: str) -> str:
    """Replace likely secret values with a fixed placeholder.

    Two passes: first any ``sk-...`` shaped value, then any assignment whose key
    name indicates a secret.  Already-redacted values are left untouched so the
    function is idempotent.
    """
    if not text:
        return text
    redacted = _SECRET_VALUE_PATTERN.sub(_REDACTED, text)

    def replace_assignment(match: re.Match[str]) -> str:
        key, value = match.group(1), match.group(2)
        if value == _REDACTED or value.startswith(f"{_REDACTED}"):
            return f"{key}={value}"
        return f"{key}={_REDACTED}"

    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(replace_assignment, redacted)
    return redacted


def contains_secret(text: str) -> bool:
    """Return whether a diagnostic string still carries a likely secret value."""
    if not text:
        return False
    if _SECRET_VALUE_PATTERN.search(text):
        return True
    for match in _SECRET_ASSIGNMENT_PATTERN.finditer(text):
        value = match.group(2)
        if value != _REDACTED and not value.startswith(_REDACTED):
            return True
    return False


class SecretRedactionPolicy(DomainModel):
    """Declares the secrets a local install is allowed to hold and where."""

    allowed_env_var_names: frozenset[str] = frozenset(
        {
            "AMADEUS_CLIENT_SECRET",
            "BOOKING_API_TOKEN",
            "AMAP_API_KEY",
            "ANTHROPIC_API_KEY",
            "MODEL_API_KEY",
            "TRIPCHORD_BROWSER_BRIDGE_TOKEN",
            "TRIPCHORD_BROWSER_BRIDGE_CONTROL_TOKEN",
            "OPENAI_API_KEY",
        }
    )

    def validate_redaction(self, sample: str) -> bool:
        return not contains_secret(redact_secrets(sample))
