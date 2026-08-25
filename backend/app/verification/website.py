"""Website liveness verification.

Phase 1 recorded 1 995 owned domains and marked every one ``liveness: unverified``. This module
finds out which of them actually answer, because the Phase 3 crawler's yield — and therefore
everything the RAG layer can cite — is bounded by that number.

Three things it does beyond "send a request":

* **Refuses unsafe targets.** Every URL here came from a scraped spreadsheet. See
  :mod:`app.verification.net` for why that makes SSRF the default assumption rather than an
  edge case.
* **Falls back from HEAD to GET.** A surprising share of small-business hosting answers HEAD
  with 403 or 405 while serving GET perfectly. Treating that as dead would have written off
  live sites.
* **Detects parked domains.** A registrar's "this domain is for sale" page returns 200 and is
  not a business website. Counting it as live would inflate the crawl corpus with pages that
  contain no evidence about anyone.
"""

from __future__ import annotations

import re
import time
from typing import Final
from urllib.parse import urlsplit

import httpx

from app.core.logging import get_logger
from app.verification.net import (
    ALLOWED_SCHEMES,
    MAX_REDIRECTS,
    MAX_RESPONSE_BYTES,
    HostLimiter,
    UnsafeUrlError,
    resolve_public_address,
    retry_async,
)
from app.verification.types import UrlVerification, VerificationStatus

logger = get_logger(__name__)

DEFAULT_TIMEOUT: Final = 12.0
DEFAULT_USER_AGENT: Final = (
    "LeadMindBot/0.1 (+https://github.com/leadmind; lead data verification; contact via repo)"
)

_TITLE_RE: Final = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_WS_RE: Final = re.compile(r"\s+")

# Phrases that appear on registrar parking pages and nowhere on a real business site.
_PARKED_MARKERS: Final[tuple[str, ...]] = (
    "this domain is for sale",
    "buy this domain",
    "domain is parked",
    "parked domain",
    "domain for sale",
    "future home of something quite cool",
    "godaddy.com/domainsearch",
    "sedoparking",
    "hugedomains",
    "afternic",
    "bodis.com",
    "under construction",
    "coming soon",
    "default web site page",
    "welcome to nginx",
    "apache2 ubuntu default page",
    "index of /",
)

_RETRYABLE: Final[tuple[type[BaseException], ...]] = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _clean_title(html: str) -> str | None:
    match = _TITLE_RE.search(html)
    if not match:
        return None
    title = _WS_RE.sub(" ", match.group(1)).strip()
    return title[:200] or None


def looks_parked(title: str | None, body: str) -> bool:
    haystack = f"{title or ''}\n{body[:4000]}".lower()
    return any(marker in haystack for marker in _PARKED_MARKERS)


class WebsiteVerifier:
    """Checks whether URLs are live, politely and safely."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        limiter: HostLimiter | None = None,
        allow_private: bool = False,
        attempts: int = 2,
    ) -> None:
        self._timeout = timeout
        self._user_agent = user_agent
        self._limiter = limiter or HostLimiter()
        self._allow_private = allow_private
        self._attempts = attempts
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> WebsiteVerifier:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=httpx.Timeout(self._timeout),
            headers={
                "User-Agent": self._user_agent,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def verify(self, url: str) -> UrlVerification:
        """Check one URL. Never raises; every failure becomes a status."""
        if self._client is None:  # pragma: no cover - misuse guard
            raise RuntimeError("WebsiteVerifier must be used as an async context manager")

        started = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - started) * 1000)

        split = urlsplit(url)
        if split.scheme not in ALLOWED_SCHEMES or not split.hostname:
            return UrlVerification(
                url=url,
                status=VerificationStatus.SKIPPED,
                latency_ms=elapsed(),
                error=f"unsupported URL scheme {split.scheme!r}",
            )

        port = split.port or (443 if split.scheme == "https" else 80)
        try:
            await resolve_public_address(split.hostname, port, allow_private=self._allow_private)
        except UnsafeUrlError as exc:
            # A refusal, not a measurement: we did not learn the site is down, we declined to
            # look. Recorded distinctly so it cannot be mistaken for evidence either way.
            status = (
                VerificationStatus.UNREACHABLE
                if "does not resolve" in exc.reason
                else VerificationStatus.SKIPPED
            )
            return UrlVerification(url=url, status=status, latency_ms=elapsed(), error=exc.reason)

        try:
            response = await retry_async(
                lambda: self._fetch(url),
                retry_on=_RETRYABLE,
                attempts=self._attempts,
                label=f"GET {split.hostname}",
            )
        except httpx.TooManyRedirects as exc:
            return UrlVerification(
                url=url,
                status=VerificationStatus.UNREACHABLE,
                latency_ms=elapsed(),
                error=f"redirect loop: {exc}",
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return UrlVerification(
                url=url,
                status=VerificationStatus.UNREACHABLE,
                latency_ms=elapsed(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except httpx.HTTPError as exc:
            # Read timeouts and protocol errors are ambiguous: the host answered the TCP
            # handshake, so the site may well be alive but slow.
            return UrlVerification(
                url=url,
                status=VerificationStatus.UNKNOWN,
                latency_ms=elapsed(),
                error=f"{type(exc).__name__}: {exc}",
            )

        body = response.text if response.request.method == "GET" else ""
        title = _clean_title(body) if body else None
        chain = tuple(str(item.url) for item in response.history)

        return UrlVerification(
            url=url,
            status=VerificationStatus.VERIFIED
            if response.status_code < 500
            else VerificationStatus.UNKNOWN,
            status_code=response.status_code,
            final_url=str(response.url),
            redirect_count=len(response.history),
            redirect_chain=chain,
            content_type=response.headers.get("content-type"),
            server=response.headers.get("server"),
            title=title,
            content_length=len(response.content) if response.content else 0,
            is_parked=looks_parked(title, body),
            latency_ms=elapsed(),
            error=None if response.status_code < 400 else f"HTTP {response.status_code}",
        )

    async def _fetch(self, url: str) -> httpx.Response:
        assert self._client is not None
        host = urlsplit(url).hostname or url
        async with self._limiter.slot(host):
            head = await self._client.head(url)
            # Small-business hosting frequently rejects HEAD while serving GET fine; treating
            # that as a dead site would write off live businesses.
            if head.status_code not in (403, 405, 400, 501):
                if head.status_code >= 400:
                    return head
                return await self._get(url)
            return await self._get(url)

    async def _get(self, url: str) -> httpx.Response:
        assert self._client is not None
        async with self._client.stream("GET", url) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            # Rebuild a non-streaming response so callers see a normal object with a body.
            rebuilt = httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=content,
                request=response.request,
                history=response.history,
            )
            return rebuilt
