"""The webhook front door: verify, normalize, queue, acknowledge. No processing here.

A host route passes the raw body, headers and full request URL, then returns
`result.status` to MaintainX. MaintainX times out after 10 s, so nothing slow runs here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from . import signature
from .errors import InvalidPayload
from .events import WEBHOOK_EVENT_TYPES, parse_event
from .ports import Clock, FeedStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReceiveResult:
    status: int
    outcome: str


class WebhookReceiver:
    def __init__(
        self,
        *,
        store: FeedStore,
        secrets: Callable[[], Sequence[str]],
        clock: Clock,
        org_id: int | None,
        event_types: frozenset[str] = WEBHOOK_EVENT_TYPES,
    ):
        self._store = store
        self._secrets = secrets
        self._clock = clock
        self._org_id = org_id
        self._event_types = event_types

    def handle(self, raw_body: bytes, headers: Mapping[str, str], url: str) -> ReceiveResult:
        now = self._clock.now()
        if not signature.verify(raw_body, headers, url, self._secrets(), now):
            log.warning("maintainx webhook rejected: bad or missing signature")
            return ReceiveResult(401, "rejected:signature")
        try:
            event = parse_event(raw_body)
        except InvalidPayload:
            return ReceiveResult(400, "rejected:payload")
        if self._org_id is not None and event.org_id != self._org_id:
            return ReceiveResult(200, "ignored:org")
        if event.event_type not in self._event_types:
            return ReceiveResult(200, "ignored:type")
        queued = self._store.enqueue(event, now)
        log.info("maintainx webhook %s %s", event.event_type, "queued" if queued else "duplicate")
        return ReceiveResult(200, "queued" if queued else "duplicate")
