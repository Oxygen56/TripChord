"""Official booking handoff, URL policy and revalidation (v0.5).

TripChord never books, pays or locks inventory: it hands the user off to the
official platform page for the exact component they just re-priced.  This module
is the deterministic core of that contract:

- :class:`OfficialDetailLocator` declares, per provider scope, whether a stable
  detail page, a pre-filled search page or only a parameter card is supported.
- :class:`HandoffURLPolicy` rejects dangerous URL mutations per hop: short
  links, open redirects, login/account/order/checkout/payment/coupon paths and
  unknown hosts.
- :class:`RevalidationReceipt` proves a same-provider, same-component
  re-pricing happened and is still fresh.
- :class:`OfficialHandoff` binds one component to its plan version, offer,
  query and revalidation receipt; it is short-lived, single-use and invalidated
  the moment the underlying offer changes.
- :class:`ComponentHandoffChecklist` is the per-component action order that the
  UI renders — re-price first, then (only if still fresh and unchanged) go to
  the official page.  A click can never create a booked state here.

Every URL policy decision is pure and deterministic so automated tests can
prove "dangerous URL mutations are rejected, safe official hops are allowed".
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self
from urllib.parse import parse_qsl, urlparse

from pydantic import Field, model_validator

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import ProviderScopeKey

HANDOFF_SCHEMA_VERSION = "tripchord-official-handoff-v1"
HANDOFF_MAX_AGE_SECONDS = 300
HANDOFF_MAX_HOPS = 4

_FORBIDDEN_PATH_MARKERS: frozenset[str] = frozenset(
    {
        "login",
        "signin",
        "sign-in",
        "account",
        "order",
        "orders",
        "checkout",
        "cashier",
        "payment",
        "pay",
        "coupon",
        "redeem",
        "book",
        "booking",
        "confirm",
        "cart",
        "passport",
        "user",
    }
)
_FORBIDDEN_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "token",
        "cookie",
        "session",
        "auth",
        "coupon",
        "promo",
        "payment",
        "checkout",
    }
)


class OfficialHandoffState(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    USED = "used"
    INVALIDATED = "invalidated"


class LocatorKind(StrEnum):
    DETAIL_PAGE = "detail_page"
    PREFILLED_SEARCH = "prefilled_search"
    PARAM_CARD_ONLY = "param_card_only"


class RevalidationOutcome(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NOT_FOUND = "not_found"


class OfficialDetailLocator(DomainModel):
    """One provider scope's declared official handoff capability."""

    scope: ProviderScopeKey
    kind: LocatorKind
    official_hosts: tuple[str, ...] = Field(min_length=1)
    # Audited path prefixes that are allowed for this scope.  Empty means the
    # platform only exposes a parameter card (no stable deep link).
    allowed_path_prefixes: tuple[str, ...] = ()

    def allows_host(self, host: str) -> bool:
        normalized = host.lower()
        return any(
            normalized == official or normalized.endswith(f".{official}")
            for official in self.official_hosts
        )


class HandoffURLPolicy(DomainModel):
    """Per-hop validation for a single official-destination URL."""

    schema_version: str = HANDOFF_SCHEMA_VERSION
    locator: OfficialDetailLocator
    max_hops: int = Field(default=HANDOFF_MAX_HOPS, ge=1, le=HANDOFF_MAX_HOPS)

    def url_policy_sha(self) -> str:
        canonical = {
            "schema": self.schema_version,
            "scope": self.locator.scope.key,
            "hosts": tuple(self.locator.official_hosts),
            "kind": self.locator.kind.value,
            "prefixes": tuple(self.locator.allowed_path_prefixes),
            "max_hops": self.max_hops,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def validate_url(self, url: str) -> tuple[bool, str | None]:
        """Return ``(allowed, reason)`` for one official-destination URL.

        The URL must be https, on an official host, without credentials, and
        must not contain any forbidden path marker or query key.  Short links
        (URL shortening services) and open redirects are rejected by requiring
        the host to be official and the path to be non-empty and allowed.
        """
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False, "handoff must use https"
        host = (parsed.hostname or "").lower()
        if not self.locator.allows_host(host):
            return False, f"host {host!r} is not an official platform host"
        if parsed.username is not None or parsed.password is not None:
            return False, "handoff URLs must not embed credentials"
        if not parsed.path or parsed.path in {"/", ""}:
            return False, "official handoff requires a stable path, not a bare host"
        path_segments = {
            segment.lower().replace("-", "/").replace("_", "/")
            for segment in parsed.path.split("/")
            if segment
        }
        for marker in _FORBIDDEN_PATH_MARKERS:
            if any(marker in segment for segment in path_segments):
                return False, f"path contains forbidden marker {marker!r}"
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if any(key in _FORBIDDEN_QUERY_KEYS for key in query_keys):
            return False, "query contains a forbidden parameter"
        if self.locator.allowed_path_prefixes:
            normalized_path = parsed.path.lower().rstrip("/")
            allowed_prefix = any(
                normalized_path == prefix.rstrip("/")
                or normalized_path.startswith(prefix.rstrip("/") + "/")
                for prefix in self.locator.allowed_path_prefixes
            )
            if not allowed_prefix:
                return False, "path is outside the audited official path prefixes"
        return True, None


class RevalidationReceipt(DomainModel):
    """Short-lived proof of a same-provider, same-component re-pricing."""

    receipt_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    quote_id: str = Field(min_length=1)
    revalidated_at: datetime
    expires_at: datetime
    outcome: RevalidationOutcome
    total_for_party_cents: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.expires_at <= self.revalidated_at:
            raise ValueError("revalidation receipt must expire after it was created")
        if self.outcome == RevalidationOutcome.UNCHANGED and self.total_for_party_cents is None:
            raise ValueError("an unchanged revalidation receipt must carry the price")
        return self

    def is_fresh(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return self.revalidated_at <= reference < self.expires_at

    def receipt_sha256(self) -> str:
        canonical = {
            "schema": "tripchord-revalidation-receipt-v1",
            "receipt_id": self.receipt_id,
            "plan_version": self.plan_version,
            "component_id": self.component_id,
            "scope": self.scope.key,
            "quote_id": self.quote_id,
            "revalidated_at": self.revalidated_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "outcome": self.outcome.value,
            "total_for_party_cents": self.total_for_party_cents,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class OfficialHandoff(DomainModel):
    """One component's short-lived, single-use official booking hop."""

    handoff_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    locator: OfficialDetailLocator
    url: str = Field(min_length=1)
    query_fingerprint_sha256: str = Field(min_length=64, max_length=64)
    revalidation_receipt_sha256: str = Field(min_length=64, max_length=64)
    created_at: datetime
    expires_at: datetime
    state: OfficialHandoffState = OfficialHandoffState.VALID
    hops: int = Field(default=1, ge=1, le=HANDOFF_MAX_HOPS)
    url_policy: HandoffURLPolicy
    url_policy_sha256: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("handoff must expire after it is created")
        allowed, reason = self.url_policy.validate_url(self.url)
        if not allowed:
            raise ValueError(f"handoff URL failed the official URL policy: {reason}")
        if (
            self.state == OfficialHandoffState.VALID
            and self.url_policy_sha256 != self.url_policy.url_policy_sha()
        ):
            raise ValueError("handoff URL policy SHA does not match its policy")
        return self

    def is_usable(self, now: datetime | None = None) -> bool:
        reference = now or datetime.now(UTC)
        return (
            self.state is OfficialHandoffState.VALID
            and self.created_at <= reference < self.expires_at
        )

    def handoff_sha256(self) -> str:
        canonical = {
            "schema": HANDOFF_SCHEMA_VERSION,
            "handoff_id": self.handoff_id,
            "plan_version": self.plan_version,
            "component_id": self.component_id,
            "scope": self.scope.key,
            "url": self.url,
            "query_fingerprint_sha256": self.query_fingerprint_sha256,
            "revalidation_receipt_sha256": self.revalidation_receipt_sha256,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "url_policy_sha256": self.url_policy_sha256,
        }
        raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


class ComponentHandoffChecklist(DomainModel):
    """Per-component action order the UI must present in this exact order."""

    component_id: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    scope: ProviderScopeKey
    reprice_url: str | None = Field(default=None, min_length=1)
    official_handoff: OfficialHandoff | None = None
    revalidation_receipt: RevalidationReceipt | None = None
    suggested_next_step: str = Field(
        default="reprice",
        pattern="^(reprice|review_diff|go_to_official|blocked)$",
    )

    def can_go_to_official(self, now: datetime | None = None) -> bool:
        if self.official_handoff is None or self.revalidation_receipt is None:
            return False
        if not self.official_handoff.is_usable(now):
            return False
        if not self.revalidation_receipt.is_fresh(now):
            return False
        if self.revalidation_receipt.outcome is not RevalidationOutcome.UNCHANGED:
            return False
        return (
            self.official_handoff.revalidation_receipt_sha256
            == self.revalidation_receipt.receipt_sha256()
        )


def issue_official_handoff(
    *,
    plan_version: str,
    component_id: str,
    scope: ProviderScopeKey,
    locator: OfficialDetailLocator,
    url: str,
    query_fingerprint_sha256: str,
    revalidation_receipt: RevalidationReceipt,
    created_at: datetime | None = None,
    expires_at: datetime | None = None,
    handoff_id: str | None = None,
) -> OfficialHandoff:
    """Issue a short-lived, single-use handoff bound to a revalidation receipt."""
    if revalidation_receipt.component_id != component_id:
        raise ValueError("revalidation receipt must bind the same component")
    if revalidation_receipt.scope.key != scope.key:
        raise ValueError("revalidation receipt must bind the same scope")
    if revalidation_receipt.plan_version != plan_version:
        raise ValueError("revalidation receipt must bind the same plan version")
    if revalidation_receipt.outcome is not RevalidationOutcome.UNCHANGED:
        raise ValueError("a handoff may only be issued after an unchanged re-pricing")
    created = created_at or datetime.now(UTC)
    expires = expires_at or created + timedelta(seconds=HANDOFF_MAX_AGE_SECONDS)
    if expires > revalidation_receipt.expires_at:
        # The handoff cannot outlive the revalidation receipt it depends on.
        expires = revalidation_receipt.expires_at
    policy = HandoffURLPolicy(locator=locator)
    resolved_id = handoff_id or _deterministic_handoff_id(
        plan_version=plan_version,
        component_id=component_id,
        scope=scope,
        url=url,
        created_at=created,
    )
    return OfficialHandoff(
        handoff_id=resolved_id,
        plan_version=plan_version,
        component_id=component_id,
        scope=scope,
        locator=locator,
        url=url,
        query_fingerprint_sha256=query_fingerprint_sha256,
        revalidation_receipt_sha256=revalidation_receipt.receipt_sha256(),
        created_at=created,
        expires_at=expires,
        url_policy=policy,
        url_policy_sha256=policy.url_policy_sha(),
    )


def _deterministic_handoff_id(
    *,
    plan_version: str,
    component_id: str,
    scope: ProviderScopeKey,
    url: str,
    created_at: datetime,
) -> str:
    canonical = {
        "plan_version": plan_version,
        "component_id": component_id,
        "scope": scope.key,
        "url": url,
        "created_at": created_at.isoformat(),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:48]


def build_component_checklist(
    *,
    plan_version: str,
    component_id: str,
    scope: ProviderScopeKey,
    locator: OfficialDetailLocator,
    official_url: str,
    query_fingerprint_sha256: str,
    reprice_url: str,
    revalidation_receipt: RevalidationReceipt | None,
    now: datetime | None = None,
) -> ComponentHandoffChecklist:
    """Build the per-component two-step checklist deterministically.

    Step 1 is always re-pricing the exact component (same provider x component,
    never a full re-run of the trip).  Only when the revalidation receipt says
    ``unchanged`` and is still fresh may the UI offer step 2 (go to the official
    page).  Nothing here ever creates a booked state.
    """
    reference = now or datetime.now(UTC)
    handoff = None
    if (
        revalidation_receipt is not None
        and revalidation_receipt.outcome is RevalidationOutcome.UNCHANGED
        and revalidation_receipt.is_fresh(reference)
    ):
        handoff = issue_official_handoff(
            plan_version=plan_version,
            component_id=component_id,
            scope=scope,
            locator=locator,
            url=official_url,
            query_fingerprint_sha256=query_fingerprint_sha256,
            revalidation_receipt=revalidation_receipt,
            created_at=reference,
        )
    receipt_stale = (
        revalidation_receipt is not None and not revalidation_receipt.is_fresh(reference)
    )
    suggested_next_step = (
        "reprice" if handoff is None or receipt_stale else "go_to_official"
    )
    return ComponentHandoffChecklist(
        component_id=component_id,
        plan_version=plan_version,
        scope=scope,
        reprice_url=reprice_url,
        official_handoff=handoff,
        revalidation_receipt=revalidation_receipt,
        suggested_next_step=suggested_next_step,
    )
