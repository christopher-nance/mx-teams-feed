from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mx_teams_feed import events, planner
from mx_teams_feed.events import MxEvent
from mx_teams_feed.models import ACTIVE, PostRecord, WorkOrderSnapshot

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 10, 30, 19, 14, tzinfo=UTC)
NAMES = {500501: "P. Quinn", 22: "Alex Kim", 33: "Sam Lee"}

BASE = WorkOrderSnapshot(
    id=700701,
    sequential_id=5052,
    title="Springfield - 2026-10-29 @ 2:35 PM CDT - Vacuum 3 hose split",
    description="Hose split",
    status="OPEN",
    priority="LOW",
    due_date="2026-11-03T03:00:00.000Z",
    assignee_ids=(22,),
    location_id=100101,
    location_name="Springfield",
)


def _record(snapshot=BASE, **overrides):
    record = PostRecord(
        work_order_id=snapshot.id,
        request_id=800801,
        team_id="team",
        channel_id="chan",
        state=ACTIVE,
        snapshot=snapshot.to_dict(),
        message_id="m1",
        status_as_of="2026-10-29T19:35:44+00:00",
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def _event(event_type=events.WORK_ORDER_CHANGE, **fields):
    return MxEvent(key="k", event_type=event_type, work_order_id=BASE.id, **fields)


def _plan(fresh, event=None, record=None):
    return planner.plan_update(
        record=record or _record(),
        fresh=fresh,
        event=event or _event(),
        now=NOW,
        tz=TZ,
        name_of=NAMES.get,
    )


def _texts(plan):
    return [r.html for r in plan.replies]


class TestNoChange:
    def test_identical_snapshot_plans_nothing(self):
        plan = _plan(BASE)
        assert plan.replies == ()
        assert not plan.edit_root


class TestStatus:
    def test_status_change_replies_with_actor_and_edits_root(self):
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="IN_PROGRESS",
            user_id=500501,
            occurred_at="2026-10-30T19:14:00Z",
        )
        plan = _plan(replace(BASE, status="IN_PROGRESS"), event)
        assert _texts(plan) == ["<p><b>Status:</b> Open → In Progress — by P. Quinn</p>"]
        assert plan.edit_root
        assert plan.status_as_of == "2026-10-30T19:14:00+00:00"

    def test_done_reads_completed_by(self):
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE, old_status="OPEN", new_status="DONE", user_id=500501
        )
        plan = _plan(replace(BASE, status="DONE"), event)
        assert _texts(plan) == ["<p><b>Done</b> — completed by P. Quinn</p>"]

    def test_cancel_via_sub_status(self):
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="DONE",
            new_sub_status="CANCELED",
            user_id=500501,
        )
        plan = _plan(replace(BASE, status="DONE"), event)
        assert _texts(plan) == ["<p><b>Canceled</b> — by P. Quinn</p>"]
        assert plan.snapshot.sub_status == "CANCELED"

    def test_late_cancel_corrects_an_earlier_plain_done(self):
        # Another event saw DONE first (GET can't tell DONE from DONE+CANCELED); the status
        # event that follows carries the sub-status and must correct the post.
        record = _record(replace(BASE, status="DONE"))
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="DONE",
            new_sub_status="CANCELED",
            user_id=500501,
        )
        plan = _plan(replace(BASE, status="DONE"), event, record)
        assert _texts(plan) == ["<p><b>Canceled</b> — by P. Quinn</p>"]
        assert plan.edit_root
        assert plan.snapshot.sub_status == "CANCELED"

    def test_cancel_reported_by_get_credits_the_status_event_actor(self):
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="DONE",
            new_sub_status="CANCELED",
            user_id=500501,
        )
        fresh = replace(BASE, status="DONE", sub_status="CANCELED")  # GET said CANCELED
        assert _texts(_plan(fresh, event)) == ["<p><b>Canceled</b> — by P. Quinn</p>"]

    def test_cancel_seen_by_another_event_reads_canceled(self):
        fresh = replace(BASE, status="DONE", sub_status="CANCELED")
        plan = _plan(fresh, _event(events.WORK_ORDER_CHANGE))
        assert _texts(plan) == ["<p><b>Canceled</b></p>"]
        assert plan.snapshot.sub_status == "CANCELED"

    def test_reopen(self):
        record = _record(replace(BASE, status="DONE"))
        event = _event(events.WORK_ORDER_STATUS_CHANGE, old_status="DONE", new_status="OPEN")
        plan = _plan(BASE, event, record)
        assert _texts(plan) == ["<p><b>Reopened</b> — now Open</p>"]

    def test_actor_omitted_when_event_does_not_describe_the_current_status(self):
        # A stale event (status moved on since) must not credit its actor with the new state.
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="IN_PROGRESS",
            user_id=500501,
        )
        plan = _plan(replace(BASE, status="ON_HOLD"), event)
        assert _texts(plan) == ["<p><b>Status:</b> Open → On Hold</p>"]

    def test_status_already_recorded_plans_nothing(self):
        record = _record(replace(BASE, status="IN_PROGRESS"))
        event = _event(events.WORK_ORDER_STATUS_CHANGE, old_status="OPEN", new_status="IN_PROGRESS")
        plan = _plan(replace(BASE, status="IN_PROGRESS"), event, record)
        assert plan.replies == ()
        assert not plan.edit_root

    def test_as_of_falls_back_to_now(self):
        plan = _plan(replace(BASE, status="IN_PROGRESS"))
        assert plan.status_as_of == NOW.isoformat()


class TestAssignees:
    def test_added(self):
        plan = _plan(replace(BASE, assignee_ids=(22, 33)))
        assert _texts(plan) == ["<p>Assigned to Sam Lee</p>"]
        assert not plan.edit_root

    def test_everyone_removed(self):
        plan = _plan(replace(BASE, assignee_ids=()))
        assert _texts(plan) == ["<p>Unassigned — no one is assigned now</p>"]

    def test_swap(self):
        plan = _plan(replace(BASE, assignee_ids=(33,)))
        assert _texts(plan) == ["<p>Assigned to Sam Lee<br>Removed: Alex Kim</p>"]

    def test_unknown_user_falls_back_to_id(self):
        plan = _plan(replace(BASE, assignee_ids=(22, 99)))
        assert _texts(plan) == ["<p>Assigned to user 99</p>"]


class TestPriorityAndDueDate:
    def test_priority_change(self):
        plan = _plan(replace(BASE, priority="HIGH"))
        assert _texts(plan) == ["<p><b>Priority:</b> Low → High</p>"]

    def test_due_date_moved(self):
        plan = _plan(replace(BASE, due_date="2026-11-06T03:00:00.000Z"))
        assert _texts(plan) == ["<p><b>Due date moved:</b> Nov 2, 2026 → Nov 5, 2026</p>"]

    def test_due_date_set(self):
        record = _record(replace(BASE, due_date=None))
        plan = _plan(BASE, record=record)
        assert _texts(plan) == ["<p><b>Due date set:</b> Nov 2, 2026</p>"]

    def test_due_date_removed(self):
        plan = _plan(replace(BASE, due_date=None))
        assert _texts(plan) == ["<p><b>Due date removed</b></p>"]

    def test_due_time_change_on_the_same_day_is_silent(self):
        plan = _plan(replace(BASE, due_date="2026-11-03T04:30:00.000Z"))
        assert plan.replies == ()

    def test_due_date_change_rearms_the_overdue_notice(self):
        record = _record(overdue_notified=True)
        plan = _plan(replace(BASE, due_date="2026-11-06T03:00:00.000Z"), record=record)
        assert not plan.overdue_notified


class TestLocationTitleDescription:
    def test_location_change_replies(self):
        plan = _plan(replace(BASE, location_id=100102, location_name="Shelby"))
        assert _texts(plan) == ["<p>Moved to <b>Shelby</b> — updates continue in this thread.</p>"]

    def test_title_change_edits_root_silently(self):
        plan = _plan(replace(BASE, title="Vacuum 3 hose split and leaking"))
        assert plan.replies == ()
        assert plan.edit_root

    def test_description_change_edits_root_silently(self):
        plan = _plan(replace(BASE, description="Hose split, leaking at the coupler"))
        assert plan.replies == ()
        assert plan.edit_root

    def test_names_are_escaped(self):
        plan = _plan(replace(BASE, location_id=1, location_name="<Shelby>"))
        assert "&lt;Shelby&gt;" in _texts(plan)[0]


class TestOverdue:
    def test_overdue_replies_once(self):
        plan = _plan(BASE, _event(events.WORK_ORDER_OVERDUE))
        assert _texts(plan) == ["<p><b>Overdue</b> — was due Nov 2, 2026 and is still Open.</p>"]
        assert plan.overdue_notified

    def test_overdue_already_notified_is_silent(self):
        plan = _plan(BASE, _event(events.WORK_ORDER_OVERDUE), _record(overdue_notified=True))
        assert plan.replies == ()

    def test_overdue_after_completion_is_silent(self):
        record = _record(replace(BASE, status="DONE"))
        plan = _plan(replace(BASE, status="DONE"), _event(events.WORK_ORDER_OVERDUE), record)
        assert plan.replies == ()


class TestOrderingAndKeys:
    def test_several_changes_reply_in_a_stable_order(self):
        fresh = replace(BASE, status="IN_PROGRESS", priority="HIGH", assignee_ids=(22, 33))
        plan = _plan(fresh)
        assert [r.key.split(":")[0] for r in plan.replies] == ["status", "assignees", "priority"]

    def test_keys_name_the_change_not_the_wording(self):
        # The same transition seen by a status event (with actor) and by another event
        # (without) must share a key, or a retry would announce it twice.
        fresh = replace(BASE, status="IN_PROGRESS")
        with_actor = _plan(
            fresh,
            _event(events.WORK_ORDER_STATUS_CHANGE, new_status="IN_PROGRESS", user_id=500501),
        )
        without_actor = _plan(fresh, _event(events.WORK_ORDER_CHANGE))
        assert with_actor.replies[0].html != without_actor.replies[0].html
        assert with_actor.replies[0].key == without_actor.replies[0].key

    def test_different_transitions_have_different_keys(self):
        to_progress = _plan(replace(BASE, status="IN_PROGRESS")).replies[0].key
        to_hold = _plan(replace(BASE, status="ON_HOLD")).replies[0].key
        assert to_progress != to_hold


class TestDelete:
    def test_delete_plan(self):
        plan = planner.plan_delete()
        assert plan.html == "<p>This work order was <b>deleted in MaintainX</b>.</p>"
        assert plan.status_label == "Deleted in MaintainX"


class TestDeferredCompletion:
    def test_plain_done_seen_by_another_event_waits_for_the_status_event(self):
        # GET can't tell DONE from DONE+CANCELED; announcing "Done" here could be wrong.
        plan = _plan(replace(BASE, status="DONE"), _event(events.WORK_ORDER_CHANGE))
        assert plan.replies == ()
        assert plan.snapshot.status == "OPEN"

    def test_nightly_catch_up_does_announce_a_missed_completion(self):
        plan = _plan(replace(BASE, status="DONE"), _event(events.RECONCILE_WORK_ORDER))
        assert _texts(plan) == ["<p><b>Done</b></p>"]

    def test_status_event_without_sub_status_clears_an_old_cancel(self):
        record = _record(replace(BASE, status="DONE", sub_status="CANCELED"))
        event = _event(
            events.WORK_ORDER_STATUS_CHANGE,
            old_status="OPEN",
            new_status="DONE",
            user_id=500501,
        )
        plan = _plan(replace(BASE, status="DONE"), event, record)
        assert _texts(plan) == ["<p><b>Done</b> — completed by P. Quinn</p>"]
        assert plan.snapshot.sub_status is None


class TestStyledReplies:
    def _styled(self, fresh, event=None, record=None, **flags):
        return planner.plan_update(
            record=record or _record(), fresh=fresh, event=event or _event(), now=NOW, tz=TZ,
            name_of=NAMES.get, **{"emoji": True, "summary": True, **flags},
        )

    def test_new_status_first_with_was(self):
        event = _event(events.WORK_ORDER_STATUS_CHANGE, new_status="ON_HOLD", user_id=500501)
        plan = self._styled(replace(BASE, status="ON_HOLD"), event)
        assert _texts(plan) == [
            "<p><b>⏸️ On Hold</b> — by P. Quinn <i>(was \U0001F534 Open)</i></p>"
        ]

    def test_canceled(self):
        event = _event(events.WORK_ORDER_STATUS_CHANGE, new_status="DONE", new_sub_status="CANCELED")
        plan = self._styled(replace(BASE, status="DONE"), event)
        assert _texts(plan) == ["<p><b>❌ Canceled</b> <i>(was \U0001F534 Open)</i></p>"]

    def test_reopened(self):
        record = _record(replace(BASE, status="DONE"))
        event = _event(events.WORK_ORDER_STATUS_CHANGE, new_status="OPEN")
        plan = self._styled(BASE, event, record)
        assert _texts(plan) == [
            "<p><b>Reopened</b> — now \U0001F534 Open (was ✅ Done)</p>"
        ]

    def test_emoji_without_summary_keeps_the_classic_wording(self):
        event = _event(events.WORK_ORDER_STATUS_CHANGE, new_status="IN_PROGRESS")
        plan = self._styled(replace(BASE, status="IN_PROGRESS"), event, summary=False)
        assert _texts(plan) == ["<p><b>Status:</b> \U0001F534 Open → \U0001F7E1 In Progress</p>"]

    def test_overdue_gets_a_warning_sign(self):
        event = _event(events.WORK_ORDER_OVERDUE)
        plan = self._styled(BASE, event)
        assert _texts(plan)[0].startswith("<p>⚠️ <b>Overdue</b> — was due ")
