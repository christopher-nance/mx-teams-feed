"""Plain data records the engine reads and writes through the FeedStore port."""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, fields
from datetime import datetime
from typing import Any

from .events import MxEvent
from .render import scrub_description

# PostRecord.state
CREATING = "creating"  # claimed; the root post may or may not exist yet
ACTIVE = "active"
DELETED = "deleted"  # work order deleted in MaintainX; root kept as a tombstone
LOST = "lost"  # the root post was removed from Teams; stop editing it
ABANDONED = "abandoned"  # never posted, and no longer worth posting (closed, gone, unrouted)

# QueuedEvent.state
PENDING = "pending"
LEASED = "leased"
DONE = "done"
DEAD = "dead"

TERMINAL_STATUSES = frozenset({"DONE", "CANCELED", "SKIPPED"})
_SUB_STATUSES = ("CANCELED", "SKIPPED")


def _load(cls, data: dict[str, Any], fallbacks: dict[str, Any]):
    """Build a dataclass from stored data that may lack null/empty fields (Firebase RTDB drops
    them) or carry keys from a newer version."""
    values = {}
    for f in fields(cls):
        if f.name in data:
            values[f.name] = data[f.name]
        elif f.default is MISSING and f.default_factory is MISSING:
            values[f.name] = fallbacks.get(f.name)
    return cls(**values)


@dataclass(frozen=True)
class WorkOrderSnapshot:
    """The fields of a work order the feed renders or diffs."""

    id: int
    sequential_id: int | None
    title: str
    description: str
    status: str
    priority: str
    due_date: str | None
    assignee_ids: tuple[int, ...]
    location_id: int | None
    location_name: str | None
    # Webhooks report a cancellation as DONE + newSubStatus CANCELED, while GET /workorders may
    # say CANCELED outright; both are normalized to status DONE + sub_status. When GET reports
    # plain DONE the sub-status is carried over from the status-change event that set it.
    sub_status: str | None = None

    @classmethod
    def from_api(cls, wo: dict[str, Any]) -> WorkOrderSnapshot:
        location = wo.get("location") or {}
        status = wo.get("status") or "OPEN"
        sub_status = None
        if status in _SUB_STATUSES:
            status, sub_status = "DONE", status
        return cls(
            id=int(wo["id"]),
            sequential_id=wo.get("sequentialId"),
            title=wo.get("title") or "",
            # Stored without the requester-contact trailer (it holds an email address).
            description=scrub_description(wo.get("description")),
            status=status,
            priority=wo.get("priority") or "NONE",
            due_date=wo.get("dueDate"),
            assignee_ids=tuple(sorted(int(a) for a in wo.get("assigneeIds") or [])),
            location_id=wo.get("locationId"),
            location_name=location.get("name"),
            sub_status=sub_status,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["assignee_ids"] = list(self.assignee_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkOrderSnapshot:
        loaded = _load(
            cls, data, {"title": "", "description": "", "status": "OPEN", "priority": "NONE"}
        )
        return cls(**{**asdict(loaded), "assignee_ids": tuple(data.get("assignee_ids") or ())})


@dataclass
class PostRecord:
    """One Teams root post per work order. `version` is bumped by the store on every save."""

    work_order_id: int
    request_id: int | None
    team_id: str
    channel_id: str
    state: str
    snapshot: dict[str, Any]
    site_names: list[str] = field(default_factory=list)  # for stripping the title prefix
    requester: str | None = None
    message_id: str | None = None
    web_url: str | None = None
    images_html: str = ""
    extra_photos: int = 0
    status_as_of: str | None = None
    # Set just before a root POST whose outcome we can't be sure of (timeout, 5xx, crash);
    # only then is it worth searching the channel for a post whose id we failed to save.
    post_attempted_at: str | None = None
    # Fingerprint of what the root post currently shows; any mismatch with what it should show
    # (an edit that failed or was refused) triggers a corrective edit.
    root_digest: str | None = None
    overdue_notified: bool = False
    applied_event: str | None = None
    applied_actions: list[str] = field(default_factory=list)
    version: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def work_order(self) -> WorkOrderSnapshot:
        return WorkOrderSnapshot.from_dict(self.snapshot)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostRecord:
        record = _load(cls, data, {"team_id": "", "channel_id": "", "snapshot": {}})
        record.snapshot = dict(record.snapshot or {})
        record.site_names = list(record.site_names or [])
        record.applied_actions = list(record.applied_actions or [])
        return record


@dataclass
class QueuedEvent:
    event: MxEvent
    state: str = PENDING
    attempts: int = 0
    next_attempt_at: datetime | None = None
    lease_until: datetime | None = None
    received_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ChannelTarget:
    """Where a MaintainX location's posts go. `channel_id` may be unknown until first use."""

    team_id: str
    channel_id: str | None = None
    channel_name: str = "Maintenance"
    site_names: tuple[str, ...] = ()
