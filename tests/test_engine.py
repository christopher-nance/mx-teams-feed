import copy
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from mx_teams_feed import events
from mx_teams_feed.engine import FeedConfig, FeedEngine
from mx_teams_feed.errors import PermanentError, TransientError
from mx_teams_feed.events import MxEvent
from mx_teams_feed.models import ACTIVE, CREATING, DEAD, DELETED, DONE, LOST, PENDING, ChannelTarget
from mx_teams_feed.testing import (
    DictDirectory,
    FakeGraph,
    FakeMaintainX,
    FixedClock,
    MemoryStore,
    StaticChannels,
)

NOW = datetime(2026, 10, 30, 19, 14, tzinfo=UTC)
WO_ID = 700701
WR_ID = 800801
SPRINGFIELD = 100101
S3 = "https://maintainx-uploads-production.s3.us-west-2.amazonaws.com"

WORK_REQUEST = {
    "id": WR_ID,
    "requestStatus": "APPROVED",
    "createdAt": "2026-10-30T19:00:00.000Z",
    "workOrderId": WO_ID,
    "creatorContactInfo": {"type": "EMAIL", "value": "jane.doe@example.com"},
    "thumbnail": {"url": f"{S3}/a.jpg?sig=1", "mimeType": "image/jpeg"},
    "attachments": [{"url": f"{S3}/b.jpg?sig=2", "mimeType": "image/jpeg"}],
}
WORK_ORDER = {
    "id": WO_ID,
    "sequentialId": 5052,
    "title": "Springfield - 2026-10-29 @ 2:35 PM CDT - Vacuum 3 hose split",
    "description": "Hose split\n\nRequester contact information:\njane.doe@example.com",
    "status": "OPEN",
    "priority": "LOW",
    "dueDate": None,
    "assigneeIds": [],
    "locationId": SPRINGFIELD,
    "location": {"id": SPRINGFIELD, "name": "Springfield"},
}
PORTAL = "https://portal.example.com/utrack/work-orders?wo={id}"


class Harness:
    def __init__(self, channel_id="19:springfield", images=None, **config):
        self.clock = FixedClock(NOW)
        self.store = MemoryStore()
        self.mx = FakeMaintainX()
        self.mx.work_requests[WR_ID] = copy.deepcopy(WORK_REQUEST)
        self.mx.work_orders[WO_ID] = copy.deepcopy(WORK_ORDER)
        self.mx.users[500501] = {"id": 500501, "firstName": "P.", "lastName": "Quinn"}
        self.graph = FakeGraph(self.clock)
        self.channels = StaticChannels(
            {SPRINGFIELD: ChannelTarget("team-spring", channel_id, site_names=("Springfield",))}
        )
        self.engine = FeedEngine(
            store=self.store,
            maintainx=self.mx,
            graph=self.graph,
            channels=self.channels,
            clock=self.clock,
            config=FeedConfig(**{"portal_work_order_url": PORTAL, **config}),
            people=DictDirectory({"jane.doe@example.com": "Jane Doe"}),
            images=images,
        )
        self._keys = itertools.count(1)

    def deliver(self, event_type, **fields):
        event = MxEvent(key=f"evt-{next(self._keys)}", event_type=event_type, **fields)
        assert self.store.enqueue(event, self.clock.now())
        return event.key

    def approve(self):
        return self.deliver(
            events.WORK_REQUEST_STATUS_CHANGE,
            request_id=WR_ID,
            old_status="PENDING",
            new_status="APPROVED",
            occurred_at="2026-10-30T19:10:00Z",
        )

    def status(self, new, old="OPEN", user_id=500501, **extra):
        self.mx.work_orders[WO_ID]["status"] = new
        return self.deliver(
            events.WORK_ORDER_STATUS_CHANGE,
            work_order_id=WO_ID,
            old_status=old,
            new_status=new,
            user_id=user_id,
            occurred_at="2026-10-30T20:00:00Z",
            **extra,
        )

    def run(self):
        return self.engine.run_once()

    def post(self):
        return self.store.get_post(WO_ID)

    def root(self):
        return self.graph.messages[self.post().message_id]


@pytest.fixture
def h():
    return Harness()


class TestCreateRoot:
    def test_approval_posts_one_root_with_subject_and_body(self, h):
        h.approve()
        report = h.run()
        assert report.outcomes == ["posted"]
        assert len(h.graph.posts) == 1
        root = h.root()
        assert root["team"] == "team-spring" and root["channel"] == "19:springfield"
        assert root["subject"] == "Vacuum 3 hose split"
        assert "<b>Status:</b> Open <i>(as of Oct 30, 2026 2:10 PM CDT)</i>" in root["html"]
        assert "WO #5052 · Requested by Jane Doe" in root["html"]
        assert "Requester contact information" not in root["html"]
        assert f'href="https://portal.example.com/utrack/work-orders?wo={WO_ID}"' in root["html"]

    def test_photos_are_sent_inline_and_their_hosted_urls_kept(self, h):
        h.approve()
        h.run()
        assert [i.temp_id for i in h.graph.posts[0]["images"]] == ["1", "2"]
        record = h.post()
        assert record.state == ACTIVE
        assert record.images_html.count("<img") == 2
        assert "https://graph.microsoft.com/" in record.images_html

    def test_second_approval_for_the_same_work_order_does_not_repost(self, h):
        h.approve()
        h.run()
        h.approve()
        assert h.run().outcomes == ["duplicate"]
        assert len(h.graph.posts) == 1

    def test_non_approval_status_is_ignored(self, h):
        h.deliver(events.WORK_REQUEST_STATUS_CHANGE, request_id=WR_ID, new_status="DONE")
        assert h.run().outcomes == ["ignored:status"]
        assert h.graph.posts == []

    def test_site_not_enabled_is_recorded_but_not_posted(self, h):
        h.mx.work_orders[WO_ID]["locationId"] = 100102
        h.approve()
        assert h.run().outcomes == ["unrouted"]
        assert h.graph.posts == [] and h.post() is None

    def test_work_order_without_location_is_unrouted(self, h):
        h.mx.work_orders[WO_ID]["locationId"] = None
        h.approve()
        assert h.run().outcomes == ["unrouted:no location"]

    def test_requester_falls_back_to_email(self, h):
        h.mx.work_requests[WR_ID]["creatorContactInfo"]["value"] = "new.hire@example.com"
        h.approve()
        h.run()
        assert "Requested by new.hire@example.com" in h.root()["html"]

    def test_channel_is_looked_up_by_name_and_remembered(self):
        h = Harness(channel_id=None)
        h.graph.channels["team-spring"] = {"maintenance": "19:found"}
        h.approve()
        h.run()
        assert h.root()["channel"] == "19:found"
        assert h.channels.remembered == [(SPRINGFIELD, "team-spring", "19:found")]

    def test_missing_channel_dead_letters_the_event(self):
        h = Harness(channel_id=None)
        key = h.approve()
        report = h.run()
        assert report.dead == 1
        assert h.store.get_event(key).state == DEAD
        assert "Maintenance" in h.store.get_event(key).last_error

    def test_root_that_posted_but_was_not_saved_is_recovered_not_duplicated(self, h):
        h.approve()
        # The post succeeds; saving its message id does not.
        h.store.fail_next_save_matching(lambda record: record.message_id is not None)
        h.run()
        assert h.post().state == CREATING
        h.clock.advance(minutes=5)
        h.run()
        assert len(h.graph.posts) == 1
        assert h.post().state == ACTIVE
        assert h.post().message_id == h.graph.posts[0]["id"]


class TestUpdates:
    @pytest.fixture
    def posted(self, h):
        h.approve()
        h.run()
        return h

    def test_status_change_edits_root_and_replies(self, posted):
        h = posted
        h.status("IN_PROGRESS")
        assert h.run().outcomes == ["updated:status,root"]
        root = h.root()
        assert "<b>Status:</b> In Progress <i>(as of Oct 30, 2026 3:00 PM CDT)</i>" in root["html"]
        assert root["images"] == h.post().images_html  # photos re-sent unchanged
        assert [r["html"] for r in root["replies"]] == [
            "<p><b>Status:</b> Open → In Progress — by P. Quinn</p>"
        ]
        assert h.post().work_order.status == "IN_PROGRESS"

    def test_duplicate_status_event_is_silent(self, posted):
        h = posted
        h.status("IN_PROGRESS")
        h.run()
        h.status("IN_PROGRESS")
        assert h.run().outcomes == ["unchanged"]
        assert len(h.root()["replies"]) == 1

    def test_retry_after_a_failed_write_repeats_nothing_already_sent(self, posted):
        h = posted
        h.mx.work_orders[WO_ID]["priority"] = "HIGH"
        key = h.status("DONE")
        h.graph.fail_next("edit", TransientError("429", status=429, retry_after=5))
        h.run()
        assert h.store.get_event(key).state == PENDING
        assert len(h.root()["replies"]) == 2 and h.root()["edits"] == []
        h.clock.advance(minutes=2)
        h.run()
        assert h.store.get_event(key).state == DONE
        assert len(h.root()["edits"]) == 1
        assert len(h.root()["replies"]) == 2
        h.root()["replies"].pop()  # priority; the status reply is checked below
        assert [r["html"] for r in h.root()["replies"]] == [
            "<p><b>Done</b> — completed by P. Quinn</p>"
        ]

    def test_title_change_edits_subject_without_reply(self, posted):
        h = posted
        h.mx.work_orders[WO_ID]["title"] = "Vacuum 3 hose split, now leaking"
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=WO_ID)
        h.run()
        assert h.root()["subject"] == "Vacuum 3 hose split, now leaking"
        assert h.root()["replies"] == []

    def test_assignment_reply_uses_maintainx_names_cached(self, posted):
        h = posted
        h.mx.work_orders[WO_ID]["assigneeIds"] = [500501]
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=WO_ID)
        h.run()
        h.status("IN_PROGRESS")
        h.run()
        assert [r["html"] for r in h.root()["replies"]] == [
            "<p>Assigned to P. Quinn</p>",
            "<p><b>Status:</b> Open → In Progress — by P. Quinn</p>",
        ]
        assert h.mx.calls.count(("get_user", 500501)) == 1

    def test_event_for_a_work_order_without_a_post_is_ignored(self, h):
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=999)
        assert h.run().outcomes == ["ignored:no post"]

    def test_root_removed_from_teams_marks_the_post_lost(self, posted):
        h = posted
        h.graph.messages[h.post().message_id]["deleted"] = True
        h.status("IN_PROGRESS")
        assert h.run().outcomes == ["lost"]
        assert h.post().state == LOST
        h.status("DONE", old="IN_PROGRESS")
        assert h.run().outcomes == ["ignored:lost"]

    def test_work_order_missing_in_maintainx_is_tombstoned(self, posted):
        # Its delete webhook was lost or hasn't arrived; the post says so rather than lingering.
        h = posted
        del h.mx.work_orders[WO_ID]
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=WO_ID)
        assert h.run().outcomes == ["deleted"]


class TestDelete:
    def test_delete_marks_root_and_replies_once(self, h):
        h.approve()
        h.run()
        del h.mx.work_orders[WO_ID]
        h.deliver(events.WORK_ORDER_DELETE, work_order_id=WO_ID)
        assert h.run().outcomes == ["deleted"]
        root = h.root()
        assert "<b>Status:</b> Deleted in MaintainX" in root["html"]
        assert [r["html"] for r in root["replies"]] == [
            "<p>This work order was <b>deleted in MaintainX</b>.</p>"
        ]
        assert h.post().state == DELETED
        h.deliver(events.WORK_ORDER_DELETE, work_order_id=WO_ID)
        h.deliver(events.WORK_ORDER_STATUS_CHANGE, work_order_id=WO_ID, new_status="OPEN")
        assert h.run().outcomes == ["duplicate", "ignored:deleted"]

    def test_delete_for_a_work_order_that_still_exists_is_ignored(self, h):
        # A forged or confused delete must not tombstone a live post.
        h.approve()
        h.run()
        h.deliver(events.WORK_ORDER_DELETE, work_order_id=WO_ID)
        assert h.run().outcomes == ["ignored:still exists"]
        assert h.post().state == ACTIVE


class TestRunOnce:
    def test_reconnect_required_pauses_without_spending_attempts(self, h):
        first, second = h.approve(), h.approve()
        h.graph.reconnect = True
        report = h.run()
        assert report.paused
        assert report.outcomes == []
        for key in (first, second):
            queued = h.store.get_event(key)
            assert (queued.state, queued.attempts) == (PENDING, 0)

    def test_transient_failures_back_off(self, h):
        key = h.approve()
        h.graph.fail_next("post", TransientError("503"))
        h.run()
        queued = h.store.get_event(key)
        assert queued.state == PENDING
        assert queued.next_attempt_at == NOW + timedelta(seconds=60)

    def test_server_requested_delay_wins_when_longer(self, h):
        key = h.approve()
        h.graph.fail_next("post", TransientError("429", retry_after=600))
        h.run()
        assert h.store.get_event(key).next_attempt_at == NOW + timedelta(seconds=600)

    def test_transient_failures_dead_letter_after_max_attempts(self):
        h = Harness(max_attempts=2)
        key = h.approve()
        for _ in range(2):
            h.graph.fail_next("post", TransientError("503"))
            h.run()
            h.clock.advance(hours=2)
        assert h.store.get_event(key).state == DEAD

    def test_permanent_failure_dead_letters_immediately(self, h):
        key = h.approve()
        h.graph.fail_next("post", PermanentError("403 not a team member", status=403))
        assert h.run().dead == 1
        assert h.store.get_event(key).state == DEAD

    def test_unexpected_errors_are_retried_not_swallowed(self, h):
        key = h.approve()
        h.graph.fail_next("post", RuntimeError("bug"))
        report = h.run()
        assert report.retried == 1
        assert h.store.get_event(key).last_error == "RuntimeError: bug"


class TestReconcile:
    def test_backfills_approved_requests_without_posts(self, h):
        report = h.engine.reconcile()
        assert report.backfilled == 1
        assert h.run().outcomes == ["posted"]

    def test_backfill_is_once_per_day(self, h):
        h.engine.reconcile()
        assert h.engine.reconcile().backfilled == 0

    def test_refreshes_open_posts_to_catch_missed_events(self, h):
        h.approve()
        h.run()
        h.mx.work_orders[WO_ID]["status"] = "ON_HOLD"
        report = h.engine.reconcile()
        assert report.refreshed == 1
        h.run()
        assert "<b>Status:</b> On Hold" in h.root()["html"]

    def test_reports_missing_subscriptions(self, h):
        h.mx.subscriptions[1] = {"id": 1}
        report = h.engine.reconcile(subscription_ids=[1, 2])
        assert report.issues == ["MaintainX subscription 2 no longer exists"]

    def test_reports_poster_reconnect(self, h):
        h.graph.reconnect = True
        report = h.engine.reconcile()
        assert any("reconnect" in issue.lower() for issue in report.issues)

    def test_keeps_the_poster_token_warm(self, h):
        h.engine.reconcile()
        assert h.graph.token_refreshes == 1

    def test_prunes_old_finished_events(self, h):
        h.approve()
        h.run()
        h.clock.advance(days=31)
        assert h.engine.reconcile().pruned == 1


class TestRootRecovery:
    def _foreign_root_mentioning_5052(self, h):
        h.graph.messages["foreign"] = {
            "team": "team-spring",
            "channel": "19:springfield",
            "subject": "Other",
            "html": "<p>WO #5060</p><p>Same hose as WO #5052, split again</p>",
            "images": "",
            "edits": [],
            "replies": [],
            "deleted": False,
            "created": NOW,
        }

    def test_definite_post_failure_retries_without_searching(self, h):
        self._foreign_root_mentioning_5052(h)
        h.approve()
        h.graph.fail_next("post", TransientError("429", status=429, retry_after=1))
        h.run()
        assert h.post().post_attempted_at is None
        h.clock.advance(minutes=2)
        h.run()
        assert h.post().message_id != "foreign"
        assert h.graph.searches == 0
        assert len(h.graph.posts) == 1

    def test_uncertain_post_failure_recovers_by_this_work_orders_link(self, h):
        self._foreign_root_mentioning_5052(h)
        h.approve()
        h.graph.fail_next("post", TransientError("timed out"), after_commit=True)
        h.run()
        h.clock.advance(minutes=6)
        assert h.run().outcomes == ["recovered"]
        assert len(h.graph.posts) == 1
        assert h.post().message_id == h.graph.posts[0]["id"]

    def test_recovery_never_adopts_a_post_for_another_work_order(self, h):
        self._foreign_root_mentioning_5052(h)
        h.approve()
        h.graph.fail_next("post", TransientError("timed out"))  # nothing was posted
        h.run()
        h.clock.advance(minutes=6)
        h.run()
        assert h.post().message_id != "foreign"
        assert len(h.graph.posts) == 1

    def test_a_post_in_flight_elsewhere_is_not_duplicated(self, h):
        # A second worker (slot swap) sees CREATING with a fresh post attempt: wait, don't post.
        key = h.approve()
        h.graph.fail_next("post", TransientError("timed out"))
        h.run()
        h.store.events[key].next_attempt_at = NOW
        h.clock.advance(seconds=30)
        h.run()
        assert h.graph.posts == []
        assert "in progress" in h.store.get_event(key).last_error

    def test_adopted_images_are_only_this_messages_hosted_contents(self, h):
        h.approve()
        h.graph.fail_next("post", TransientError("timed out"), after_commit=True)
        h.run()
        message = h.graph.messages[h.graph.posts[0]["id"]]
        message["html"] += '<img src="https://tracker.example/pixel.gif">'
        h.clock.advance(minutes=6)
        h.run()
        assert "tracker.example" not in h.post().images_html
        assert h.post().images_html.count("<img") == 2


class TestPhotoFailures:
    def test_a_missing_photo_does_not_block_the_post(self, h):
        h.mx.missing_files.add(WORK_REQUEST["thumbnail"]["url"])
        h.approve()
        assert h.run().outcomes == ["posted"]
        assert len(h.graph.posts[0]["images"]) == 1
        assert h.post().extra_photos == 1

    def test_an_image_the_processor_chokes_on_does_not_block_the_post(self):
        class Exploding:
            def fit(self, data, mime_type, max_pixels, max_bytes):
                raise ValueError("decompression bomb")

        h = Harness(images=Exploding())
        h.approve()
        assert h.run().outcomes == ["posted"]
        assert h.post().extra_photos == 2


class TestRootEditFailures:
    def test_replies_still_go_out_when_the_root_edit_is_refused(self, h):
        h.approve()
        h.run()
        key = h.status("IN_PROGRESS")
        h.graph.fail_next("edit", PermanentError("400 hosted content", status=400))
        assert h.run().outcomes == ["updated:status,root-failed"]
        assert h.store.get_event(key).state == DONE
        assert len(h.root()["replies"]) == 1
        assert "<b>Status:</b> Open" in h.root()["html"]

    def test_a_root_showing_the_wrong_status_is_corrected_later(self, h):
        h.approve()
        h.run()
        h.status("IN_PROGRESS")
        h.graph.fail_next("edit", PermanentError("400", status=400))
        h.run()
        h.engine.reconcile()
        h.run()
        assert "<b>Status:</b> In Progress" in h.root()["html"]
        assert len(h.root()["replies"]) == 1

    def test_revert_during_backoff_leaves_the_root_and_bookkeeping_consistent(self, h):
        h.approve()
        h.run()
        key = h.status("IN_PROGRESS")
        h.graph.fail_next("reply", TransientError("429", status=429, retry_after=1))
        h.run()
        h.mx.work_orders[WO_ID]["status"] = "OPEN"
        h.clock.advance(minutes=2)
        h.run()
        assert h.store.get_event(key).state == DONE
        assert "<b>Status:</b> Open" in h.root()["html"]
        assert h.post().applied_actions == []


class TestPausing:
    def test_rejected_maintainx_key_pauses_instead_of_dead_lettering(self, h):
        key = h.approve()
        h.mx.reject_credentials = True
        report = h.run()
        assert report.paused and report.dead == 0
        assert h.store.get_event(key).state == PENDING

    def test_while_paused_nothing_is_claimed_until_the_poster_is_back(self, h):
        h.approve()
        h.graph.reconnect = True
        h.run()
        calls = len(h.mx.calls)
        assert h.run().paused
        assert len(h.mx.calls) == calls
        h.graph.reconnect = False
        h.clock.advance(minutes=5)
        assert h.run().outcomes == ["posted"]


class TestCrashLoops:
    def test_an_event_that_keeps_killing_the_worker_is_dead_lettered(self):
        h = Harness(max_attempts=2)
        key = h.approve()
        for _ in range(2):  # two claims whose worker died before finishing
            h.store.claim(h.clock.now(), limit=10, lease_seconds=1)
            h.clock.advance(seconds=2)
        assert h.run().dead == 1
        assert "attempts" in h.store.get_event(key).last_error


class TestBackfillScope:
    def test_old_approved_requests_are_not_backfilled(self, h):
        h.mx.work_requests[WR_ID]["createdAt"] = "2026-09-01T00:00:00.000Z"
        assert h.engine.reconcile().backfilled == 0

    def test_backfill_of_a_finished_work_order_is_skipped(self, h):
        h.mx.work_orders[WO_ID]["status"] = "DONE"
        h.engine.reconcile()
        assert h.run().outcomes == ["ignored:closed"]
        assert h.graph.posts == []

    def test_live_approval_is_never_skipped_for_being_closed(self, h):
        h.mx.work_orders[WO_ID]["status"] = "DONE"
        h.approve()
        assert h.run().outcomes == ["posted"]

    def test_a_create_that_died_is_retried_and_reported(self):
        h = Harness(max_attempts=1)
        h.approve()
        h.graph.fail_next("post", PermanentError("403 not an owner", status=403))
        h.run()
        assert h.post().state == CREATING
        h.clock.advance(hours=2)
        report = h.engine.reconcile()
        assert any(str(WO_ID) in issue for issue in report.issues)
        assert report.backfilled == 1
        assert h.run().outcomes == ["posted"]

    def test_backfill_of_a_deleted_work_order_is_ignored_not_dead(self, h):
        del h.mx.work_orders[WO_ID]
        h.engine.reconcile()
        assert h.run().outcomes == ["ignored:work order gone"]


class NullDroppingStore(MemoryStore):
    """Behaves like Firebase RTDB: null values and empty containers vanish on write."""

    @staticmethod
    def _drop(value):
        if isinstance(value, dict):
            cleaned = {k: NullDroppingStore._drop(v) for k, v in value.items()}
            return {k: v for k, v in cleaned.items() if v not in (None, [], {})}
        return value

    def _reload(self, work_order_id):
        from mx_teams_feed.models import PostRecord

        stored = self.posts[work_order_id]
        self.posts[work_order_id] = PostRecord.from_dict(self._drop(stored.to_dict()))

    def create_post(self, record):
        created = super().create_post(record)
        if created:
            self._reload(record.work_order_id)
        return created

    def save_post(self, record, expected_version):
        saved = super().save_post(record, expected_version)
        if saved:
            self._reload(record.work_order_id)
        return saved


class TestRoundTwo:
    def test_recovery_finds_its_post_when_the_link_has_an_ampersand(self):
        h = Harness(portal_work_order_url="https://portal.example.com/utrack?view=wo&id={id}")
        h.approve()
        h.graph.fail_next("post", TransientError("timed out"), after_commit=True)
        h.run()
        h.clock.advance(minutes=6)
        assert h.run().outcomes == ["recovered"]
        assert len(h.graph.posts) == 1

    def test_revert_after_an_interrupted_update_is_announced(self, h):
        h.approve()
        h.run()
        h.status("IN_PROGRESS")
        h.graph.fail_next("edit", TransientError("503", status=503))
        h.run()
        h.status("OPEN", old="IN_PROGRESS")
        h.clock.advance(minutes=2)
        h.run()
        replies = [r["html"] for r in h.root()["replies"]]
        assert replies[-1].startswith("<p><b>Status:</b> In Progress → Open")
        assert "<b>Status:</b> Open" in h.root()["html"]

    def test_a_failing_token_check_while_paused_does_not_crash_the_worker(self, h):
        h.approve()
        h.graph.reconnect = True
        h.run()
        h.graph.reconnect = False
        h.graph.fail_next(None, TransientError("token endpoint 503"))
        h.clock.advance(minutes=5)
        assert h.run().paused

    def test_paused_feed_rechecks_at_most_every_five_minutes(self, h):
        h.approve()
        h.mx.reject_credentials = True
        h.run()
        calls = len(h.mx.calls)
        for _ in range(3):
            h.clock.advance(seconds=15)
            assert h.run().paused
        assert len(h.mx.calls) == calls
        assert h.graph.token_refreshes == 0

    def test_unchanged_refresh_does_not_rewrite_records_in_a_null_dropping_store(self):
        h = Harness()
        h.store = NullDroppingStore()
        h.engine._store = h.store
        h.approve()
        h.run()
        version = h.post().version
        h.engine.reconcile()
        assert h.run().outcomes == ["unchanged"]
        assert h.post().version == version

    def test_a_late_first_post_records_what_it_showed(self):
        h = Harness(max_attempts=1)
        h.approve()
        h.graph.fail_next("post", PermanentError("403 not an owner", status=403))
        h.run()
        h.mx.work_orders[WO_ID].update(status="IN_PROGRESS", assigneeIds=[500501])
        h.clock.advance(hours=2)
        h.engine.reconcile()
        assert h.run().outcomes == ["posted"]
        h.mx.work_orders[WO_ID]["priority"] = "HIGH"
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=WO_ID)
        h.run()
        assert [r["html"] for r in h.root()["replies"]] == ["<p><b>Priority:</b> Low → High</p>"]

    def test_a_work_order_deleted_without_a_webhook_is_tombstoned(self, h):
        h.approve()
        h.run()
        del h.mx.work_orders[WO_ID]
        h.engine.reconcile()
        assert h.run().outcomes == ["deleted"]
        assert h.post().state == DELETED
        assert "Deleted in MaintainX" in h.root()["html"]

    def test_an_unpostable_claim_is_abandoned_not_reported_forever(self):
        h = Harness(max_attempts=1)
        h.approve()
        h.graph.fail_next("post", PermanentError("403", status=403))
        h.run()
        h.mx.work_orders[WO_ID]["status"] = "DONE"
        h.clock.advance(hours=2)
        h.engine.reconcile()
        assert h.run().outcomes == ["ignored:closed"]
        h.clock.advance(days=1)
        assert h.engine.reconcile().issues == []

    def test_a_refused_title_edit_is_retried_later(self, h):
        h.approve()
        h.run()
        h.mx.work_orders[WO_ID]["title"] = "Vacuum 3 hose replaced"
        h.deliver(events.WORK_ORDER_CHANGE, work_order_id=WO_ID)
        h.graph.fail_next("edit", PermanentError("403", status=403))
        h.run()
        h.engine.reconcile()
        h.run()
        assert h.root()["subject"] == "Vacuum 3 hose replaced"

    def test_a_crash_loop_only_condemns_the_event_that_crashes(self):
        h = Harness(max_attempts=2)
        poison = h.approve()
        h.clock.advance(seconds=1)
        healthy = h.deliver(events.WORK_ORDER_CHANGE, work_order_id=999)
        for _ in range(2):  # the worker dies twice while processing the oldest event
            h.store.claim(h.clock.now(), limit=1, lease_seconds=1)
            h.clock.advance(seconds=2)
        report = h.run()
        assert h.store.get_event(poison).state == DEAD
        assert h.store.get_event(healthy).state == DONE
        assert report.outcomes == ["ignored:no post"]
