"""Read-only ECB USD/CNY reference-rate support shared by live sources."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from tripchord.providers.base import ProviderError

ECB_DAILY_REFERENCE_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_MAX_RESPONSE_BYTES = 128_000
_RATE_QUANTUM = Decimal("0.000001")


class UsdCnyReferenceRate:
    def __init__(
        self,
        *,
        rate_date: date,
        captured_at: datetime,
        usd_per_eur: Decimal,
        cny_per_eur: Decimal,
        response_sha256: str,
    ) -> None:
        self.rate_date = rate_date
        self.captured_at = captured_at
        self.usd_per_eur = usd_per_eur
        self.cny_per_eur = cny_per_eur
        self.usd_to_cny = (cny_per_eur / usd_per_eur).quantize(
            _RATE_QUANTUM, rounding=ROUND_HALF_UP
        )
        self.response_sha256 = response_sha256


async def fetch_usd_cny_reference_rate(
    *,
    client: httpx.AsyncClient | None = None,
    now: Callable[[], datetime] | None = None,
) -> UsdCnyReferenceRate:
    captured_at = (now or (lambda: datetime.now(UTC)))().astimezone(UTC)
    owned_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=8,
        follow_redirects=False,
        headers={"user-agent": "TripChord/0.1 (+read-only ECB reference-rate estimate)"},
    )
    try:
        try:
            response = await http_client.get(
                ECB_DAILY_REFERENCE_URL,
                headers={"accept": "application/xml,text/xml"},
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "ecb-reference-rate",
                "timeout",
                "ECB daily reference-rate read timed out",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                "ecb-reference-rate",
                "network_error",
                "ECB daily reference-rate read failed",
                retryable=True,
            ) from exc
        if response.history or str(response.url) != ECB_DAILY_REFERENCE_URL:
            raise ProviderError(
                "ecb-reference-rate",
                "redirect_forbidden",
                "ECB reference-rate source redirected away from the audited URL",
            )
        if (
            response.status_code != 200
            or not response.content
            or len(response.content) > _MAX_RESPONSE_BYTES
        ):
            raise ProviderError(
                "ecb-reference-rate",
                "http_status",
                "ECB reference-rate source returned an unusable response",
            )
        try:
            root = ET.fromstring(response.content)
            day_node = next(
                node
                for node in root.iter()
                if node.tag.endswith("Cube") and "time" in node.attrib
            )
            rate_date = date.fromisoformat(day_node.attrib["time"])
            rates = {
                node.attrib["currency"]: Decimal(node.attrib["rate"])
                for node in day_node
                if node.tag.endswith("Cube") and "currency" in node.attrib and "rate" in node.attrib
            }
            usd_per_eur = rates["USD"]
            cny_per_eur = rates["CNY"]
        except (ET.ParseError, StopIteration, KeyError, InvalidOperation, ValueError) as exc:
            raise ProviderError(
                "ecb-reference-rate",
                "schema_drift",
                "ECB reference-rate response lacked valid USD/CNY rates",
            ) from exc
        age_days = (captured_at.date() - rate_date).days
        if age_days < 0 or age_days > 4:
            raise ProviderError(
                "ecb-reference-rate",
                "stale_rate",
                "ECB reference-rate date was outside the four-day weekend-safe window",
            )
        return UsdCnyReferenceRate(
            rate_date=rate_date,
            captured_at=captured_at,
            usd_per_eur=usd_per_eur,
            cny_per_eur=cny_per_eur,
            response_sha256=hashlib.sha256(response.content).hexdigest(),
        )
    finally:
        if owned_client:
            await http_client.aclose()
