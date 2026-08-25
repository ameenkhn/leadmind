"""Test helpers shared by the API integration modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class QueryCounter:
    """Counts SQL statements issued on one connection.

    Exists so the "constant number of queries per page" claim in ``app.services.leads`` is
    asserted rather than asserted-about. An N+1 does not fail a test by returning wrong data —
    it returns exactly the right data, slowly — so nothing else here would catch the regression.
    """

    count: int = 0

    def __enter__(self) -> QueryCounter:
        self.count = 0
        return self

    def __exit__(self, *exc: object) -> None:
        return None
