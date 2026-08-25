"""Network safety and pacing primitives shared by every verifier.

Two concerns live here, and both are security requirements rather than conveniences.

**SSRF.** Every URL checked by this system came out of a scraped spreadsheet — untrusted input
by definition. Handing such a URL to an HTTP client running inside your own network is the
textbook server-side request forgery setup: ``http://169.254.169.254/`` reads cloud instance
credentials, ``http://127.0.0.1:5432/`` probes your own database, ``http://10.0.0.1/`` reaches
whatever is on the LAN. :func:`resolve_public_address` refuses anything that resolves to a
non-public address, and it checks *after* DNS resolution, because a hostname is free to point at
``127.0.0.1``.

**Politeness.** These are small businesses' websites. A burst of parallel requests at one host
is indistinguishable from an attack. :class:`HostLimiter` caps concurrency per host and enforces
a minimum gap between requests to the same host, independently of the global concurrency budget.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")

# Ports a scraped URL has no legitimate reason to name. Anything outside this set is refused
# rather than dialled, so a crafted URL cannot turn the verifier into a port scanner.
ALLOWED_PORTS: Final[frozenset[int]] = frozenset({80, 443, 8080, 8443})
ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

MAX_REDIRECTS: Final = 5
MAX_RESPONSE_BYTES: Final = 2 * 1024 * 1024


class UnsafeUrlError(Exception):
    """The URL resolves somewhere a verifier must not go."""

    def __init__(self, reason: str, *, host: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.host = host


def is_public_address(raw: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def resolve_public_address(host: str, port: int, *, allow_private: bool = False) -> list[str]:
    """Resolve ``host`` and refuse any answer that is not a public address.

    ``allow_private`` exists solely so the test suite can point the client at a local HTTP
    server. It is an explicit argument rather than an environment flag precisely so that
    enabling it is visible at the call site.
    """
    if port not in ALLOWED_PORTS:
        raise UnsafeUrlError(f"port {port} is not permitted", host=host)

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"host does not resolve: {exc.strerror or exc}", host=host) from exc

    addresses = [str(info[4][0]) for info in infos]
    if not addresses:
        raise UnsafeUrlError("host resolved to no usable address", host=host)

    if not allow_private:
        for address in addresses:
            if not is_public_address(address):
                raise UnsafeUrlError(f"host resolves to non-public address {address}", host=host)
    return addresses


class HostLimiter:
    """Per-host concurrency and minimum spacing.

    A global semaphore alone is not enough: 64 concurrent requests all aimed at one small
    shared-hosting box is exactly the traffic pattern that gets a crawler blocked, and it is
    rude regardless of whether anyone blocks it.
    """

    __slots__ = ("_last_request", "_locks", "_semaphores", "min_interval_seconds", "per_host")

    def __init__(self, *, per_host: int = 2, min_interval_seconds: float = 0.4) -> None:
        self.per_host = per_host
        self.min_interval_seconds = min_interval_seconds
        self._semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(per_host)
        )
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, host: str) -> None:
        await self._semaphores[host].acquire()
        async with self._locks[host]:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_request[host])
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()

    def release(self, host: str) -> None:
        self._semaphores[host].release()

    def slot(self, host: str) -> _HostSlot:
        return _HostSlot(self, host)


class _HostSlot:
    __slots__ = ("_host", "_limiter")

    def __init__(self, limiter: HostLimiter, host: str) -> None:
        self._limiter = limiter
        self._host = host

    async def __aenter__(self) -> None:
        await self._limiter.acquire(self._host)

    async def __aexit__(self, *exc_info: object) -> None:
        self._limiter.release(self._host)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    retry_on: tuple[type[BaseException], ...],
    attempts: int = 3,
    base_delay: float = 0.5,
    label: str = "operation",
) -> T:
    """Exponential backoff with full jitter.

    Jitter, not a fixed schedule: without it a batch that hits a transient failure retries in
    lockstep and reproduces the thundering herd that caused the failure.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last = exc
            if attempt == attempts:
                break
            ceiling = base_delay * (2 ** (attempt - 1))
            delay = ceiling * (secrets.randbelow(1000) / 1000)
            logger.debug(
                "retrying", label=label, attempt=attempt, delay=round(delay, 3), error=str(exc)
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last
