"""Fixtures for the API integration tests.

The corpus fixture is **module**-scoped rather than session-scoped on purpose. It holds one
long-lived uncommitted transaction containing the full ingest; a session-scoped version would
still be open when the golden ingest tests run, and their writes would block on the same unique
keys until it ended. Module scope means pytest tears each one down before the next module starts,
at the cost of one extra ingest per module.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from tests.integration.helpers import QueryCounter

SEEDED_UNREACHABLE = 5
SEEDED_UNKNOWN = 3
SEEDED_VERIFIED = 40


def seed_verification(session: Session) -> dict[str, list[str]]:
    """Write a deterministic slice of verification results.

    Real verification needs DNS and HTTP, which a test must not. But a filter that reads
    ``domain_verifications`` cannot be tested against a table nobody wrote to — it would pass by
    returning nothing, which is exactly the bug it exists to catch. So the outcomes are seeded
    here: a block of verified domains, a few proven unreachable, and a few ``unknown``, because
    the distinction between the last two is the one most worth asserting.

    Domains are taken in sorted order from the corpus itself, so the same rows are chosen on
    every run.
    """
    from app.models.verification import DomainVerificationRecord, UrlVerificationRecord
    from app.verification.runner import email_domains_with_counts, expiry_for, owned_website_urls
    from app.verification.types import MailProvider, VerificationStatus

    now = dt.datetime.now(dt.UTC)
    domains = [domain for domain, _ in sorted(email_domains_with_counts(session))]
    plan: list[tuple[str, VerificationStatus, bool, MailProvider]] = []
    cursor = 0
    for domain in domains[cursor : cursor + SEEDED_UNREACHABLE]:
        plan.append((domain, VerificationStatus.UNREACHABLE, False, MailProvider.NONE))
    cursor += SEEDED_UNREACHABLE
    for domain in domains[cursor : cursor + SEEDED_UNKNOWN]:
        plan.append((domain, VerificationStatus.UNKNOWN, False, MailProvider.NONE))
    cursor += SEEDED_UNKNOWN
    for domain in domains[cursor : cursor + SEEDED_VERIFIED]:
        plan.append((domain, VerificationStatus.VERIFIED, True, MailProvider.GOOGLE))

    for domain, status, has_mx, provider in plan:
        session.add(
            DomainVerificationRecord(
                domain=domain,
                status=status,
                has_mx=has_mx,
                provider=provider,
                is_freemail=False,
                checked_at=now,
                expires_at=expiry_for(status, now),
            )
        )

    urls = sorted(owned_website_urls(session))[:10]
    for index, url in enumerate(urls):
        parked = index == 0
        live = index > 1
        status = VerificationStatus.VERIFIED if index != 1 else VerificationStatus.UNREACHABLE
        session.add(
            UrlVerificationRecord(
                url=url,
                host=url.split("//", 1)[-1].split("/", 1)[0],
                status=status,
                status_code=200 if live or parked else None,
                is_live=live,
                is_parked=parked,
                checked_at=now,
                expires_at=expiry_for(status, now),
            )
        )

    session.flush()
    return {
        "unreachable": [d for d, s, _, _ in plan if s is VerificationStatus.UNREACHABLE],
        "unknown": [d for d, s, _, _ in plan if s is VerificationStatus.UNKNOWN],
        "verified": [d for d, s, _, _ in plan if s is VerificationStatus.VERIFIED],
    }


@pytest.fixture(scope="module")
def api_corpus(engine: Engine, workbook_path: Path) -> Iterator[tuple[TestClient, Session]]:
    """The API, served over one full ingest of the real workbook, rolled back afterwards."""
    from app.api.app import create_app
    from app.api.deps import get_db
    from app.ingestion.pipeline import ingest

    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, expire_on_commit=False)
    ingest(workbook_path, session)
    seed_verification(session)

    app: FastAPI = create_app()

    def _session_override() -> Iterator[Session]:
        # Deliberately no commit: every request shares the fixture's transaction, so the review
        # endpoints' writes are visible to later requests in the same test and vanish at the end.
        yield session

    app.dependency_overrides[get_db] = _session_override

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(api_corpus: tuple[TestClient, Session]) -> TestClient:
    return api_corpus[0]


@pytest.fixture
def corpus_session(api_corpus: tuple[TestClient, Session]) -> Session:
    return api_corpus[1]


@pytest.fixture
def query_counter(corpus_session: Session) -> Iterator[QueryCounter]:
    counter = QueryCounter()
    connection = corpus_session.get_bind()

    def _before(*_args: object, **_kwargs: object) -> None:
        counter.count += 1

    event.listen(connection, "before_cursor_execute", _before)
    try:
        yield counter
    finally:
        event.remove(connection, "before_cursor_execute", _before)
