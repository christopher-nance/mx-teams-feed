"""The seams a host app implements. The library never imports a web framework, database
client, HTTP library or MSAL; V3 (Flask + Firebase RTDB) and V4 (Flask + Firestore) each
supply adapters for these protocols.
"""

from __future__ import annotations

import json as _json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .events import MxEvent
from .models import ChannelTarget, PostRecord, QueuedEvent


class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware current time."""

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return _json.loads(self.body) if self.body else None

    def header(self, name: str) -> str | None:
        lowered = name.lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return None


class HttpTransport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Perform one request. Raise TransientError on connection failures/timeouts."""


class GraphTokenProvider(Protocol):
    def access_token(self, force_refresh: bool = False) -> str:
        """A delegated Graph token for the poster account; raise ReconnectRequired when the
        refresh token is gone (password reset, revoked sessions, consent change)."""


class ChannelResolver(Protocol):
    def resolve(self, mx_location_id: int) -> ChannelTarget | None:
        """The enabled Teams target for a MaintainX location, or None (not enabled/mapped)."""

    def remember_channel_id(self, mx_location_id: int, team_id: str, channel_id: str) -> None:
        """Cache a channel id the engine looked up by name. May be a no-op."""


class PeopleDirectory(Protocol):
    def name_for_email(self, email: str) -> str | None: ...


class ImageProcessor(Protocol):
    def fit(
        self, data: bytes, mime_type: str, max_pixels: int, max_bytes: int
    ) -> tuple[bytes, str] | None:
        """Downscale/re-encode to fit the limits; return (bytes, mime) or None if impossible."""


class FeedStore(Protocol):
    """Durable inbox + post state. Implementations must make `enqueue`, `create_post` and
    `save_post` atomic (RTDB transactions / Firestore create() and @transactional) and must not
    serve these reads from a cache."""

    def enqueue(self, event: MxEvent, now: datetime) -> bool:
        """Store a new event; False if this key was already queued (duplicate delivery)."""

    def claim(self, now: datetime, limit: int, lease_seconds: int) -> list[QueuedEvent]:
        """Lease up to `limit` due events, oldest first, incrementing their attempt count."""

    def complete(self, key: str, outcome: str, now: datetime) -> None: ...

    def fail(self, key: str, error: str, now: datetime, retry_at: datetime | None) -> None:
        """Record a failure; retry at `retry_at`, or dead-letter the event when it is None."""

    def release(self, key: str, now: datetime) -> None:
        """Return a leased event unprocessed, without spending an attempt."""

    def get_event(self, key: str) -> QueuedEvent | None: ...

    def get_post(self, work_order_id: int) -> PostRecord | None: ...

    def create_post(self, record: PostRecord) -> bool:
        """Insert only if no record exists for this work order; False if one does."""

    def save_post(self, record: PostRecord, expected_version: int) -> bool:
        """Compare-and-set on `version`; on success the stored version is expected_version+1."""

    def list_posts(self, states: Iterable[str]) -> list[PostRecord]: ...

    def prune(self, finished_before: datetime) -> int:
        """Delete finished (done/dead) events older than the cutoff; return how many."""
