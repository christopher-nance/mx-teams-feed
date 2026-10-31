"""The worker: drain the inbox, keep one Teams root post per approved work order current.

Run `run_once()` on a short interval from exactly one worker process, and `reconcile()` once
a night. Each reply is recorded on the post record (by the change it announces) before the
next write, so an event retried after a partial failure doesn't repeat what it already sent.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from . import events, planner, render
from .errors import (
    ConflictError,
    NotFound,
    PauseFeed,
    PermanentError,
    TransientError,
)
from .events import MxEvent
from .graph import HostedImage, PostedMessage, image_tags
from .media import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_IMAGES,
    DEFAULT_MAX_PIXELS,
    collect_media,
    prepare_images,
)
from .models import (
    ABANDONED,
    ACTIVE,
    CREATING,
    DELETED,
    LOST,
    TERMINAL_STATUSES,
    ChannelTarget,
    PostRecord,
    QueuedEvent,
    WorkOrderSnapshot,
)
from .ports import ChannelResolver, Clock, FeedStore, ImageProcessor, PeopleDirectory

log = logging.getLogger(__name__)


class MaintainXApi(Protocol):
    def get_work_request(self, request_id: int) -> dict[str, Any]: ...
    def get_work_order(self, work_order_id: int) -> dict[str, Any]: ...
    def get_user(self, user_id: int) -> dict[str, Any]: ...
    def list_work_requests(self, statuses: Iterable[str], limit: int = 100) -> list[dict]: ...
    def get_subscription(self, subscription_id: int) -> dict[str, Any] | None: ...
    def download(self, presigned_url: str) -> tuple[bytes, str]: ...


class TeamsApi(Protocol):
    def post_message(
        self, team_id: str, channel_id: str, subject: str, html: str, images: Sequence[HostedImage]
    ) -> PostedMessage: ...
    def update_message(
        self, team_id: str, channel_id: str, message_id: str, subject: str, html: str
    ) -> None: ...
    def post_reply(self, team_id: str, channel_id: str, message_id: str, html: str) -> str: ...
    def find_channel_id(self, team_id: str, display_name: str) -> str | None: ...
    def find_root_message(
        self,
        team_id: str,
        channel_id: str,
        marker: str,
        top: int = 50,
        created_after: datetime | None = None,
    ) -> PostedMessage | None: ...
    def refresh_token(self) -> None: ...


@dataclass(frozen=True)
class FeedConfig:
    # Where the post's link goes; "{id}" is the MaintainX work-order global id.
    portal_work_order_url: str
    timezone: str = "America/Chicago"
    max_images: int = DEFAULT_MAX_IMAGES
    max_pixels: int = DEFAULT_MAX_PIXELS
    max_image_bytes: int = DEFAULT_MAX_BYTES
    max_attempts: int = 5
    backoff_seconds: tuple[int, ...] = (60, 300, 1800, 3600)
    lease_seconds: int = 300
    batch_size: int = 10
    inbox_retention_days: int = 30
    # Nightly backfill only considers requests created this recently (so enabling a site
    # doesn't flood its channel with history).
    backfill_days: int = 7
    # Finished work orders stop being re-checked nightly after this many quiet days.
    refresh_window_days: int = 14
    # A post record still "creating" after this long is reported as an issue.
    stuck_after_minutes: int = 60
    # While paused (sign-in lost, key rejected), check whether it's fixed this often.
    pause_recheck_seconds: int = 300


@dataclass
class RunReport:
    outcomes: list[str] = field(default_factory=list)
    retried: int = 0
    dead: int = 0
    paused: str | None = None  # why nothing can run until a person fixes it


@dataclass
class ReconcileReport:
    issues: list[str] = field(default_factory=list)
    backfilled: int = 0
    refreshed: int = 0
    pruned: int = 0


class FeedEngine:
    def __init__(
        self,
        *,
        store: FeedStore,
        maintainx: MaintainXApi,
        graph: TeamsApi,
        channels: ChannelResolver,
        clock: Clock,
        config: FeedConfig,
        people: PeopleDirectory | None = None,
        images: ImageProcessor | None = None,
    ):
        self._store = store
        self._mx = maintainx
        self._graph = graph
        self._channels = channels
        self._clock = clock
        self._config = config
        self._people = people
        self._images = images
        self._tz = ZoneInfo(config.timezone)
        self._names: dict[int, str | None] = {}
        self._paused: str | None = None
        self._next_probe: datetime | None = None

    # --- worker loop -------------------------------------------------------------------

    def run_once(self) -> RunReport:
        report = RunReport()
        if self._paused and not self._resume():
            report.paused = self._paused
            return report
        # Claim one event at a time: a claim spends an attempt, so a worker that dies mid-event
        # only ever charges the event it was actually processing.
        for _ in range(self._config.batch_size):
            claimed = self._store.claim(self._clock.now(), 1, self._config.lease_seconds)
            if not claimed:
                break
            queued = claimed[0]
            key = queued.event.key
            if queued.attempts > self._config.max_attempts:
                # Claimed more times than allowed without ever finishing: the worker keeps
                # dying while processing it. Stop the crash loop.
                error = f"exceeded {self._config.max_attempts} attempts (worker may have crashed)"
                self._store.fail(key, error, self._clock.now(), retry_at=None)
                report.dead += 1
                continue
            try:
                outcome = self.process(queued.event)
            except PauseFeed as exc:
                # Nothing works until a person reconnects or fixes a credential; put the event
                # back without spending an attempt and stop claiming until it is fixed.
                self._store.release(key, self._clock.now())
                self._pause(str(exc) or type(exc).__name__)
                report.paused = self._paused
                break
            except PermanentError as exc:
                self._store.fail(key, _describe(exc), self._clock.now(), retry_at=None)
                report.dead += 1
                log.error("maintenance feed event %s dead-lettered: %s", key, exc)
            except TransientError as exc:
                self._retry(queued, exc, report)
            except Exception as exc:  # a bug: retry with backoff, then dead-letter visibly
                log.exception("maintenance feed event %s failed unexpectedly", key)
                self._retry(queued, exc, report)
            else:
                self._store.complete(key, outcome, self._clock.now())
                report.outcomes.append(outcome)
        return report

    def _pause(self, reason: str) -> None:
        self._paused = reason
        self._next_probe = self._clock.now() + timedelta(seconds=self._config.pause_recheck_seconds)
        log.warning("maintenance feed paused: %s", reason)

    def _resume(self) -> bool:
        """While paused, re-check every few minutes (not every run) whether it's fixed."""
        if self._next_probe and self._clock.now() < self._next_probe:
            return False
        try:
            self._graph.refresh_token()
        except PauseFeed as exc:
            self._pause(str(exc) or type(exc).__name__)
            return False
        except Exception as exc:  # token endpoint down: stay paused, try again later
            log.warning("maintenance feed sign-in check failed: %s", type(exc).__name__)
            self._pause(self._paused or "sign-in check failed")
            return False
        # A MaintainX-key pause resumes here too; if the key is still bad, the next event
        # re-pauses for another interval.
        self._paused = None
        self._next_probe = None
        return True

    def _retry(self, queued: QueuedEvent, exc: Exception, report: RunReport) -> None:
        now = self._clock.now()
        if queued.attempts >= self._config.max_attempts:
            self._store.fail(queued.event.key, _describe(exc), now, retry_at=None)
            report.dead += 1
            return
        schedule = self._config.backoff_seconds
        delay = schedule[min(queued.attempts, len(schedule)) - 1]
        requested = getattr(exc, "retry_after", None) or 0
        retry_at = now + timedelta(seconds=max(delay, requested))
        self._store.fail(queued.event.key, _describe(exc), now, retry_at=retry_at)
        report.retried += 1

    # --- one event ---------------------------------------------------------------------

    def process(self, event: MxEvent) -> str:
        kind = event.event_type
        if kind in (events.WORK_REQUEST_STATUS_CHANGE, events.BACKFILL_APPROVED):
            if kind == events.WORK_REQUEST_STATUS_CHANGE and event.new_status != "APPROVED":
                return "ignored:status"
            return self._create_root(event)
        if kind in (
            events.WORK_ORDER_STATUS_CHANGE,
            events.WORK_ORDER_CHANGE,
            events.WORK_ORDER_OVERDUE,
            events.RECONCILE_WORK_ORDER,
        ):
            return self._update(event)
        if kind == events.WORK_ORDER_DELETE:
            return self._delete(event)
        return "ignored:type"

    def _create_root(self, event: MxEvent) -> str:
        if event.request_id is None:
            return "ignored:no request"
        request = self._mx.get_work_request(event.request_id)
        work_order_id = request.get("workOrderId")
        if not work_order_id:
            return "ignored:no work order"
        work_order_id = int(work_order_id)
        record = self._store.get_post(work_order_id)
        if record is not None and record.state != CREATING:
            return "duplicate"

        try:
            work_order = self._mx.get_work_order(work_order_id)
        except NotFound:
            return self._abandon(record, "ignored:work order gone")
        snapshot = WorkOrderSnapshot.from_api(work_order)
        if event.event_type == events.BACKFILL_APPROVED and snapshot.status in TERMINAL_STATUSES:
            # Finished before we ever posted it; don't post history.
            return self._abandon(record, "ignored:closed")
        if snapshot.location_id is None:
            return self._abandon(record, "unrouted:no location")
        target = self._channels.resolve(snapshot.location_id)
        if target is None:
            return self._abandon(record, "unrouted")

        if record is None:
            record = self._claim_post(event, snapshot, target)
            if record is None:
                return "duplicate"
        elif record.post_attempted_at and self._recover_root(record):
            return "recovered"

        record.snapshot = snapshot.to_dict()  # what this post is about to show
        record.requester = self._requester(request)
        when = render.parse_iso(event.occurred_at) if event.occurred_at else self._clock.now()
        record.status_as_of = when.isoformat()
        prepared = prepare_images(
            collect_media(request, work_order),
            self._mx.download,
            self._images,
            max_images=self._config.max_images,
            max_pixels=self._config.max_pixels,
            max_bytes=self._config.max_image_bytes,
        )
        record.extra_photos = prepared.extra
        subject, html = self._render_root(record, snapshot, prepared.placeholder_html)
        # Record the attempt before posting, so a crash or lost response is recoverable.
        record.post_attempted_at = self._clock.now().isoformat()
        self._save(record)
        try:
            posted = self._graph.post_message(
                record.team_id, record.channel_id, subject, html, prepared.images
            )
        except Exception as exc:
            if _definitely_not_written(exc):
                record.post_attempted_at = None
                self._save(record)
            raise
        self._adopt(record, posted)
        log.info("maintenance feed posted work order %s", work_order_id)
        return "posted"

    def _abandon(self, record: PostRecord | None, outcome: str) -> str:
        """Retire a claim that was never posted and no longer should be, so it stops being
        retried and reported. A claim whose post may have gone out is left for recovery."""
        if record is not None and record.state == CREATING and not record.post_attempted_at:
            self._mark(record, ABANDONED, outcome)
        return outcome

    def _claim_post(
        self, event: MxEvent, snapshot: WorkOrderSnapshot, target: ChannelTarget
    ) -> PostRecord | None:
        channel_id = target.channel_id or self._lookup_channel(snapshot.location_id, target)
        now = self._clock.now().isoformat()
        candidate = PostRecord(
            work_order_id=snapshot.id,
            request_id=event.request_id,
            team_id=target.team_id,
            channel_id=channel_id,
            state=CREATING,
            snapshot=snapshot.to_dict(),
            site_names=[n for n in target.site_names if n],
            created_at=now,
            updated_at=now,
        )
        if not self._store.create_post(candidate):
            return None  # someone else claimed it first; their event will post it
        return self._store.get_post(snapshot.id)

    def _lookup_channel(self, location_id: int | None, target: ChannelTarget) -> str:
        channel_id = self._graph.find_channel_id(target.team_id, target.channel_name)
        if not channel_id:
            raise PermanentError(
                f"no channel named '{target.channel_name}' in team {target.team_id}"
            )
        if location_id is not None:
            self._channels.remember_channel_id(location_id, target.team_id, channel_id)
        return channel_id

    def _recover_root(self, record: PostRecord) -> bool:
        """A post was attempted but its result is unknown. Wait out a post that may still be in
        flight (a second worker during a slot swap), then look for it by this work order's own
        link — never by text another post could contain."""
        attempted = render.parse_iso(record.post_attempted_at)
        in_flight_until = attempted + timedelta(seconds=self._config.lease_seconds)
        now = self._clock.now()
        if now < in_flight_until:
            raise TransientError(
                "root post in progress", retry_after=(in_flight_until - now).total_seconds()
            )
        try:
            found = self._graph.find_root_message(
                record.team_id,
                record.channel_id,
                # The body carries the link HTML-escaped ("&" as "&amp;"), so search for that.
                render.esc(self._link(record.work_order_id)),
                created_after=attempted - timedelta(minutes=2),
            )
        except PermanentError as exc:
            # Posting again without checking would risk a duplicate root post.
            raise PermanentError(
                "can't check whether the root post already exists "
                f"(grant ChannelMessage.Read.All): {exc}"
            ) from exc
        if found is None:
            return False
        self._adopt(record, found)
        log.info("maintenance feed recovered root post for work order %s", record.work_order_id)
        return True

    def _adopt(self, record: PostRecord, posted: PostedMessage) -> None:
        record.message_id = posted.id
        record.web_url = posted.web_url
        # Keep only images hosted on this very message.
        own = f"/messages/{posted.id}/hostedContents/"
        record.images_html = " ".join(t for t in image_tags(posted.body_html) if own in t)
        subject, html = self._render_root(record, record.work_order, record.images_html)
        record.root_digest = _digest(subject + html)
        record.post_attempted_at = None
        record.state = ACTIVE
        record.updated_at = self._clock.now().isoformat()
        self._save(record)

    def _update(self, event: MxEvent) -> str:
        record = self._store.get_post(event.work_order_id) if event.work_order_id else None
        if record is None or record.state in (CREATING, ABANDONED):
            return "ignored:no post"
        if record.state in (DELETED, LOST):
            return f"ignored:{record.state}"
        try:
            fresh = WorkOrderSnapshot.from_api(self._mx.get_work_order(record.work_order_id))
        except NotFound:
            # Gone without a delete webhook reaching us (lost delivery): tombstone it now.
            return self._tombstone(record, event)

        plan = planner.plan_update(
            record=record,
            fresh=fresh,
            event=event,
            now=self._clock.now(),
            tz=self._tz,
            name_of=self._user_name,
        )
        done: list[str] = []
        try:
            # Replies first: they are what notifies people, so a refused edit can't block them.
            for reply in plan.replies:
                self._once(
                    record,
                    reply.key,
                    lambda html=reply.html: self._graph.post_reply(
                        record.team_id, record.channel_id, record.message_id, html
                    ),
                )
                done.append(reply.key.split(":", 1)[0])

            # Everything announced is now history: advance the snapshot before the root edit,
            # so a failed edit can never make a later change (say, a revert) look like no change.
            if record.work_order != plan.snapshot or record.applied_actions:
                record.snapshot = plan.snapshot.to_dict()
                record.status_as_of = plan.status_as_of
                record.overdue_notified = plan.overdue_notified
                record.applied_actions = []
                record.updated_at = self._clock.now().isoformat()
                self._save(record)

            # Edit whenever the root doesn't show what it should, including after an earlier
            # edit failed or was refused.
            subject, html = self._render_root(record, plan.snapshot, record.images_html)
            digest = _digest(subject + html)
            if digest != record.root_digest:
                try:
                    self._graph.update_message(
                        record.team_id, record.channel_id, record.message_id, subject, html
                    )
                except NotFound:
                    raise
                except PermanentError as exc:
                    # Keep the replies; the nightly refresh retries the stale root.
                    log.error(
                        "maintenance feed root edit refused for %s: %s", record.work_order_id, exc
                    )
                    done.append("root-failed")
                else:
                    record.root_digest = digest
                    record.updated_at = self._clock.now().isoformat()
                    self._save(record)
                    done.append("root")
        except NotFound:
            return self._mark(record, LOST, "lost")
        return "updated:" + ",".join(done) if done else "unchanged"

    def _delete(self, event: MxEvent) -> str:
        record = self._store.get_post(event.work_order_id) if event.work_order_id else None
        if record is None or record.state in (CREATING, ABANDONED):
            return "ignored:no post"
        if record.state == DELETED:
            return "duplicate"
        if record.state == LOST:
            return "ignored:lost"
        try:
            self._mx.get_work_order(record.work_order_id)
        except NotFound:
            return self._tombstone(record, event)
        return "ignored:still exists"  # don't tombstone a live work order

    def _tombstone(self, record: PostRecord, event: MxEvent) -> str:
        plan = planner.plan_delete()
        when = render.parse_iso(event.occurred_at) if event.occurred_at else self._clock.now()
        record.status_as_of = when.isoformat()
        subject, html = self._render_root(
            record, record.work_order, record.images_html, status_override=plan.status_label
        )
        try:
            self._once(
                record,
                "deleted",
                lambda: self._graph.post_reply(
                    record.team_id, record.channel_id, record.message_id, plan.html
                ),
            )
            self._once(
                record,
                "root:" + _digest(subject + html),
                lambda: self._graph.update_message(
                    record.team_id, record.channel_id, record.message_id, subject, html
                ),
            )
        except NotFound:
            return self._mark(record, LOST, "lost")
        record.root_digest = _digest(subject + html)
        return self._mark(record, DELETED, "deleted")

    def _once(self, record: PostRecord, key: str, write: Callable[[], Any]) -> None:
        """Perform a Graph write unless this change was already sent, then record it durably.

        A write whose response is lost (timeout after Graph accepted it) can still repeat:
        delivery is at least once, not exactly once."""
        if key in record.applied_actions:
            return
        write()
        record.applied_actions.append(key)
        self._save(record)

    def _mark(self, record: PostRecord, state: str, outcome: str) -> str:
        record.state = state
        record.applied_actions = []
        record.updated_at = self._clock.now().isoformat()
        self._save(record)
        return outcome

    # --- nightly -----------------------------------------------------------------------

    def reconcile(self, subscription_ids: Iterable[int] = ()) -> ReconcileReport:
        report = ReconcileReport()
        now = self._clock.now()
        day = now.date().isoformat()
        try:
            self._graph.refresh_token()
        except PauseFeed:
            report.issues.append(
                "Poster account sign-in expired — reconnect it from the portal admin page"
            )
        for subscription_id in subscription_ids:
            if self._mx.get_subscription(subscription_id) is None:
                report.issues.append(f"MaintainX subscription {subscription_id} no longer exists")

        stuck_cutoff = now - timedelta(minutes=self._config.stuck_after_minutes)
        for record in self._store.list_posts([CREATING]):
            if _before(record.created_at, stuck_cutoff):
                report.issues.append(
                    f"Work order {record.work_order_id} has not been posted to Teams "
                    f"(waiting since {record.created_at})"
                )

        # Recent approved (still open) requests with no post: a lost webhook, an outage, a
        # create that gave up, or a site that was just enabled. Keys are per day, so an
        # unrouted site is retried tomorrow.
        since = now - timedelta(days=self._config.backfill_days)
        for request in self._mx.list_work_requests(["APPROVED"]):
            work_order_id = request.get("workOrderId")
            if not work_order_id or not _after(request.get("createdAt"), since):
                continue
            existing = self._store.get_post(int(work_order_id))
            if existing is not None and existing.state != CREATING:
                continue
            backfill = events.synthetic(
                f"backfill:{request['id']}:{day}",
                events.BACKFILL_APPROVED,
                request_id=int(request["id"]),
                work_order_id=int(work_order_id),
            )
            if self._store.enqueue(backfill, now):
                report.backfilled += 1

        quiet_cutoff = now - timedelta(days=self._config.refresh_window_days)
        for record in self._store.list_posts([ACTIVE]):
            finished = record.work_order.status in TERMINAL_STATUSES
            if finished and _before(record.updated_at, quiet_cutoff):
                continue
            refresh = events.synthetic(
                f"reconcile:{record.work_order_id}:{day}",
                events.RECONCILE_WORK_ORDER,
                work_order_id=record.work_order_id,
            )
            if self._store.enqueue(refresh, now):
                report.refreshed += 1

        report.pruned = self._store.prune(now - timedelta(days=self._config.inbox_retention_days))
        return report

    # --- helpers -----------------------------------------------------------------------

    def _render_root(
        self,
        record: PostRecord,
        snapshot: WorkOrderSnapshot,
        images_html: str,
        status_override: str | None = None,
    ) -> tuple[str, str]:
        as_of = render.parse_iso(record.status_as_of) if record.status_as_of else self._clock.now()
        view = render.RootView(
            status_label=status_override or self._status_label(snapshot),
            as_of_text=render.format_local(as_of, self._tz),
            sequential_id=snapshot.sequential_id,
            requester=record.requester,
            description=render.scrub_description(snapshot.description),
            images_html=images_html,
            link_url=self._link(record.work_order_id),
            extra_photos=record.extra_photos,
        )
        site_names = [*record.site_names, snapshot.location_name or ""]
        subject = render.headline(snapshot.title, snapshot.sequential_id, site_names)
        return subject, render.root_html(view)

    def _link(self, work_order_id: int) -> str:
        return self._config.portal_work_order_url.format(id=work_order_id)

    @staticmethod
    def _status_label(snapshot: WorkOrderSnapshot) -> str:
        return render.status_label(snapshot.status, snapshot.sub_status)

    def _requester(self, request: dict[str, Any]) -> str | None:
        # requesterId is the shared API account; the person is the request's contact email.
        contact = request.get("creatorContactInfo") or {}
        email = contact.get("value") if contact.get("type", "EMAIL") == "EMAIL" else None
        if not email:
            return None
        name = self._people.name_for_email(email) if self._people else None
        return name or email

    def _user_name(self, user_id: int) -> str | None:
        if user_id not in self._names:
            try:
                user = self._mx.get_user(user_id)
            except NotFound:
                self._names[user_id] = None
            else:
                full = " ".join(p for p in (user.get("firstName"), user.get("lastName")) if p)
                self._names[user_id] = full.strip() or None
        return self._names[user_id]

    def _save(self, record: PostRecord) -> None:
        expected = record.version
        if not self._store.save_post(record, expected_version=expected):
            raise ConflictError(f"post record for work order {record.work_order_id} changed")
        record.version = expected + 1


def _definitely_not_written(exc: Exception) -> bool:
    """True when Graph answered and refused (4xx, throttled) or no request was sent at all.
    Timeouts, connection drops and 5xx leave the outcome unknown."""
    if isinstance(exc, (PermanentError, PauseFeed)):
        return True
    return isinstance(exc, TransientError) and exc.status is not None and exc.status < 500


def _describe(exc: Exception) -> str:
    if isinstance(exc, (TransientError, PermanentError, PauseFeed)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _before(iso: str | None, cutoff: datetime) -> bool:
    return bool(iso) and render.parse_iso(iso) < cutoff


def _after(iso: str | None, cutoff: datetime) -> bool:
    return bool(iso) and render.parse_iso(iso) >= cutoff
