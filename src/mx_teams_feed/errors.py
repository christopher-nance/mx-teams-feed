"""Exception types. The engine decides retry vs dead-letter purely from these classes."""

from __future__ import annotations


class FeedError(Exception):
    """Base class for everything this library raises on purpose."""


class InvalidPayload(FeedError):
    """A webhook body that is not a MaintainX event."""


class TransientError(FeedError):
    """Worth retrying later: throttling, 5xx, timeouts, write conflicts."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ConflictError(TransientError):
    """A state record changed underneath us (optimistic-concurrency miss)."""


class PermanentError(FeedError):
    """Retrying will not help (4xx other than throttling)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(PermanentError):
    """The remote object does not exist (404)."""


class PauseFeed(FeedError):
    """Nothing can work until a person fixes something. The engine stops claiming events and
    loses nothing; queued events wait."""


class ReconnectRequired(PauseFeed):
    """The poster account's Microsoft sign-in must be renewed interactively from a host
    admin page (password reset, revoked sessions, consent change)."""


class CredentialsRejected(PauseFeed):
    """A configured credential was refused (MaintainX API key, or a Graph token that still
    fails after a refresh)."""
