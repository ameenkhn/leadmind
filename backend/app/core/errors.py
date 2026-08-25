"""Typed exception hierarchy.

Every failure that can reasonably be *attributed* carries the offending value, so an operator
reading a log line can act on it without re-running the pipeline.
"""

from __future__ import annotations

from typing import Any


class LeadMindError(Exception):
    """Base class for every error raised by LeadMind."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = " ".join(f"{k}={v!r}" for k, v in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


class ConfigurationError(LeadMindError):
    """A config file is missing, malformed, or internally inconsistent."""


class SchemaMismatchError(LeadMindError):
    """An input sheet has columns the reader was not told about.

    Raised rather than swallowed: silently ignoring an unknown column is how a dataset quietly
    loses a field between one export and the next.
    """


class NormalizationError(LeadMindError):
    """A normalizer could not produce a value it is contractually required to produce."""


class IngestionError(LeadMindError):
    """The ingest pipeline could not complete."""


class NotFoundError(LeadMindError):
    """A requested resource does not exist."""


class ConflictError(LeadMindError):
    """The request is well-formed but conflicts with the current state of the resource.

    Raised in the service layer rather than the API layer so the rule lives with the invariant it
    protects, and so the same guard applies to a CLI or a background job that never sees HTTP.
    """


class InvalidRequestError(LeadMindError):
    """The request is syntactically valid but semantically wrong for this resource."""
