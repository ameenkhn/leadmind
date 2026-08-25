"""SSRF guard and pacing tests.

Every URL this system checks came out of a scraped spreadsheet. These tests are the reason it is
safe to hand such a URL to an HTTP client: they assert that the guard refuses loopback, private,
link-local and metadata addresses, that it does so *after* DNS resolution (a hostname is free to
point at 127.0.0.1), and that non-web ports are refused rather than dialled.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.verification.net import (
    ALLOWED_PORTS,
    HostLimiter,
    UnsafeUrlError,
    is_public_address,
    resolve_public_address,
    retry_async,
)


class TestAddressClassification:
    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",  # loopback: your own machine
            "10.0.0.5",  # RFC1918
            "172.16.4.1",
            "192.168.1.1",
            "169.254.169.254",  # cloud instance metadata — the classic SSRF prize
            "0.0.0.0",
            "224.0.0.1",  # multicast
            "::1",
            "fe80::1",  # link-local
            "fc00::1",  # unique local
        ],
    )
    def test_non_public_addresses_are_rejected(self, address: str) -> None:
        assert is_public_address(address) is False

    @pytest.mark.parametrize("address", ["8.8.8.8", "142.251.42.14", "2606:4700::1111"])
    def test_public_addresses_are_accepted(self, address: str) -> None:
        assert is_public_address(address) is True

    def test_garbage_is_not_public(self) -> None:
        assert is_public_address("not-an-address") is False


class TestResolveGuard:
    async def test_localhost_is_refused_even_though_it_resolves(self) -> None:
        """A hostname is free to point at 127.0.0.1, so the check must follow resolution."""
        with pytest.raises(UnsafeUrlError) as info:
            await resolve_public_address("localhost", 80)
        assert "non-public" in info.value.reason

    async def test_allow_private_is_opt_in_for_tests_only(self) -> None:
        addresses = await resolve_public_address("localhost", 80, allow_private=True)
        assert addresses

    @pytest.mark.parametrize("port", [22, 25, 3306, 5432, 6379, 11211])
    async def test_non_web_ports_are_refused_before_any_connection(self, port: int) -> None:
        """Refused on the port alone, so a crafted URL cannot become a port scanner."""
        assert port not in ALLOWED_PORTS
        with pytest.raises(UnsafeUrlError) as info:
            await resolve_public_address("example.com", port)
        assert "not permitted" in info.value.reason

    async def test_unresolvable_host_is_reported_distinctly(self) -> None:
        with pytest.raises(UnsafeUrlError) as info:
            await resolve_public_address(
                "no-such-host.invalid-tld-that-cannot-exist", 443, allow_private=True
            )
        assert "does not resolve" in info.value.reason


class TestHostLimiter:
    async def test_concurrency_is_capped_per_host(self) -> None:
        limiter = HostLimiter(per_host=2, min_interval_seconds=0)
        active = 0
        peak = 0

        async def worker() -> None:
            nonlocal active, peak
            async with limiter.slot("example.com"):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.02)
                active -= 1

        await asyncio.gather(*(worker() for _ in range(8)))
        assert peak <= 2

    async def test_minimum_interval_is_enforced(self) -> None:
        limiter = HostLimiter(per_host=1, min_interval_seconds=0.05)
        started = time.monotonic()
        for _ in range(3):
            async with limiter.slot("example.com"):
                pass
        assert time.monotonic() - started >= 0.10

    async def test_different_hosts_do_not_block_each_other(self) -> None:
        limiter = HostLimiter(per_host=1, min_interval_seconds=0.05)
        started = time.monotonic()

        async def touch(host: str) -> None:
            async with limiter.slot(host):
                pass

        await asyncio.gather(*(touch(f"host{i}.example") for i in range(6)))
        assert time.monotonic() - started < 0.10


class TestRetry:
    async def test_retries_then_succeeds(self) -> None:
        attempts = 0

        async def flaky() -> str:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ValueError("transient")
            return "ok"

        result = await retry_async(flaky, retry_on=(ValueError,), attempts=3, base_delay=0.001)
        assert result == "ok"
        assert attempts == 3

    async def test_gives_up_and_reraises_the_last_error(self) -> None:
        async def always_fails() -> None:
            raise ValueError("permanent")

        with pytest.raises(ValueError, match="permanent"):
            await retry_async(always_fails, retry_on=(ValueError,), attempts=2, base_delay=0.001)

    async def test_unlisted_exceptions_are_not_retried(self) -> None:
        attempts = 0

        async def wrong_error() -> None:
            nonlocal attempts
            attempts += 1
            raise KeyError("not retryable")

        with pytest.raises(KeyError):
            await retry_async(wrong_error, retry_on=(ValueError,), attempts=3, base_delay=0.001)
        assert attempts == 1
