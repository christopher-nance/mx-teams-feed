"""Behavior every FeedStore must have. Host apps subclass `FeedStoreContract` in their own test
suite and implement `make_store()` to prove their RTDB/Firestore adapter is correct:

    class TestRtdbStore(FeedStoreContract):
        def make_store(self):
            return RtdbFeedStore(emulator_ref)

Uses plain asserts so it runs under pytest or unittest-style collectors.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .events import MxEvent
from .models import ACTIVE, CREATING, DEAD, DONE, LEASED, PENDING, PostRecord, WorkOrderSnapshot

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def _event(key: str, work_order_id: int = 1) -> MxEvent:
    return MxEvent(key=key, event_type="WORK_ORDER_CHANGE", work_order_id=work_order_id)


def _post(work_order_id: int = 1, state: str = ACTIVE) -> PostRecord:
    return PostRecord(
        work_order_id=work_order_id,
        request_id=None,
        team_id="team",
        channel_id="channel",
        state=state,
        snapshot={"id": work_order_id},
    )


class FeedStoreContract:
    def make_store(self):  # pragma: no cover - supplied by subclasses
        raise NotImplementedError

    # --- inbox -------------------------------------------------------------------------

    def test_enqueue_rejects_duplicate_keys(self):
        store = self.make_store()
        assert store.enqueue(_event("a"), T0)
        assert not store.enqueue(_event("a"), T0)

    def test_claim_leases_due_events_oldest_first(self):
        store = self.make_store()
        store.enqueue(_event("late"), T0 + timedelta(seconds=2))
        store.enqueue(_event("early"), T0)
        claimed = store.claim(T0 + timedelta(seconds=5), limit=10, lease_seconds=60)
        assert [q.event.key for q in claimed] == ["early", "late"]
        assert all(q.state == LEASED and q.attempts == 1 for q in claimed)

    def test_claim_respects_limit(self):
        store = self.make_store()
        for key in "abc":
            store.enqueue(_event(key), T0)
        assert len(store.claim(T0, limit=2, lease_seconds=60)) == 2

    def test_leased_events_are_not_claimed_twice_until_the_lease_expires(self):
        store = self.make_store()
        store.enqueue(_event("a"), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        assert store.claim(T0 + timedelta(seconds=30), limit=10, lease_seconds=60) == []
        again = store.claim(T0 + timedelta(seconds=61), limit=10, lease_seconds=60)
        assert [q.event.key for q in again] == ["a"]
        assert again[0].attempts == 2

    def test_completed_events_are_not_claimed_again(self):
        store = self.make_store()
        store.enqueue(_event("a"), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        store.complete("a", "posted", T0)
        assert store.get_event("a").state == DONE
        assert store.get_event("a").outcome == "posted"
        assert store.claim(T0 + timedelta(hours=1), limit=10, lease_seconds=60) == []

    def test_failed_events_retry_at_the_given_time(self):
        store = self.make_store()
        store.enqueue(_event("a"), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        store.fail("a", "429 throttled", T0, retry_at=T0 + timedelta(minutes=5))
        assert store.get_event("a").state == PENDING
        assert store.get_event("a").last_error == "429 throttled"
        assert store.claim(T0 + timedelta(minutes=4), limit=10, lease_seconds=60) == []
        assert len(store.claim(T0 + timedelta(minutes=5), limit=10, lease_seconds=60)) == 1

    def test_failure_without_retry_time_dead_letters(self):
        store = self.make_store()
        store.enqueue(_event("a"), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        store.fail("a", "403 forbidden", T0, retry_at=None)
        assert store.get_event("a").state == DEAD
        assert store.claim(T0 + timedelta(days=1), limit=10, lease_seconds=60) == []

    def test_release_returns_an_event_without_spending_an_attempt(self):
        store = self.make_store()
        store.enqueue(_event("a"), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        store.release("a", T0)
        again = store.claim(T0, limit=10, lease_seconds=60)
        assert again[0].attempts == 1

    def test_prune_removes_only_old_finished_events(self):
        store = self.make_store()
        for key in ("done", "dead", "pending", "recent"):
            store.enqueue(_event(key), T0)
        store.claim(T0, limit=10, lease_seconds=60)
        store.complete("done", "ok", T0)
        store.fail("dead", "x", T0, retry_at=None)
        store.release("pending", T0)
        store.complete("recent", "ok", T0 + timedelta(days=40))
        assert store.prune(T0 + timedelta(days=30)) == 2
        assert store.get_event("done") is None and store.get_event("dead") is None
        assert store.get_event("pending") is not None and store.get_event("recent") is not None

    # --- posts -------------------------------------------------------------------------

    def test_create_post_only_once_per_work_order(self):
        store = self.make_store()
        assert store.create_post(_post(1, CREATING))
        assert not store.create_post(_post(1, CREATING))
        assert store.get_post(1).state == CREATING

    def test_save_post_is_compare_and_set_on_version(self):
        store = self.make_store()
        store.create_post(_post(1))
        record = store.get_post(1)
        record.message_id = "m1"
        assert store.save_post(record, expected_version=record.version)
        stored = store.get_post(1)
        assert (stored.message_id, stored.version) == ("m1", record.version + 1)
        record.message_id = "stale"
        assert not store.save_post(record, expected_version=record.version)
        assert store.get_post(1).message_id == "m1"

    def test_get_post_returns_a_copy(self):
        store = self.make_store()
        store.create_post(_post(1))
        store.get_post(1).message_id = "mutated"
        assert store.get_post(1).message_id is None

    def test_missing_post_is_none(self):
        assert self.make_store().get_post(404) is None

    def test_realistic_record_round_trips(self):
        # None fields, empty lists and a nested snapshot: stores that drop nulls (RTDB) or
        # coerce types must still give back an equal record.
        snapshot = WorkOrderSnapshot(
            id=7,
            sequential_id=None,
            title="Leak",
            description="",
            status="DONE",
            priority="NONE",
            due_date=None,
            assignee_ids=(),
            location_id=1,
            location_name=None,
            sub_status="CANCELED",
        )
        record = _post(7, CREATING)
        record.snapshot = snapshot.to_dict()
        record.site_names = ["Springfield"]
        record.applied_actions = []
        store = self.make_store()
        store.create_post(record)
        loaded = store.get_post(7)
        assert loaded.work_order == snapshot
        assert loaded.to_dict() == record.to_dict()

    def test_list_posts_filters_by_state(self):
        store = self.make_store()
        store.create_post(_post(1, ACTIVE))
        store.create_post(_post(2, CREATING))
        assert [p.work_order_id for p in store.list_posts([ACTIVE])] == [1]
