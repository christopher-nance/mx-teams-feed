"""MaintainX webhook events, normalized into one shape."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from typing import Any

from .errors import InvalidPayload

WORK_REQUEST_STATUS_CHANGE = "WORK_REQUEST_STATUS_CHANGE"
WORK_ORDER_STATUS_CHANGE = "WORK_ORDER_STATUS_CHANGE"
WORK_ORDER_CHANGE = "WORK_ORDER_CHANGE"
WORK_ORDER_OVERDUE = "WORK_ORDER_OVERDUE"
WORK_ORDER_DELETE = "WORK_ORDER_DELETE"
# Synthetic events the nightly reconcile enqueues; never sent by MaintainX.
BACKFILL_APPROVED = "BACKFILL_APPROVED"
RECONCILE_WORK_ORDER = "RECONCILE_WORK_ORDER"

WEBHOOK_EVENT_TYPES = frozenset(
    {
        WORK_REQUEST_STATUS_CHANGE,
        WORK_ORDER_STATUS_CHANGE,
        WORK_ORDER_CHANGE,
        WORK_ORDER_OVERDUE,
        WORK_ORDER_DELETE,
    }
)


@dataclass(frozen=True)
class MxEvent:
    """Only scalar ids and statuses are kept. Payload bodies (nested work-order copies with the
    requester's email, presigned file URLs, custom-field labels) are never stored: the engine
    re-reads current state from the API instead."""

    key: str
    event_type: str
    org_id: int | None = None
    occurred_at: str | None = None
    work_order_id: int | None = None
    request_id: int | None = None
    user_id: int | None = None
    old_status: str | None = None
    new_status: str | None = None
    old_sub_status: str | None = None
    new_sub_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MxEvent:
        # Tolerates stores that drop null fields (Firebase RTDB) and ignores unknown keys.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def parse_event(raw_body: bytes) -> MxEvent:
    try:
        payload = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidPayload("body is not JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("eventType"), str):
        raise InvalidPayload("body has no eventType")
    return MxEvent(
        key=hashlib.sha256(raw_body).hexdigest(),
        event_type=payload["eventType"],
        org_id=_int(payload.get("orgId")),
        occurred_at=payload.get("occurredAt"),
        work_order_id=_int(payload.get("workOrderId")),
        # WORK_REQUEST_STATUS_CHANGE says requestId; NEW_WORK_REQUEST says workRequestId.
        request_id=_int(payload.get("requestId", payload.get("workRequestId"))),
        user_id=_int(payload.get("userId")),
        old_status=payload.get("oldStatus"),
        new_status=payload.get("newStatus"),
        old_sub_status=payload.get("oldSubStatus"),
        new_sub_status=payload.get("newSubStatus"),
    )


def synthetic(key: str, event_type: str, **ids: Any) -> MxEvent:
    return MxEvent(key=key, event_type=event_type, **ids)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
