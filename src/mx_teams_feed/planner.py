"""Decide what to say in Teams when a work order changes. Pure: no I/O.

The planner diffs the snapshot stored with the post against a freshly fetched work order, so
duplicate, late or out-of-order webhook deliveries can't produce duplicate replies — once a
change is recorded in the snapshot it is never announced again. Each reply carries a stable
`key` so the engine can skip replies already sent when an event is retried mid-way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo

from . import events
from .events import MxEvent
from .models import TERMINAL_STATUSES, PostRecord, WorkOrderSnapshot
from .render import esc, format_date, parse_iso, status_badge, status_label

NameOf = Callable[[int], str | None]

_PRIORITY_LABELS = {"NONE": "None", "LOW": "Low", "MEDIUM": "Medium", "HIGH": "High"}


@dataclass(frozen=True)
class Reply:
    # Names the change ("status:OPEN/-->IN_PROGRESS/-"), never its wording, so the same change
    # seen by two different events is recognized as already announced.
    key: str
    html: str


@dataclass(frozen=True)
class UpdatePlan:
    replies: tuple[Reply, ...]
    edit_root: bool
    snapshot: WorkOrderSnapshot
    status_as_of: str | None
    overdue_notified: bool


@dataclass(frozen=True)
class DeletePlan:
    html: str
    status_label: str


def plan_update(
    *,
    record: PostRecord,
    fresh: WorkOrderSnapshot,
    event: MxEvent,
    now: datetime,
    tz: tzinfo,
    name_of: NameOf,
    emoji: bool = False,
    summary: bool = False,
) -> UpdatePlan:
    old = record.work_order
    replies: list[Reply] = []
    edit_root = False
    status_as_of = record.status_as_of
    overdue_notified = record.overdue_notified

    # Only credit the event's actor if the event describes the state we are announcing.
    describes_current = (
        event.event_type == events.WORK_ORDER_STATUS_CHANGE and event.new_status == fresh.status
    )
    if fresh.sub_status:  # GET said CANCELED/SKIPPED outright
        sub_status = fresh.sub_status
    elif describes_current:  # the status event is authoritative, including "no sub-status"
        sub_status = event.new_sub_status
    elif fresh.status == old.status:
        sub_status = old.sub_status
    else:
        sub_status = None
    fresh = replace(fresh, sub_status=sub_status)
    if (
        not describes_current
        and event.event_type != events.RECONCILE_WORK_ORDER
        and fresh.status == "DONE"
        and not fresh.sub_status
        and old.status != "DONE"
    ):
        # GET can't tell DONE from DONE+CANCELED; leave completion to the status event (seconds
        # behind) rather than announce "Done" for a cancellation. The nightly refresh catches up.
        fresh = replace(fresh, status=old.status, sub_status=old.sub_status)

    if _state(fresh) != _state(old):
        actor = _name(name_of, event.user_id) if describes_current and event.user_id else None
        replies.append(
            Reply(
                f"status:{_state(old)}->{_state(fresh)}",
                _summary_status_html(old, fresh, actor, emoji)
                if summary
                else _status_html(old, fresh, actor, emoji),
            )
        )
        edit_root = True
        when = parse_iso(event.occurred_at) if event.occurred_at else now
        status_as_of = when.isoformat()

    assignee_html = _assignees_html(old, fresh, name_of)
    if assignee_html:
        key = f"assignees:{_ids(old.assignee_ids)}->{_ids(fresh.assignee_ids)}"
        replies.append(Reply(key, assignee_html))

    if fresh.priority != old.priority:
        replies.append(
            Reply(
                f"priority:{old.priority}->{fresh.priority}",
                f"<p><b>Priority:</b> {_priority(old.priority)} → {_priority(fresh.priority)}</p>",
            )
        )

    due_html = _due_html(old, fresh, tz)
    if due_html:
        replies.append(Reply(f"due:{old.due_date}->{fresh.due_date}", due_html))
        overdue_notified = False

    if fresh.location_id != old.location_id and fresh.location_id is not None:
        where = esc(fresh.location_name or f"location {fresh.location_id}")
        replies.append(
            Reply(
                f"location:{old.location_id}->{fresh.location_id}",
                f"<p>Moved to <b>{where}</b> — updates continue in this thread.</p>",
            )
        )

    if fresh.title != old.title or fresh.description != old.description:
        edit_root = True

    if (
        event.event_type == events.WORK_ORDER_OVERDUE
        and not overdue_notified
        and fresh.status not in TERMINAL_STATUSES
    ):
        label = esc(status_badge(status_label(fresh.status, fresh.sub_status), emoji))
        if fresh.due_date:
            text = f"was due {format_date(fresh.due_date, tz)} and is still {label}."
        else:
            text = f"still {label}."
        mark = "⚠️ " if emoji else ""
        replies.append(
            Reply(f"overdue:{fresh.due_date}", f"<p>{mark}<b>Overdue</b> — {text}</p>")
        )
        overdue_notified = True

    return UpdatePlan(
        replies=tuple(replies),
        edit_root=edit_root,
        snapshot=fresh,
        status_as_of=status_as_of,
        overdue_notified=overdue_notified,
    )


def plan_delete() -> DeletePlan:
    return DeletePlan(
        html="<p>This work order was <b>deleted in MaintainX</b>.</p>",
        status_label="Deleted in MaintainX",
    )


def _summary_status_html(
    old: WorkOrderSnapshot, new: WorkOrderSnapshot, actor: str | None, emoji: bool
) -> str:
    """New status first: "🟡 In Progress — by Jane Doe (was 🔴 Open)"."""
    new_label = status_badge(status_label(new.status, new.sub_status), emoji)
    old_label = status_badge(status_label(old.status, old.sub_status), emoji)
    if _is_terminal(old) and not _is_terminal(new):
        return f"<p><b>Reopened</b> — now {esc(new_label)} (was {esc(old_label)})</p>"
    if new.status == "DONE" and not new.sub_status:
        by = f" — completed by {esc(actor)}" if actor else ""
    else:
        by = f" — by {esc(actor)}" if actor else ""
    return f"<p><b>{esc(new_label)}</b>{by} <i>(was {esc(old_label)})</i></p>"


def _status_html(
    old: WorkOrderSnapshot, new: WorkOrderSnapshot, actor: str | None, emoji: bool = False
) -> str:
    by = f" — by {esc(actor)}" if actor else ""
    new_label = status_badge(status_label(new.status, new.sub_status), emoji)
    if _is_terminal(old) and not _is_terminal(new):
        return f"<p><b>Reopened</b> — now {esc(new_label)}</p>"
    if new.status == "DONE" and not new.sub_status:
        completed = f" — completed by {esc(actor)}" if actor else ""
        return f"<p><b>Done</b>{completed}</p>"
    if _is_terminal(new):
        return f"<p><b>{esc(new_label)}</b>{by}</p>"
    old_label = status_badge(status_label(old.status, old.sub_status), emoji)
    return f"<p><b>Status:</b> {esc(old_label)} → {esc(new_label)}{by}</p>"


def _assignees_html(old: WorkOrderSnapshot, new: WorkOrderSnapshot, name_of: NameOf) -> str | None:
    added = [a for a in new.assignee_ids if a not in old.assignee_ids]
    removed = [a for a in old.assignee_ids if a not in new.assignee_ids]
    if not added and not removed:
        return None
    if removed and not new.assignee_ids:
        return "<p>Unassigned — no one is assigned now</p>"
    lines = []
    if added:
        lines.append("Assigned to " + ", ".join(esc(_name(name_of, a)) for a in added))
    if removed:
        lines.append("Removed: " + ", ".join(esc(_name(name_of, a)) for a in removed))
    return f"<p>{'<br>'.join(lines)}</p>"


def _due_html(old: WorkOrderSnapshot, new: WorkOrderSnapshot, tz: tzinfo) -> str | None:
    before = format_date(old.due_date, tz) if old.due_date else None
    after = format_date(new.due_date, tz) if new.due_date else None
    if before == after:
        return None
    if before is None:
        return f"<p><b>Due date set:</b> {after}</p>"
    if after is None:
        return "<p><b>Due date removed</b></p>"
    return f"<p><b>Due date moved:</b> {before} → {after}</p>"


def _state(snapshot: WorkOrderSnapshot) -> str:
    return f"{snapshot.status}/{snapshot.sub_status or '-'}"


def _ids(ids: tuple[int, ...]) -> str:
    return ",".join(str(i) for i in ids) or "-"


def _is_terminal(snapshot: WorkOrderSnapshot) -> bool:
    return snapshot.status in TERMINAL_STATUSES or snapshot.sub_status in ("CANCELED", "SKIPPED")


def _priority(value: str) -> str:
    return _PRIORITY_LABELS.get(value, value.title())


def _name(name_of: NameOf, user_id: int) -> str:
    return name_of(user_id) or f"user {user_id}"
