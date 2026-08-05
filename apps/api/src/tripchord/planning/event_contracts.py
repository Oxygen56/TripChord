from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from tripchord.domain.common import DomainModel
from tripchord.planning.offer_semantics import (
    OfferIdentityConfidence,
    OfferSemanticChange,
    OfferSemanticDiff,
    semantic_offer_diff,
    stable_offer_identity,
)
from tripchord.planning.package import (
    PackageEventKind,
    PackageQuote,
    QuoteAvailability,
)


class EventDisposition(StrEnum):
    NO_CHANGE = "no_change"
    REFRESH = "refresh"
    LOCAL_REPAIR = "local_repair"
    GLOBAL_REPLAN = "global_replan"
    HUMAN_BLOCK = "human_block"


class OfferValueSnapshot(DomainModel):
    transient_offer_id: str = Field(min_length=1)
    # Optional/defaulted identity metadata keeps schema-v1 envelopes readable;
    # legacy records are interpreted conservatively as ambiguous low-confidence
    # observations rather than upgraded to verified identity.
    stable_product_key: str | None = Field(default=None, min_length=64, max_length=64)
    stable_offer_key: str = Field(min_length=64, max_length=64)
    product_identity_confidence: OfferIdentityConfidence = OfferIdentityConfidence.LOW
    offer_identity_confidence: OfferIdentityConfidence = OfferIdentityConfidence.LOW
    identity_ambiguous: bool = True
    official_product_id: str | None = None
    official_offer_id: str | None = None
    provider: str = Field(min_length=1)
    total_for_party_cents: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    availability: str = Field(min_length=1)
    captured_at: datetime
    evidence_refs: tuple[str, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("offer snapshot captured_at must be timezone-aware")
        return value

    @classmethod
    def from_quote(cls, quote: PackageQuote) -> OfferValueSnapshot:
        identity = stable_offer_identity(quote)
        return cls(
            transient_offer_id=quote.id,
            stable_product_key=identity.product_key,
            stable_offer_key=identity.offer_key,
            product_identity_confidence=identity.product_confidence,
            offer_identity_confidence=identity.offer_confidence,
            identity_ambiguous=identity.product_ambiguous or identity.offer_ambiguous,
            official_product_id=identity.official_product_id,
            official_offer_id=identity.official_offer_id,
            provider=quote.provider,
            total_for_party_cents=quote.total_for_party_cents,
            currency=quote.currency,
            availability=quote.availability.value,
            captured_at=quote.captured_at,
            evidence_refs=quote.evidence_refs,
        )


class OfferEventEnvelope(DomainModel):
    event_id: str = Field(min_length=1)
    trip_id: str = Field(min_length=1)
    kind: PackageEventKind
    target_component_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    occurred_at: datetime
    observed_at: datetime
    schema_version: int = Field(default=1, ge=1)
    dedupe_key: str = Field(min_length=64, max_length=64)
    old_value: OfferValueSnapshot
    new_value: OfferValueSnapshot | None = None

    @field_validator("occurred_at", "observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_order(self) -> OfferEventEnvelope:
        if self.observed_at < self.occurred_at:
            raise ValueError("observed_at cannot precede occurred_at")
        return self


class OfferEventResolution(DomainModel):
    disposition: EventDisposition
    verified_change: bool
    reason: str = Field(min_length=1)
    envelope: OfferEventEnvelope
    semantic_diff: OfferSemanticDiff | None = None
    replacement_component_id: str | None = None
    cascade_component_ids: tuple[str, ...] = ()
    candidate_pool_expansion_required: bool = False


def make_event_envelope(
    *,
    event_id: str,
    trip_id: str,
    kind: PackageEventKind,
    target_component_id: str,
    source: str,
    occurred_at: datetime,
    old: PackageQuote,
    new: PackageQuote | None,
    schema_version: int = 1,
    observed_at: datetime | None = None,
) -> OfferEventEnvelope:
    observed = observed_at or datetime.now(UTC)
    old_value = OfferValueSnapshot.from_quote(old)
    new_value = OfferValueSnapshot.from_quote(new) if new is not None else None
    dedupe_payload = {
        "trip_id": trip_id,
        "kind": kind.value,
        "target_stable_offer_key": old_value.stable_offer_key,
        "new_stable_offer_key": (
            new_value.stable_offer_key if new_value is not None else None
        ),
        "old_total": old_value.total_for_party_cents,
        "new_total": new_value.total_for_party_cents if new_value is not None else None,
        "old_availability": old_value.availability,
        "new_availability": new_value.availability if new_value is not None else None,
        "source": source,
        "schema_version": schema_version,
    }
    dedupe_key = hashlib.sha256(
        json.dumps(dedupe_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return OfferEventEnvelope(
        event_id=event_id,
        trip_id=trip_id,
        kind=kind,
        target_component_id=target_component_id,
        source=source,
        occurred_at=occurred_at,
        observed_at=observed,
        schema_version=schema_version,
        dedupe_key=dedupe_key,
        old_value=old_value,
        new_value=new_value,
    )


def resolve_offer_event(
    *,
    event_id: str,
    trip_id: str,
    kind: PackageEventKind,
    target_component_id: str,
    source: str,
    occurred_at: datetime,
    old: PackageQuote,
    compatible_observations: tuple[PackageQuote, ...],
    schema_version: int = 1,
    observed_at: datetime | None = None,
) -> tuple[PackageQuote | None, OfferEventResolution]:
    same_offer: list[tuple[PackageQuote, OfferSemanticDiff]] = []
    same_product: list[tuple[PackageQuote, OfferSemanticDiff]] = []
    alternatives: list[tuple[PackageQuote, OfferSemanticDiff]] = []
    ambiguous: list[tuple[PackageQuote, OfferSemanticDiff]] = []
    for quote in compatible_observations:
        difference = semantic_offer_diff(old, quote)
        if difference.same_offer:
            same_offer.append((quote, difference))
        if difference.same_product:
            same_product.append((quote, difference))
        elif difference.different_product_confirmed:
            alternatives.append((quote, difference))
        else:
            ambiguous.append((quote, difference))

    if kind == PackageEventKind.PRICE_CHANGED:
        changed = tuple(
            (quote, difference)
            for quote, difference in same_offer
            if difference.change == OfferSemanticChange.PRICE_CHANGED
            and quote.availability == QuoteAvailability.AVAILABLE
        )
        if changed:
            selected, difference = min(
                changed,
                key=lambda item: (
                    item[0].total_for_party_cents,
                    -item[0].captured_at.timestamp(),
                    item[0].id,
                ),
            )
            return selected, _resolution(
                EventDisposition.LOCAL_REPAIR,
                True,
                "同一稳定商品的新旧金额不同，价格变化已由新证据确认",
                event_id,
                trip_id,
                kind,
                target_component_id,
                source,
                occurred_at,
                old,
                selected,
                difference,
                schema_version,
                observed_at,
            )
        available_same_offer = tuple(
            item for item in same_offer if item[0].availability == QuoteAvailability.AVAILABLE
        )
        if available_same_offer:
            selected, difference = max(
                available_same_offer,
                key=lambda item: (item[0].captured_at, item[0].id),
            )
            disposition = (
                EventDisposition.REFRESH
                if difference.change == OfferSemanticChange.OBSERVATION_REFRESHED
                else EventDisposition.NO_CHANGE
            )
            return None, _resolution(
                disposition,
                False,
                "重查命中同一稳定商品且金额未变化，只更新观察证据，不生成新方案版本",
                event_id,
                trip_id,
                kind,
                target_component_id,
                source,
                occurred_at,
                old,
                selected,
                difference,
                schema_version,
                observed_at,
            )
        if ambiguous:
            selected, difference = max(
                ambiguous,
                key=lambda item: (item[0].captured_at, item[0].id),
            )
            return None, _resolution(
                EventDisposition.HUMAN_BLOCK,
                False,
                "重查报价缺少可核验的官方商品/报价 ID；语义指纹存在歧义，拒绝冒充同一商品涨价",
                event_id,
                trip_id,
                kind,
                target_component_id,
                source,
                occurred_at,
                old,
                selected,
                difference,
                schema_version,
                observed_at,
                expansion=True,
            )
        return None, _resolution(
            EventDisposition.HUMAN_BLOCK,
            False,
            "重查未找到同一稳定商品，无法把其他商品的价格当作目标商品涨跌证据",
            event_id,
            trip_id,
            kind,
            target_component_id,
            source,
            occurred_at,
            old,
            None,
            None,
            schema_version,
            observed_at,
            expansion=True,
        )

    available_alternatives = tuple(
        item for item in alternatives if item[0].availability == QuoteAvailability.AVAILABLE
    )
    if available_alternatives:
        selected, difference = min(
            available_alternatives,
            key=lambda item: (
                item[0].total_for_party_cents,
                -item[0].captured_at.timestamp(),
                item[0].id,
            ),
        )
        return selected, _resolution(
            EventDisposition.LOCAL_REPAIR,
            True,
            "已排除售罄商品的稳定身份，并找到兼容的不同商品作为局部替代",
            event_id,
            trip_id,
            kind,
            target_component_id,
            source,
            occurred_at,
            old,
            selected,
            difference,
            schema_version,
            observed_at,
        )
    available_same_product = tuple(
        item for item in same_product if item[0].availability == QuoteAvailability.AVAILABLE
    )
    if available_same_product:
        selected, difference = max(
            available_same_product,
            key=lambda item: (item[0].captured_at, item[0].id),
        )
        return None, _resolution(
            EventDisposition.NO_CHANGE,
            False,
            "新证据仍显示同一稳定商品可用（即使条款发生变化），售罄事件与重查结果冲突，拒绝替换",
            event_id,
            trip_id,
            kind,
            target_component_id,
            source,
            occurred_at,
            old,
            selected,
            difference,
            schema_version,
            observed_at,
        )
    confirmed_sold_out = tuple(
        item for item in same_product if item[0].availability == QuoteAvailability.SOLD_OUT
    )
    if confirmed_sold_out:
        selected, difference = max(
            confirmed_sold_out,
            key=lambda item: (item[0].captured_at, item[0].id),
        )
        return None, _resolution(
            EventDisposition.GLOBAL_REPLAN,
            True,
            "同一稳定商品的最新证据确认已售罄，局部池无可用替代，需要扩大候选池",
            event_id,
            trip_id,
            kind,
            target_component_id,
            source,
            occurred_at,
            old,
            selected,
            difference,
            schema_version,
            observed_at,
            expansion=True,
        )
    if ambiguous:
        selected, difference = max(
            ambiguous,
            key=lambda item: (item[0].captured_at, item[0].id),
        )
        return None, _resolution(
            EventDisposition.HUMAN_BLOCK,
            False,
            "候选缺少足够身份字段，无法证明它既不是售罄商品本身、也确实是不同替代品",
            event_id,
            trip_id,
            kind,
            target_component_id,
            source,
            occurred_at,
            old,
            selected,
            difference,
            schema_version,
            observed_at,
            expansion=True,
        )
    return None, _resolution(
        EventDisposition.GLOBAL_REPLAN,
        True,
        "局部候选池没有不同稳定商品，需要扩大跨平台或跨日期候选池",
        event_id,
        trip_id,
        kind,
        target_component_id,
        source,
        occurred_at,
        old,
        None,
        None,
        schema_version,
        observed_at,
        expansion=True,
    )


def _resolution(
    disposition: EventDisposition,
    verified_change: bool,
    reason: str,
    event_id: str,
    trip_id: str,
    kind: PackageEventKind,
    target_component_id: str,
    source: str,
    occurred_at: datetime,
    old: PackageQuote,
    new: PackageQuote | None,
    difference: OfferSemanticDiff | None,
    schema_version: int,
    observed_at: datetime | None,
    *,
    expansion: bool = False,
) -> OfferEventResolution:
    envelope = make_event_envelope(
        event_id=event_id,
        trip_id=trip_id,
        kind=kind,
        target_component_id=target_component_id,
        source=source,
        occurred_at=occurred_at,
        old=old,
        new=new,
        schema_version=schema_version,
        observed_at=observed_at,
    )
    return OfferEventResolution(
        disposition=disposition,
        verified_change=verified_change,
        reason=reason,
        envelope=envelope,
        semantic_diff=difference,
        replacement_component_id=new.id if new is not None else None,
        candidate_pool_expansion_required=expansion,
    )


__all__ = [
    "EventDisposition",
    "OfferEventEnvelope",
    "OfferEventResolution",
    "OfferValueSnapshot",
    "make_event_envelope",
    "resolve_offer_event",
]
