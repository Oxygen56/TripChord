"""v0.5 / v0.6 / v0.7 production wiring API (third-round continuation).

This router wires the deterministic cores into *production paths*:

- **v0.5** — official-handoff re-pricing: reprice one component (same provider
  x component), build a :class:`RevalidationReceipt` and a two-step
  :class:`ComponentHandoffChecklist`, persist them, and consume a handoff as
  single-use.  A click here can never create a booked state.
- **v0.6** — booking protection: expose the booking checklist / explicit
  acknowledgement / override-request lifecycle and the persisted booking
  ledger that the planning pipeline consults.
- **v0.7** — provider SDK: per-vertical one-click cooldown and conformance
  status through the SDK state machine, instead of editing core enums.

The router follows the existing ``platform/api.py`` conventions: it reads the
tenant from ``request.state``, the live run cache / registry / stores from
``request.app.state``, and fails closed when a required store is missing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from tripchord.persistence.booking_ledger import BookingLedgerStore
from tripchord.persistence.handoff_store import HandoffStore
from tripchord.platform.booking import BookingLedger
from tripchord.platform.booking_gate import BookingService
from tripchord.platform.capability import ProviderScopeKey, ProviderVertical
from tripchord.platform.handoff import (
    LocatorKind,
    OfficialDetailLocator,
    OfficialHandoff,
)
from tripchord.platform.reprice import (
    ComponentRepriceRequest,
    ComponentRepriceService,
)

router = APIRouter(prefix="/api/v1", tags=["product-wiring"])


# ---------------------------------------------------------------------------
# v0.5 official-handoff re-pricing
# ---------------------------------------------------------------------------


class RepriceComponentRequest(BaseModel):
    timeout_seconds: int | None = Field(default=None, ge=30, le=300)


class RepriceComponentResponse(BaseModel):
    run_id: str
    component_id: str
    plan_version: str
    scope_key: str
    outcome: str
    live_mode: str
    revalidation_receipt: dict[str, object] | None = None
    checklist: dict[str, object] | None = None
    blocked_reason: str | None = None


class ConsumeHandoffRequest(BaseModel):
    handoff_id: str = Field(min_length=1)


class ConsumeHandoffResponse(BaseModel):
    handoff_id: str
    consumed: bool
    state: str
    booked: bool = False


def _tenant(request: Request) -> str:
    return str(getattr(request.state, "tenant_id", "anonymous"))


def _quote_from_run(run: object, component_id: str) -> tuple[str, str, int | None]:
    """Locate a component quote inside a live package run.

    Returns ``(provider, scope_key, total_for_party_cents)`` or raises 404.
    """
    package = getattr(run, "package", None)
    candidate = getattr(package, "final_candidate", None)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live run has no final candidate to re-price",
        )
    candidates: list[object] = []
    flight = getattr(candidate, "flight", None)
    if flight is not None:
        candidates.append(flight)
    candidates.extend(getattr(candidate, "lodgings", ()) or ())
    candidates.extend(getattr(candidate, "transfers", ()) or ())
    for quote in candidates:
        if getattr(quote, "id", None) == component_id:
            provider = str(getattr(quote, "provider", ""))
            total = getattr(quote, "total_for_party_cents", None)
            return provider, provider, total
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"component {component_id!r} was not found in the live run",
    )


def _locator_for_scope(request: Request, scope: ProviderScopeKey) -> OfficialDetailLocator:
    """Build an :class:`OfficialDetailLocator` from the registry capability."""
    registry = getattr(request.app.state, "provider_registry", None)
    capability = None
    if registry is not None:
        capability = registry.get(scope)
    if capability is None:
        # Deterministic fallback keeps the API useful without a wired registry.
        capability = None
    hosts = tuple(getattr(capability, "official_domains", ()) or ())
    if not hosts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"no official domains declared for scope {scope.key}",
        )
    supports_detail = bool(getattr(capability, "supports_stable_detail_page", False))
    supports_prefill = bool(getattr(capability, "supports_prefilled_search_page", False))
    kind = (
        LocatorKind.DETAIL_PAGE
        if supports_detail
        else LocatorKind.PREFILLED_SEARCH
        if supports_prefill
        else LocatorKind.PARAM_CARD_ONLY
    )
    return OfficialDetailLocator(
        scope=scope,
        kind=kind,
        official_hosts=hosts,
        allowed_path_prefixes=("/search/",) if supports_detail or supports_prefill else (),
    )


def _scope_from_provider(provider: str) -> ProviderScopeKey:
    name = provider.lower().split("-")[0].split("_")[0]
    if name in {"ctrip", "qunar", "tongcheng", "icom", "fliggy", "zhixing"}:
        # Transfer quotes are marked "icom-public-transfer"; map them to the
        # transfer vertical by provider id.
        if name == "icom":
            return ProviderScopeKey(provider="icom", vertical=ProviderVertical.TRANSFER)
        return ProviderScopeKey(provider=name, vertical=ProviderVertical.FLIGHT)
    return ProviderScopeKey(provider=name, vertical=ProviderVertical.FLIGHT)


def _handoff_store(request: Request) -> HandoffStore:
    store = getattr(request.app.state, "handoff_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="handoff store is not configured",
        )
    return cast(HandoffStore, store)


@router.post(
    "/agents/live-plans/{run_id}/components/{component_id}/reprice",
    response_model=RepriceComponentResponse,
)
async def reprice_component_endpoint(
    run_id: str,
    component_id: str,
    request: Request,
    body: RepriceComponentRequest | None = None,
) -> RepriceComponentResponse:
    """Re-price exactly one component (same provider x component) and build the
    two-step official-handoff checklist.  Never re-runs the whole trip and never
    creates a booked state."""
    del body
    cache = getattr(request.app.state, "live_run_cache", None)
    if cache is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="live run cache is not configured",
        )
    entry = await cache.get(run_id, _tenant(request))
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="live planning run was not found or has expired",
        )
    provider, provider_key, current_total = _quote_from_run(entry.run, component_id)
    scope = _scope_from_provider(provider_key)
    locator = _locator_for_scope(request, scope)

    quote_source_factory = getattr(request.app.state, "reprice_quote_source_factory", None)
    live_mode = "fixture" if quote_source_factory is not None else "live-unavailable"
    quote_source = None
    if quote_source_factory is not None:
        quote_source = quote_source_factory(
            run=entry.run,
            component_id=component_id,
            provider=provider,
            timeout_seconds=30,
        )

    service = ComponentRepriceService(quote_source=quote_source, now=datetime.now(UTC))
    reprice_request = ComponentRepriceRequest(
        plan_version=run_id,
        component_id=component_id,
        scope=scope,
        query_fingerprint_sha256="0" * 64,
        current_total_for_party_cents=current_total,
        reprice_url=f"/api/v1/agents/live-plans/{run_id}/components/{component_id}/reprice",
    )
    result = await service.reprice_component(reprice_request, locator)
    store = _handoff_store(request)
    store.put(
        plan_version=run_id,
        component_id=component_id,
        receipt=result.revalidation_receipt,
        checklist=result.checklist,
    )
    return RepriceComponentResponse(
        run_id=run_id,
        component_id=component_id,
        plan_version=run_id,
        scope_key=scope.key,
        outcome=result.outcome.value,
        live_mode=live_mode,
        revalidation_receipt=(
            result.revalidation_receipt.model_dump(mode="json")
            if result.revalidation_receipt is not None
            else None
        ),
        checklist=(
            result.checklist.model_dump(mode="json") if result.checklist is not None else None
        ),
        blocked_reason=result.blocked_reason,
    )


@router.post(
    "/agents/live-plans/{run_id}/components/{component_id}/handoff/consume",
    response_model=ConsumeHandoffResponse,
)
async def consume_handoff_endpoint(
    run_id: str,
    component_id: str,
    body: ConsumeHandoffRequest,
    request: Request,
) -> ConsumeHandoffResponse:
    """Consume one official handoff as single-use.

    This is the *only* transition to ``used`` — opening the official page.
    It never produces a booked state; a separate explicit user action is
    required to create a Booking Fact (see the v0.6 booking endpoints).
    """
    store = _handoff_store(request)
    record = store.get(run_id, component_id)
    if record is None or record.checklist is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no active handoff for this component; re-price first",
        )
    handoff: OfficialHandoff | None = getattr(record.checklist, "official_handoff", None)
    if handoff is None or handoff.handoff_id != body.handoff_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="handoff id does not match the component's active handoff",
        )
    consumed = store.consume_handoff(handoff)
    return ConsumeHandoffResponse(
        handoff_id=body.handoff_id,
        consumed=consumed,
        state="used" if consumed else handoff.state.value,
        booked=False,
    )


# ---------------------------------------------------------------------------
# v0.6 booking protection
# ---------------------------------------------------------------------------


class BookingAcknowledgeRequest(BaseModel):
    checklist_id: str = Field(min_length=1)
    acknowledgement_id: str = Field(min_length=1)
    user_token_sha256: str = Field(min_length=64, max_length=64)


class BookingOverrideRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)
    requested_by_token_sha256: str = Field(min_length=64, max_length=64)


class BookingLedgerResponse(BaseModel):
    plan_version: str
    protected_component_ids: tuple[str, ...]
    facts: tuple[dict[str, object], ...] = ()
    overrides: tuple[dict[str, object], ...] = ()
    checklists: tuple[dict[str, object], ...] = ()


class BookingAcknowledgeResponse(BaseModel):
    plan_version: str
    component_id: str
    protected: bool
    fact: dict[str, object]


class BookingOverrideResponse(BaseModel):
    plan_version: str
    component_id: str
    request_id: str
    state: str


def _booking_store(request: Request) -> BookingLedgerStore:
    store = getattr(request.app.state, "booking_ledger_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="booking ledger store is not configured",
        )
    return cast(BookingLedgerStore, store)


def _load_ledger(request: Request, plan_version: str) -> BookingLedger:
    store = _booking_store(request)
    ledger = store.load(plan_version)
    if ledger is None:
        return BookingLedger(plan_version=plan_version)
    return ledger


def _save_ledger(request: Request, ledger: BookingLedger) -> None:
    _booking_store(request).save(ledger)


@router.get("/plans/{plan_version}/booking", response_model=BookingLedgerResponse)
async def get_booking_ledger_endpoint(plan_version: str, request: Request) -> BookingLedgerResponse:
    ledger = _load_ledger(request, plan_version)
    return BookingLedgerResponse(
        plan_version=plan_version,
        protected_component_ids=tuple(fact.component_id for fact in ledger.facts),
        facts=tuple(fact.model_dump(mode="json") for fact in ledger.facts),
        overrides=tuple(override.model_dump(mode="json") for override in ledger.overrides),
        checklists=tuple(checklist.model_dump(mode="json") for checklist in ledger.checklists),
    )


@router.post(
    "/plans/{plan_version}/components/{component_id}/booking/acknowledge",
    response_model=BookingAcknowledgeResponse,
)
async def acknowledge_booking_endpoint(
    plan_version: str,
    component_id: str,
    body: BookingAcknowledgeRequest,
    request: Request,
) -> BookingAcknowledgeResponse:
    """Create an append-only Booking Fact from an *explicit* user acknowledgement.

    This is the only way a component becomes protected.  Opening an official
    page, an Agent output or platform text can never create a fact.
    """
    ledger = _load_ledger(request, plan_version)
    service = BookingService(ledger, now=datetime.now(UTC))
    try:
        updated, fact = service.acknowledge_component(
            plan_version=plan_version,
            component_id=component_id,
            checklist_id=body.checklist_id,
            acknowledgement_id=body.acknowledgement_id,
            user_token_sha256=body.user_token_sha256,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    _save_ledger(request, updated)
    return BookingAcknowledgeResponse(
        plan_version=plan_version,
        component_id=component_id,
        protected=True,
        fact=fact.model_dump(mode="json"),
    )


@router.post(
    "/plans/{plan_version}/components/{component_id}/booking/override",
    response_model=BookingOverrideResponse,
)
async def request_booking_override_endpoint(
    plan_version: str,
    component_id: str,
    body: BookingOverrideRequest,
    request: Request,
) -> BookingOverrideResponse:
    """Record an explicit, audited request to un-protect a component.

    The request never auto-applies: it stays ``requested`` until a later
    explicit resolution.  Until then the protection gate keeps blocking changes
    to the component.
    """
    ledger = _load_ledger(request, plan_version)
    service = BookingService(ledger, now=datetime.now(UTC))
    try:
        updated, override = service.request_override(
            plan_version=plan_version,
            component_id=component_id,
            requested_by_token_sha256=body.requested_by_token_sha256,
            reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    _save_ledger(request, updated)
    return BookingOverrideResponse(
        plan_version=plan_version,
        component_id=component_id,
        request_id=override.request_id,
        state=override.state.value,
    )


@router.post(
    "/plans/{plan_version}/booking/overrides/{request_id}/resolve",
    response_model=BookingOverrideResponse,
)
async def resolve_booking_override_endpoint(
    plan_version: str,
    request_id: str,
    body: dict[str, bool],
    request: Request,
) -> BookingOverrideResponse:
    """Explicitly apply or reject an override request (audited, never silent)."""
    ledger = _load_ledger(request, plan_version)
    service = BookingService(ledger, now=datetime.now(UTC))
    try:
        updated, resolved = service.resolve_override(
            request_id, apply=bool(body.get("apply", False))
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    _save_ledger(request, updated)
    return BookingOverrideResponse(
        plan_version=plan_version,
        component_id=resolved.component_id,
        request_id=resolved.request_id,
        state=resolved.state.value,
    )


# ---------------------------------------------------------------------------
# v0.7 provider SDK
# ---------------------------------------------------------------------------


class ProviderCooldownRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=400)


class ProviderCooldownResponse(BaseModel):
    scope: str
    from_stage: str
    to_stage: str
    reason: str


@router.post("/providers/{scope}/cooldown", response_model=ProviderCooldownResponse)
async def provider_cooldown_endpoint(
    scope: str,
    body: ProviderCooldownRequest,
    request: Request,
) -> ProviderCooldownResponse:
    """One-click per-vertical cooldown through the v0.7 SDK state machine.

    This pauses exactly one ``provider x vertical`` scope without touching the
    immutable registry; the runtime overlay records the transition.
    """
    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider registry is not configured",
        )
    provider, vertical_raw = scope.split(":", 1)
    vertical = ProviderVertical(vertical_raw)
    key = ProviderScopeKey(provider=provider, vertical=vertical)
    capability = registry.get(key)
    if capability is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown provider scope: {scope}",
        )
    from tripchord.platform.sdk import one_click_cooldown

    updated, transition = one_click_cooldown(
        capability,
        performed_at=datetime.now(UTC),
        reason=body.reason,
    )
    overlay = getattr(request.app.state, "provider_cooldown_overlay", None)
    if overlay is not None:
        overlay[scope] = updated.model_dump(mode="json")
    return ProviderCooldownResponse(
        scope=scope,
        from_stage=transition.from_stage.value,
        to_stage=transition.to_stage.value,
        reason=transition.reason,
    )


class ProviderConformanceView(BaseModel):
    scope: str
    certification_stage: str
    conformance: str


class ObservabilitySummaryView(BaseModel):
    planning_jobs_by_terminal_state: dict[str, int]
    handoff_count: int
    booking_fact_count: int
    protected_component_count: int
    boundary: str


@router.get("/observability/summary", response_model=ObservabilitySummaryView)
async def observability_summary_endpoint(request: Request) -> ObservabilitySummaryView:
    """Local observability summary (v0.9).

    Separately counts terminal job states, issued official handoffs and booking
    facts so a local panel can track the product surfaces without touching real
    OTA data.  This is a process-local summary; it does not claim platform-wide
    availability.
    """
    from tripchord.observability import metrics as runtime_metrics

    job_counts = dict(runtime_metrics._job_counts)
    handoff_store = getattr(request.app.state, "handoff_store", None)
    booking_store = getattr(request.app.state, "booking_ledger_store", None)
    handoff_count = 0
    booking_fact_count = 0
    protected_component_count = 0
    if handoff_store is not None:
        handoff_count = len(handoff_store._used_ids)
    if booking_store is not None:
        from pathlib import Path

        root = booking_store._root
        facts = 0
        protected = 0
        for ledger_file in Path(root).glob("*.json"):
            ledger = booking_store.load(ledger_file.stem)
            if ledger is None:
                continue
            facts += len(ledger.facts)
            protected += len(ledger.constraints)
        booking_fact_count = facts
        protected_component_count = protected
    return ObservabilitySummaryView(
        planning_jobs_by_terminal_state=job_counts,
        handoff_count=handoff_count,
        booking_fact_count=booking_fact_count,
        protected_component_count=protected_component_count,
        boundary=(
            "本地可观测汇总；真实平台 canary/覆盖需授权 Companion 会话，不在此汇总中伪造"
        ),
    )


@router.get("/providers/sdk/conformance", response_model=tuple[ProviderConformanceView, ...])
async def provider_sdk_conformance_endpoint(
    request: Request,
) -> tuple[ProviderConformanceView, ...]:
    """Report the v0.7 SDK conformance verdict per provider scope."""
    from tripchord.platform.sdk import ProviderAdapter, ProviderConformanceRunner

    registry = getattr(request.app.state, "provider_registry", None)
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="provider registry is not configured",
        )
    runner = ProviderConformanceRunner()
    adapter_registry = getattr(request.app.state, "provider_adapter_registry", None) or {}
    views: list[ProviderConformanceView] = []
    for capability in registry.capabilities:
        adapter = adapter_registry.get(capability.key.key)
        adapter_obj: ProviderAdapter | None = adapter
        status_value = runner.run(capability, adapter_obj)
        views.append(
            ProviderConformanceView(
                scope=capability.key.key,
                certification_stage=capability.certification_stage.value,
                conformance=status_value.value,
            )
        )
    return tuple(views)
