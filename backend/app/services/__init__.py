"""Query and decision logic, deliberately free of FastAPI.

The API layer's job is HTTP: parse, validate, serialise, choose a status code. Everything that
decides *what is true* — which leads match a filter, which lead survives a merge, whether a
decision conflicts with the current state — lives here, where it can be called from a test, a
CLI command or a background job without a request object in sight. It is also what keeps the
routers short enough to read.
"""

from __future__ import annotations
