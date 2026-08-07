"""Component-level re-pricing and official-handoff issuance (v0.5 wiring).

v0.5 landed the deterministic core (:mod:`tripchord.platform.handoff`):
``OfficialDetailLocator``, ``HandoffURLPolicy``, ``RevalidationReceipt`` and
``OfficialHandoff``.  This module wires that core into a *live re-price API*
path: a same-provider, same-component re-pricing that never re-runs the whole
trip, then builds the two-step ``ComponentHandoffChecklist`` the UI must render
("re-price and review the difference" first, "go to the official page" only
when the revalidation receipt is fresh and unchanged).

The fresh quote is obtained through :class:`RepriceQuoteSource`, which the
caller backs with the existing deterministic replay/fixture infrastructure in
local mode and with the authorised browser/live session in a real run.  A click
here can never create a booked state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlencode, urlunparse

from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.platform.capability import ProviderScopeKey
from tripchord.platform.handoff import (
    HANDOFF_MAX_AGE_SECONDS,
    ComponentHandoffChecklist,
    HandoffURLPolicy,
    LocatorKind,
    OfficialDetailLocator,
    RevalidationOutcome,
    RevalidationReceipt,
    build_component_checklist,
)
from tripchord.providers.browser_bridge import BrowserSearchQuery

REPRICE_SCHEMA_VERSION = "tripchord-component-reprice-v1"
QUERY_FINGERPRINT_SCHEMA_VERSION = "tripchord-component-query-fingerprint-v1"


def compute_query_fingerprint_sha256(query: BrowserSearchQuery) -> str:
    """Deterministic SHA-256 over the exact query parameters of a component.

    The official handoff is bound to this fingerprint (see
    :class:`~tripchord.platform.handoff.OfficialHandoff`), so a later, different
    query — changed dates, travellers, rooms, route or currency — can never
    reuse the same official hop.  The fingerprint is computed over the query the
    whole plan was priced under, which is the conservative binding the handoff
    contract requires.
    """
    canonical = {
        "schema": QUERY_FINGERPRINT_SCHEMA_VERSION,
        "origin": query.origin,
        "destination": query.destination,
        "start_date": query.start_date.isoformat(),
        "end_date": query.end_date.isoformat() if query.end_date is not None else None,
        "adults": query.adults,
        "rooms": query.rooms,
        "currency": query.currency,
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ComponentRepriceOutcome(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NOT_FOUND = "not_found"
    SKIPPED_NO_CURRENT_QUOTE = "skipped_no_current_quote"
    LIVE_UNAVAILABLE = "live_unavailable"


class ComponentRepriceRequest(DomainModel):
    """What the UI/user asks for: re-price exactly one component."""

    plan_version: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    # Deterministic fingerprint of the exact query (dates, travellers, rooms)
    # the plan component was priced under.  The handoff is bound to it so a
    # different query can never reuse the same official hop.
    query_fingerprint_sha256: str = Field(min_length=64, max_length=64)
    current_total_for_party_cents: int | None = Field(default=None, ge=0)
    # Same-provider, same-component re-price URL (never a full-trip re-run).
    reprice_url: str | None = Field(default=None, min_length=1)


class FreshComponentQuote(DomainModel):
    """The fresh quote for the exact same component from the same scope."""

    quote_id: str = Field(min_length=1)
    component_id: str = Field(min_length=1)
    scope: ProviderScopeKey
    total_for_party_cents: int | None = Field(default=None, ge=0)
    fetched_at: datetime


class RepriceQuoteSource(Protocol):
    """A provider-backed source of a fresh quote for one component.

    Local mode backs this with deterministic replay/fixtures; a real run backs
    it with the authorised browser/live session.  Returning ``None`` means the
    source could not locate the component at all.
    """

    async def fetch_fresh_quote(
        self,
        request: ComponentRepriceRequest,
    ) -> FreshComponentQuote | None: ...


class RepriceURLBuilder(Protocol):
    """Builds the official destination URL for a component after re-pricing."""

    def build(
        self,
        *,
        scope: ProviderScopeKey,
        locator: OfficialDetailLocator,
        query_fingerprint_sha256: str,
        plan_version: str,
        component_id: str,
    ) -> str: ...


class ComponentRepriceResult(DomainModel):
    """One component re-price round: receipt + two-step checklist."""

    outcome: ComponentRepriceOutcome
    revalidation_receipt: RevalidationReceipt | None = None
    checklist: ComponentHandoffChecklist | None = None
    blocked_reason: str | None = None


def build_reprice_query_url(
    *,
    plan_version: str,
    component_id: str,
    scope: ProviderScopeKey,
    query_fingerprint_sha256: str,
    base_url: str,
) -> str:
    """Same-provider, same-component re-price URL with an auditable fingerprint.

    The URL stays on the local API (it is a TripChord re-price action, not a
    platform URL), and carries the query fingerprint so the receipt can be
    re-validated against the exact query it re-priced.
    """
    params = [
        ("plan_version", plan_version),
        ("component_id", component_id),
        ("scope", scope.key),
        ("query_fingerprint_sha256", query_fingerprint_sha256),
    ]
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode(params)}"


def build_official_url(
    *,
    locator: OfficialDetailLocator,
    path: str,
    query_params: dict[str, str] | None = None,
) -> str:
    """Deterministically build an official destination URL for a component.

    Uses the locator's first official host and the audited path.  The resulting
    URL is validated against :class:`HandoffURLPolicy` before it may be used in
    an ``OfficialHandoff``; callers that need a stable deep link must pass an
    audited ``path`` inside the locator's allowed prefixes.
    """
    host = locator.official_hosts[0]
    normalized_path = path if path.startswith("/") else f"/{path}"
    query_string = urlencode(sorted((query_params or {}).items()))
    return urlunparse(("https", host, normalized_path, "", query_string, ""))


def _classify(
    *,
    current_total_for_party_cents: int | None,
    fresh_total_for_party_cents: int | None,
) -> ComponentRepriceOutcome:
    if current_total_for_party_cents is None:
        return ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE
    if fresh_total_for_party_cents is None:
        return ComponentRepriceOutcome.NOT_FOUND
    if fresh_total_for_party_cents == current_total_for_party_cents:
        return ComponentRepriceOutcome.UNCHANGED
    return ComponentRepriceOutcome.CHANGED


class ComponentRepriceService:
    """Deterministic wiring of ``OfficialHandoff`` into the live re-price path."""

    def __init__(
        self,
        *,
        quote_source: RepriceQuoteSource | None = None,
        url_builder: RepriceURLBuilder | None = None,
        now: datetime | None = None,
        receipt_ttl_seconds: int = HANDOFF_MAX_AGE_SECONDS,
    ) -> None:
        self._quote_source = quote_source
        self._url_builder = url_builder or DefaultRepriceURLBuilder()
        self._now = now or datetime.now(UTC)
        self._receipt_ttl_seconds = receipt_ttl_seconds

    async def reprice_component(
        self,
        request: ComponentRepriceRequest,
        locator: OfficialDetailLocator,
    ) -> ComponentRepriceResult:
        """Run one component re-price and build the two-step checklist.

        Ordering is strict: a fresh quote must be obtained from the re-price
        source; only an ``unchanged``, fresh outcome may issue an official
        handoff.  Nothing here creates a booked state.
        """
        if locator.scope.key != request.scope.key:
            return ComponentRepriceResult(
                outcome=ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE,
                blocked_reason=(
                    f"locator scope {locator.scope.key} does not match "
                    f"request scope {request.scope.key}"
                ),
            )
        if self._quote_source is None:
            return ComponentRepriceResult(
                outcome=ComponentRepriceOutcome.LIVE_UNAVAILABLE,
                blocked_reason=(
                    "live component re-price is unavailable without an authorised "
                    "Companion session; no fresh quote source is configured"
                ),
            )
        fresh_quote = await self._quote_source.fetch_fresh_quote(request)
        if (
            fresh_quote is not None
            and fresh_quote.scope.key != request.scope.key
        ):
            return ComponentRepriceResult(
                outcome=ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE,
                blocked_reason=(
                    f"fresh quote scope {fresh_quote.scope.key} does not match "
                    f"request scope {request.scope.key}"
                ),
            )

        fresh_total = fresh_quote.total_for_party_cents if fresh_quote is not None else None
        outcome = _classify(
            current_total_for_party_cents=request.current_total_for_party_cents,
            fresh_total_for_party_cents=fresh_total,
        )

        receipt: RevalidationReceipt | None = None
        revalidation_outcome = RevalidationOutcome.NOT_FOUND
        if outcome is ComponentRepriceOutcome.UNCHANGED:
            revalidation_outcome = RevalidationOutcome.UNCHANGED
        elif outcome is ComponentRepriceOutcome.CHANGED:
            revalidation_outcome = RevalidationOutcome.CHANGED
        elif outcome is ComponentRepriceOutcome.NOT_FOUND:
            revalidation_outcome = RevalidationOutcome.NOT_FOUND

        if (
            outcome is not ComponentRepriceOutcome.SKIPPED_NO_CURRENT_QUOTE
            and fresh_quote is not None
        ):
            receipt = RevalidationReceipt(
                receipt_id=f"receipt-{request.plan_version}-{request.component_id}",
                plan_version=request.plan_version,
                component_id=request.component_id,
                scope=request.scope,
                quote_id=fresh_quote.quote_id,
                revalidated_at=self._now,
                expires_at=self._now + timedelta(seconds=self._receipt_ttl_seconds),
                outcome=revalidation_outcome,
                total_for_party_cents=(
                    fresh_total if revalidation_outcome is RevalidationOutcome.UNCHANGED else None
                ),
            )

        official_url = None
        unstable_path_reason: str | None = None
        if outcome is ComponentRepriceOutcome.UNCHANGED:
            try:
                official_url = self._url_builder.build(
                    scope=request.scope,
                    locator=locator,
                    query_fingerprint_sha256=request.query_fingerprint_sha256,
                    plan_version=request.plan_version,
                    component_id=request.component_id,
                )
            except UnstableHandoffPath as exc:
                # No stable deep link: degrade safely to the parameter-card
                # form (reprice step remains; the official hop is not offered).
                unstable_path_reason = str(exc)

        reprice_url = request.reprice_url or build_reprice_query_url(
            plan_version=request.plan_version,
            component_id=request.component_id,
            scope=request.scope,
            query_fingerprint_sha256=request.query_fingerprint_sha256,
            base_url="/api/v1/reprice",
        )

        checklist = None
        if (
            official_url is not None
            and receipt is not None
            and receipt.outcome is RevalidationOutcome.UNCHANGED
        ):
            checklist = build_component_checklist(
                plan_version=request.plan_version,
                component_id=request.component_id,
                scope=request.scope,
                locator=locator,
                official_url=official_url,
                query_fingerprint_sha256=request.query_fingerprint_sha256,
                reprice_url=reprice_url,
                revalidation_receipt=receipt,
                now=self._now,
            )

        blocked_reason = None
        if unstable_path_reason is not None and outcome is ComponentRepriceOutcome.UNCHANGED:
            blocked_reason = (
                f"reprice unchanged but official hop degraded to parameter card: "
                f"{unstable_path_reason}"
            )

        return ComponentRepriceResult(
            outcome=outcome,
            revalidation_receipt=receipt,
            checklist=checklist,
            blocked_reason=blocked_reason,
        )


class DefaultRepriceURLBuilder:
    """Builds an official URL from the locator's first audited path prefix.

    If the locator only supports a parameter card (no stable deep link), the
    builder raises :class:`UnstableHandoffPath` so the caller falls back to the
    parameter-card form instead of guessing a URL.
    """

    def __init__(self) -> None:
        self._policy_cache: dict[str, HandoffURLPolicy] = {}

    def build(
        self,
        *,
        scope: ProviderScopeKey,
        locator: OfficialDetailLocator,
        query_fingerprint_sha256: str,
        plan_version: str,
        component_id: str,
    ) -> str:
        if locator.kind is LocatorKind.PARAM_CARD_ONLY or not locator.allowed_path_prefixes:
            raise UnstableHandoffPath(
                f"scope {scope.key} has no stable official deep link; "
                "use the parameter-card fallback instead"
            )
        path = locator.allowed_path_prefixes[0]
        url = build_official_url(
            locator=locator,
            path=path,
            query_params={
                "tripchord_plan": plan_version,
                "tripchord_component": component_id,
                "q": query_fingerprint_sha256,
            },
        )
        policy = self._policy_cache.get(scope.key)
        if policy is None:
            policy = HandoffURLPolicy(locator=locator)
            self._policy_cache[scope.key] = policy
        allowed, reason = policy.validate_url(url)
        if not allowed:
            raise UnstableHandoffPath(f"constructed official URL failed policy: {reason}")
        return url


class UnstableHandoffPath(ValueError):
    """The scope cannot produce a stable official deep link."""


class ParamCardOnlyLocator:
    """A locator for scopes that only support a parameter card (no deep link)."""
