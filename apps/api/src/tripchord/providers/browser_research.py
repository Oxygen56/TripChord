from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from pydantic import Field

from tripchord.domain.common import DomainModel
from tripchord.domain.source import SourceMode, SourceRecord
from tripchord.providers.base import ProviderError


class BrowserResearchPolicy(DomainModel):
    allowed_domains: tuple[str, ...]
    max_response_bytes: int = Field(default=1_000_000, ge=1_000, le=5_000_000)
    cache_ttl_seconds: int = Field(default=900, gt=0, le=3600)
    user_agent: str = "TripChordResearch/0.1 (+read-only public evidence)"


class BrowserResearchResult(DomainModel):
    requested_url: str
    final_url: str
    status_code: int
    title: str | None = None
    text_excerpt: str
    content_sha256: str
    source: SourceRecord


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.parts.append(value)
        if self._in_title:
            self.title_parts.append(value)


class ControlledBrowserResearchProvider:
    """Allowlisted, read-only public-page fetcher. It never logs in or bypasses controls."""

    name = "controlled-browser"

    def __init__(
        self,
        policy: BrowserResearchPolicy,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._policy = policy
        self._client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"user-agent": policy.user_agent},
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def read_public_page(self, url: str) -> BrowserResearchResult:
        self._validate_url(url)
        response = await self._client.get(url)
        final_url = str(response.url)
        self._validate_url(final_url)
        if not response.is_success:
            raise ProviderError(
                self.name,
                f"http_{response.status_code}",
                f"public page returned HTTP {response.status_code}",
                retryable=response.status_code in {429, 500, 502, 503, 504},
            )
        raw = response.content
        if len(raw) > self._policy.max_response_bytes:
            raise ProviderError(self.name, "response_too_large", "page exceeds research limit")
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            raise ProviderError(self.name, "unsupported_content", "only HTML pages are allowed")
        extractor = _TextExtractor()
        extractor.feed(response.text)
        excerpt = " ".join(extractor.parts)[:12_000]
        now = datetime.now(UTC)
        return BrowserResearchResult(
            requested_url=url,
            final_url=final_url,
            status_code=response.status_code,
            title=" ".join(extractor.title_parts) or None,
            text_excerpt=excerpt,
            content_sha256=hashlib.sha256(raw).hexdigest(),
            source=SourceRecord(
                provider=self.name,
                mode=SourceMode.PRODUCTION,
                request_id=response.headers.get("x-request-id"),
                captured_at=now,
                expires_at=now + timedelta(seconds=self._policy.cache_ttl_seconds),
            ),
        )

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise ProviderError(
                self.name,
                "url_forbidden",
                "only allowlisted HTTPS URLs are allowed",
            )
        allowed = any(
            host == domain or host.endswith(f".{domain}") for domain in self._policy.allowed_domains
        )
        if not allowed:
            raise ProviderError(self.name, "domain_forbidden", f"domain is not allowlisted: {host}")
