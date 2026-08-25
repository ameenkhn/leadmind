"""HTTP layer.

Routers do HTTP and nothing else: parse parameters, call a service, choose a status code. All
decisions about what is true live in ``app.services``, so they can be tested — and reused by a
CLI command or a background job — without a request object.
"""

from __future__ import annotations

from app.api.app import create_app

__all__ = ["create_app"]
