"""Async DNS, behind a protocol.

The protocol exists so the test suite can inject a deterministic resolver. DNS is the one
dependency here that is genuinely external, genuinely slow, and genuinely flaky; tests that
depend on the real thing are tests that fail when someone's wifi drops, and they teach you
nothing you did not already know about your own code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import dns.asyncresolver
import dns.exception
import dns.resolver

from app.core.logging import get_logger

logger = get_logger(__name__)


class MxLookupError(Exception):
    """The lookup could not be completed. Distinct from 'the domain has no MX records'."""


class NoSuchDomainError(Exception):
    """The domain does not exist (NXDOMAIN). A measurement, not a failure."""


@dataclass(frozen=True, slots=True)
class MxRecord:
    preference: int
    exchange: str


class MxResolver(Protocol):
    """Anything that can answer "what are this domain's mail exchangers?"."""

    async def mx(self, domain: str) -> list[MxRecord]:
        """Return MX records in whatever order the answer arrived, or an empty list when the
        domain exists but publishes none.

        Ordering is normalised by the caller rather than here, so the invariant holds for every
        implementation of this protocol instead of only the one that remembers to sort.

        Raises :class:`NoSuchDomainError` when the domain does not exist, and
        :class:`MxLookupError` for timeouts and server failures — a distinction the caller
        needs, because "no mailbox here" and "we could not find out" are different facts.
        """
        ...


class DnsPythonResolver:
    """Real DNS via ``dnspython``'s async resolver."""

    __slots__ = ("_resolver", "_semaphore", "_timeout")

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        nameservers: list[str] | None = None,
        max_concurrency: int = 32,
    ) -> None:
        resolver = dns.asyncresolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        if nameservers:
            resolver.nameservers = nameservers
        self._resolver = resolver
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def mx(self, domain: str) -> list[MxRecord]:
        async with self._semaphore:
            try:
                answer = await self._resolver.resolve(domain, "MX")
            except dns.resolver.NXDOMAIN as exc:
                raise NoSuchDomainError(domain) from exc
            except dns.resolver.NoAnswer:
                # The domain exists but publishes no MX. RFC 5321 says senders then fall back to
                # the A record, so this is not automatically "no mail" — but in practice, for a
                # lead list, a domain with no MX is a domain nobody reads mail at.
                return []
            except (dns.resolver.NoNameservers, dns.exception.Timeout) as exc:
                raise MxLookupError(f"{type(exc).__name__}: {exc}") from exc
            except dns.exception.DNSException as exc:  # pragma: no cover - defensive
                raise MxLookupError(f"{type(exc).__name__}: {exc}") from exc

        return [
            MxRecord(preference=int(item.preference), exchange=str(item.exchange).rstrip("."))
            for item in answer
        ]


class StaticResolver:
    """A deterministic resolver for tests.

    ``records`` maps domain to MX list; a domain mapped to ``None`` raises
    :class:`NoSuchDomainError`; a domain absent from the mapping raises
    :class:`MxLookupError`, which is how an unreachable resolver behaves.
    """

    __slots__ = ("calls", "records")

    def __init__(self, records: dict[str, list[MxRecord] | None]) -> None:
        self.records = records
        self.calls: list[str] = []

    async def mx(self, domain: str) -> list[MxRecord]:
        self.calls.append(domain)
        if domain not in self.records:
            raise MxLookupError(f"no stub configured for {domain!r}")
        value = self.records[domain]
        if value is None:
            raise NoSuchDomainError(domain)
        return value
