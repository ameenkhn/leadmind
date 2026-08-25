"""Website liveness tests against a real local HTTP server.

Not mocked. A stubbed transport would test that the mock returns what the mock was told to
return; these tests exercise the actual socket, the actual redirect following, the actual
HEAD-then-GET fallback and the actual timeout handling, which is where the behaviour that
matters lives.

The server binds loopback, so the verifier is constructed with ``allow_private=True`` — the one
place in the codebase that is permitted, and an explicit argument precisely so it is visible.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.verification.net import HostLimiter
from app.verification.types import VerificationStatus
from app.verification.website import WebsiteVerifier, looks_parked

pytestmark = pytest.mark.integration

PARKED_BODY = (
    b"<html><head><title>Buy this domain</title></head><body>This domain is for sale</body></html>"
)
LIVE_BODY = (
    b"<html><head><title>  Aasha  Ayurveda   Clinic </title></head>"
    b"<body><h1>Panchakarma in Pune</h1></body></html>"
)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # silence the default stderr spam
        return

    def _send(self, code: int, body: bytes = b"", content_type: str = "text/html") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Server", "TestServer/1.0")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        path = self.path
        if path == "/head-405":
            self._send(405)
        elif path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/live")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/missing":
            self._send(404)
        elif path == "/boom":
            self._send(500)
        else:
            self._send(200)

    def do_GET(self) -> None:
        path = self.path
        if path == "/live" or path == "/head-405":
            self._send(200, LIVE_BODY)
        elif path == "/parked":
            self._send(200, PARKED_BODY)
        elif path == "/redirect":
            self.send_response(301)
            self.send_header("Location", "/live")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop":
            self.send_response(301)
            self.send_header("Location", "/loop2")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/loop2":
            self.send_response(301)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/slow":
            import time

            time.sleep(2.0)
            self._send(200, LIVE_BODY)
        elif path == "/missing":
            self._send(404, b"not found")
        elif path == "/boom":
            self._send(500, b"server error")
        else:
            self._send(200, LIVE_BODY)


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    # A fixed port from ALLOWED_PORTS, not an ephemeral one: the SSRF guard refuses any port it
    # does not recognise as a web port, and it is right to. That refusal showed up as ten failing
    # tests the first time these ran, which is the guard doing its job.
    httpd: ThreadingHTTPServer | None = None
    for candidate in (8080, 8443):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
            break
        except OSError:
            continue
    if httpd is None:
        pytest.skip("no permitted local port free for the test server")

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def verifier() -> Iterator[WebsiteVerifier]:
    yield WebsiteVerifier(
        timeout=1.0,
        limiter=HostLimiter(per_host=4, min_interval_seconds=0),
        allow_private=True,
        attempts=1,
    )


async def _verify(verifier: WebsiteVerifier, url: str):  # type: ignore[no-untyped-def]
    async with verifier as active:
        return await active.verify(url)


class TestLiveness:
    async def test_live_page_is_verified(self, server: str, verifier: WebsiteVerifier) -> None:
        result = await _verify(verifier, f"{server}/live")
        assert result.status is VerificationStatus.VERIFIED
        assert result.status_code == 200
        assert result.is_live
        assert result.title == "Aasha Ayurveda Clinic"
        assert "TestServer/1.0" in (result.server or "")
        assert result.latency_ms >= 0

    async def test_redirects_are_followed_and_recorded(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        result = await _verify(verifier, f"{server}/redirect")
        assert result.is_live
        assert result.redirect_count >= 1
        assert result.final_url is not None
        assert result.final_url.endswith("/live")

    async def test_head_rejection_falls_back_to_get(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        """Small-business hosting often 405s HEAD while serving GET. Not a dead site."""
        result = await _verify(verifier, f"{server}/head-405")
        assert result.is_live
        assert result.status_code == 200

    async def test_404_is_a_measurement_but_not_live(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        result = await _verify(verifier, f"{server}/missing")
        assert result.status is VerificationStatus.VERIFIED
        assert result.status_code == 404
        assert result.is_live is False

    async def test_server_error_is_unknown_not_dead(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        """A 500 means the host is up and something is broken today; that may not last."""
        result = await _verify(verifier, f"{server}/boom")
        assert result.status is VerificationStatus.UNKNOWN
        assert result.status_code == 500
        assert result.is_live is False

    async def test_parked_page_is_not_live(self, server: str, verifier: WebsiteVerifier) -> None:
        """A registrar's for-sale page answers 200 and contains no evidence about anyone."""
        result = await _verify(verifier, f"{server}/parked")
        assert result.status_code == 200
        assert result.is_parked is True
        assert result.is_live is False

    async def test_redirect_loop_is_unreachable(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        result = await _verify(verifier, f"{server}/loop")
        assert result.status in (
            VerificationStatus.UNREACHABLE,
            VerificationStatus.UNKNOWN,
        )
        assert result.is_live is False

    async def test_timeout_is_unknown_not_unreachable(
        self, server: str, verifier: WebsiteVerifier
    ) -> None:
        """The host answered the handshake; slow is not the same as gone."""
        result = await _verify(verifier, f"{server}/slow")
        assert result.status is VerificationStatus.UNKNOWN
        assert result.is_live is False


class TestSafety:
    async def test_private_address_is_refused_when_not_opted_in(self, server: str) -> None:
        """The same URL the other tests use, with the guard on: refused, not fetched."""
        strict = WebsiteVerifier(timeout=1.0, allow_private=False, attempts=1)
        result = await _verify(strict, f"{server}/live")
        assert result.status is VerificationStatus.SKIPPED
        assert result.is_live is False
        assert "non-public" in (result.error or "")

    async def test_non_http_scheme_is_skipped(self, verifier: WebsiteVerifier) -> None:
        result = await _verify(verifier, "ftp://example.com/file")
        assert result.status is VerificationStatus.SKIPPED
        assert "unsupported URL scheme" in (result.error or "")

    async def test_unresolvable_host_is_unreachable(self, verifier: WebsiteVerifier) -> None:
        result = await _verify(verifier, "https://no-such-host.invalid-tld-cannot-exist/")
        assert result.status is VerificationStatus.UNREACHABLE


class TestPoliteness:
    async def test_per_host_pacing_applies_to_real_requests(self, server: str) -> None:
        import time

        verifier = WebsiteVerifier(
            timeout=2.0,
            limiter=HostLimiter(per_host=1, min_interval_seconds=0.15),
            allow_private=True,
            attempts=1,
        )
        started = time.monotonic()
        async with verifier as active:
            await asyncio.gather(*(active.verify(f"{server}/live") for _ in range(3)))
        assert time.monotonic() - started >= 0.30


class TestParkedDetection:
    @pytest.mark.parametrize(
        "body",
        [
            "This domain is for sale",
            "Welcome to nginx!",
            "Apache2 Ubuntu Default Page",
            "Coming soon",
            "<h1>Index of /</h1>",
        ],
    )
    def test_placeholder_pages_are_detected(self, body: str) -> None:
        assert looks_parked(None, body) is True

    def test_a_real_business_page_is_not_parked(self) -> None:
        assert (
            looks_parked(
                "Aasha Ayurveda Clinic",
                "<h1>Panchakarma treatments in Pune</h1><p>Book a consultation.</p>",
            )
            is False
        )
