"""In-memory implementations of the ports, for tests (here and in host apps) and spikes."""

from __future__ import annotations

import copy
import itertools
import json as _json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .errors import CredentialsRejected, NotFound, ReconnectRequired
from .events import MxEvent
from .graph import PostedMessage, image_tags
from .models import DEAD, DONE, LEASED, PENDING, ChannelTarget, PostRecord, QueuedEvent
from .ports import HttpResponse


class FixedClock:
    """A clock that only moves when told to; `sleep` advances it instead of blocking."""

    def __init__(self, now: datetime):
        self._now = now
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self._now += timedelta(seconds=seconds)

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    json: Any
    content: bytes | None


@dataclass
class _Route:
    method: str
    url_contains: str
    respond: Callable[[RecordedRequest], HttpResponse]
    remaining: int | None


class FakeTransport:
    """A scripted HttpTransport. Routes match on method + URL substring, first match wins;
    a route added with `times=n` is used n times and then skipped. Unmatched requests fail
    the test loudly instead of silently returning something."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._routes: list[_Route] = []

    def add(
        self,
        method: str,
        url_contains: str,
        *,
        status: int = 200,
        json: Any = None,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        times: int | None = None,
        respond: Callable[[RecordedRequest], HttpResponse] | None = None,
    ) -> FakeTransport:
        if respond is None:
            payload = body if body is not None else (_json.dumps(json).encode() if json else b"")
            response = HttpResponse(status=status, headers=headers or {}, body=payload)

            def respond(_request: RecordedRequest, _response: HttpResponse = response):
                return _response

        self._routes.append(_Route(method, url_contains, respond, times))
        return self

    def send(self, method, url, *, headers=None, json=None, content=None, timeout=30.0):
        request = RecordedRequest(method, url, dict(headers or {}), json, content)
        self.requests.append(request)
        for route in self._routes:
            if route.method != method or route.url_contains not in url:
                continue
            if route.remaining is not None:
                if route.remaining <= 0:
                    continue
                route.remaining -= 1
            return route.respond(request)
        raise AssertionError(f"unexpected request: {method} {url}")


class FakeTokenProvider:
    """Hands out token-1, token-2, …; records each call's force_refresh flag."""

    def __init__(self, reconnect: bool = False) -> None:
        self.calls: list[bool] = []
        self.reconnect = reconnect

    def access_token(self, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        if self.reconnect:
            raise ReconnectRequired("poster sign-in expired")
        return f"token-{len(self.calls)}"


class FakeMaintainX:
    """Stands in for MaintainXClient: dicts of records, NotFound for anything missing."""

    def __init__(self) -> None:
        self.work_requests: dict[int, dict[str, Any]] = {}
        self.work_orders: dict[int, dict[str, Any]] = {}
        self.users: dict[int, dict[str, Any]] = {}
        self.subscriptions: dict[int, dict[str, Any]] = {}
        self.missing_files: set[str] = set()  # download() raises NotFound for these URLs
        self.reject_credentials = False  # every API call raises CredentialsRejected (bad key)
        self.calls: list[tuple[str, Any]] = []

    def get_work_request(self, request_id: int) -> dict[str, Any]:
        return self._get("get_work_request", self.work_requests, request_id)

    def get_work_order(self, work_order_id: int) -> dict[str, Any]:
        return self._get("get_work_order", self.work_orders, work_order_id)

    def get_user(self, user_id: int) -> dict[str, Any]:
        return self._get("get_user", self.users, user_id)

    def list_work_requests(self, statuses: Iterable[str], limit: int = 100) -> list[dict[str, Any]]:
        wanted = set(statuses)
        self.calls.append(("list_work_requests", tuple(sorted(wanted))))
        self._check_credentials()
        matches = [r for r in self.work_requests.values() if r.get("requestStatus") in wanted]
        return copy.deepcopy(matches)

    def get_subscription(self, subscription_id: int) -> dict[str, Any] | None:
        self.calls.append(("get_subscription", subscription_id))
        self._check_credentials()
        return copy.deepcopy(self.subscriptions.get(subscription_id))

    def download(self, url: str) -> tuple[bytes, str]:
        self.calls.append(("download", url))
        if url in self.missing_files:
            raise NotFound("attachment download: 404", status=404)
        return b"img:" + url.encode(), "image/jpeg"

    def _check_credentials(self) -> None:
        if self.reject_credentials:
            raise CredentialsRejected("MaintainX GET: 401 Invalid token")

    def _get(self, name: str, table: dict[int, dict[str, Any]], key: int) -> dict[str, Any]:
        self.calls.append((name, key))
        self._check_credentials()
        if key not in table:
            raise NotFound(f"{name} {key}: 404", status=404)
        return copy.deepcopy(table[key])


class FakeGraph:
    """Stands in for GraphClient. `messages[id]` holds team, channel, subject, html, the img tags
    currently in the body (`images`), `edits`, `replies` and `created`. Queue failures with
    fail_next(); set `reconnect` to make every call raise ReconnectRequired."""

    def __init__(self, clock: FixedClock | None = None) -> None:
        self.channels: dict[str, dict[str, str]] = {}  # team -> {casefolded name: channel id}
        self.messages: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []
        self.reconnect = False
        self.token_refreshes = 0
        self.searches = 0
        self._clock = clock
        self._failures: list[tuple[str, Exception, bool]] = []
        self._ids = itertools.count(1)

    def fail_next(self, operation: str, error: Exception, after_commit: bool = False) -> None:
        """operation: "post", "edit" or "reply". With after_commit the write happens and the
        error is raised afterwards: a response lost after Graph accepted the request."""
        self._failures.append((operation, error, after_commit))

    def post_message(self, team_id, channel_id, subject, html, images) -> PostedMessage:
        late = self._check("post")
        message_id = f"m{next(self._ids)}"
        hosted = (
            f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}"
            f"/messages/{message_id}/hostedContents/"
        )
        body = html.replace("../hostedContents/", hosted)
        self.messages[message_id] = {
            "team": team_id,
            "channel": channel_id,
            "subject": subject,
            "html": body,
            "images": " ".join(image_tags(body)),
            "edits": [],
            "replies": [],
            "deleted": False,
            "created": self._clock.now() if self._clock else None,
        }
        self.posts.append(
            {
                "id": message_id,
                "team": team_id,
                "channel": channel_id,
                "subject": subject,
                "html": html,
                "images": list(images),
            }
        )
        if late:
            raise late
        return PostedMessage(message_id, f"https://teams.example/{message_id}", body)

    def update_message(self, team_id, channel_id, message_id, subject, html) -> None:
        late = self._check("edit")
        message = self._message(message_id)
        message.update(subject=subject, html=html, images=" ".join(image_tags(html)))
        message["edits"].append({"subject": subject, "html": html})
        if late:
            raise late

    def post_reply(self, team_id, channel_id, message_id, html) -> str:
        late = self._check("reply")
        message = self._message(message_id)
        message["replies"].append({"html": html})
        if late:
            raise late
        return f"{message_id}-r{len(message['replies'])}"

    def find_channel_id(self, team_id: str, display_name: str) -> str | None:
        self._check(None)
        return self.channels.get(team_id, {}).get(display_name.strip().casefold())

    def find_root_message(
        self, team_id, channel_id, marker, top=50, created_after=None
    ) -> PostedMessage | None:
        self._check(None)
        self.searches += 1
        pattern = re.compile(re.escape(marker) + r"(?!\d)")
        for message_id, message in self.messages.items():
            if (message["team"], message["channel"]) != (team_id, channel_id):
                continue
            created = message.get("created")
            if created_after and created and created < created_after:
                continue
            if not message["deleted"] and pattern.search(message["html"]):
                return PostedMessage(
                    message_id, f"https://teams.example/{message_id}", message["html"]
                )
        return None

    def refresh_token(self) -> None:
        self._check(None)
        self.token_refreshes += 1

    def _check(self, operation: str | None) -> Exception | None:
        """Raise a queued failure now, or return it to raise after the write commits."""
        if self.reconnect:
            raise ReconnectRequired("poster sign-in expired")
        for index, (name, error, after_commit) in enumerate(self._failures):
            if name == operation:
                del self._failures[index]
                if after_commit:
                    return error
                raise error
        return None

    def _message(self, message_id: str) -> dict[str, Any]:
        message = self.messages.get(message_id)
        if message is None or message["deleted"]:
            raise NotFound(f"message {message_id}: 404", status=404)
        return message


class StaticChannels:
    def __init__(self, targets: dict[int, ChannelTarget]):
        self.targets = targets
        self.remembered: list[tuple[int, str, str]] = []

    def resolve(self, mx_location_id: int) -> ChannelTarget | None:
        return self.targets.get(mx_location_id)

    def remember_channel_id(self, mx_location_id: int, team_id: str, channel_id: str) -> None:
        self.remembered.append((mx_location_id, team_id, channel_id))


class DictDirectory:
    def __init__(self, names: dict[str, str]):
        self.names = {email.lower(): name for email, name in names.items()}

    def name_for_email(self, email: str) -> str | None:
        return self.names.get(email.lower())


class MemoryStore:
    """A FeedStore in a dict. Returns copies so callers can't mutate state without saving."""

    def __init__(self) -> None:
        self.events: dict[str, QueuedEvent] = {}
        self.posts: dict[int, PostRecord] = {}
        self._order: dict[str, int] = {}
        self._seq = itertools.count()
        self._save_failures: list[Callable[[PostRecord], bool]] = []

    def fail_next_save_matching(self, predicate: Callable[[PostRecord], bool]) -> None:
        """Make the next save_post whose record matches report a version conflict."""
        self._save_failures.append(predicate)

    # --- inbox -------------------------------------------------------------------------

    def enqueue(self, event: MxEvent, now: datetime) -> bool:
        if event.key in self.events:
            return False
        self.events[event.key] = QueuedEvent(
            event=event, state=PENDING, next_attempt_at=now, received_at=now
        )
        self._order[event.key] = next(self._seq)
        return True

    def claim(self, now: datetime, limit: int, lease_seconds: int) -> list[QueuedEvent]:
        due = [q for q in self.events.values() if self._is_due(q, now)]
        due.sort(key=lambda q: (q.received_at or now, self._order[q.event.key]))
        claimed = []
        for queued in due[:limit]:
            queued.state = LEASED
            queued.attempts += 1
            queued.lease_until = now + timedelta(seconds=lease_seconds)
            claimed.append(copy.deepcopy(queued))
        return claimed

    def complete(self, key: str, outcome: str, now: datetime) -> None:
        queued = self.events[key]
        queued.state, queued.outcome, queued.finished_at = DONE, outcome, now
        queued.lease_until = None

    def fail(self, key: str, error: str, now: datetime, retry_at: datetime | None) -> None:
        queued = self.events[key]
        queued.last_error = error
        queued.lease_until = None
        if retry_at is None:
            queued.state, queued.finished_at = DEAD, now
        else:
            queued.state, queued.next_attempt_at = PENDING, retry_at

    def release(self, key: str, now: datetime) -> None:
        queued = self.events[key]
        queued.state, queued.next_attempt_at, queued.lease_until = PENDING, now, None
        queued.attempts = max(0, queued.attempts - 1)

    def get_event(self, key: str) -> QueuedEvent | None:
        queued = self.events.get(key)
        return copy.deepcopy(queued) if queued else None

    def prune(self, finished_before: datetime) -> int:
        old = [
            key
            for key, q in self.events.items()
            if q.state in (DONE, DEAD) and q.finished_at and q.finished_at < finished_before
        ]
        for key in old:
            del self.events[key]
        return len(old)

    @staticmethod
    def _is_due(queued: QueuedEvent, now: datetime) -> bool:
        if queued.state == PENDING:
            return queued.next_attempt_at is None or queued.next_attempt_at <= now
        if queued.state == LEASED:
            return queued.lease_until is not None and queued.lease_until <= now
        return False

    # --- posts -------------------------------------------------------------------------

    def get_post(self, work_order_id: int) -> PostRecord | None:
        record = self.posts.get(work_order_id)
        return copy.deepcopy(record) if record else None

    def create_post(self, record: PostRecord) -> bool:
        if record.work_order_id in self.posts:
            return False
        self.posts[record.work_order_id] = copy.deepcopy(record)
        return True

    def save_post(self, record: PostRecord, expected_version: int) -> bool:
        for index, predicate in enumerate(self._save_failures):
            if predicate(record):
                del self._save_failures[index]
                return False
        current = self.posts.get(record.work_order_id)
        if current is None or current.version != expected_version:
            return False
        saved = copy.deepcopy(record)
        saved.version = expected_version + 1
        self.posts[record.work_order_id] = saved
        return True

    def list_posts(self, states: Iterable[str]) -> list[PostRecord]:
        wanted = set(states)
        return [copy.deepcopy(p) for p in self.posts.values() if p.state in wanted]
