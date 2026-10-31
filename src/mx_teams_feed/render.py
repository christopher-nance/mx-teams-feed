"""Text and HTML rendering for Teams posts. Pure functions only."""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

# The portal's request wizard titles look like "<Site> - 2026-10-29 @ 2:35 PM CDT - <text>".
_MACHINE_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} @ \d{1,2}:\d{2} [AP]M\b")
_CONTACT_MARKER = "requester contact information"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_STATUS_LABELS = {
    "OPEN": "Open",
    "IN_PROGRESS": "In Progress",
    "ON_HOLD": "On Hold",
    "DONE": "Done",
    "CANCELED": "Canceled",
    "SKIPPED": "Skipped",
}


def strip_title_prefix(title: str, site_names: Iterable[str]) -> str:
    """Drop the portal's "<Site> - <timestamp> - " prefix; never touch hand-typed titles.

    The prefix only counts when it names one of this work order's own sites and is followed
    by the portal's timestamp, so an ordinary title can't be mangled by accident.
    """
    title = (title or "").strip()
    lowered = title.lower()
    for name in sorted({n.strip() for n in site_names if n and n.strip()}, key=len, reverse=True):
        prefix = f"{name} - "
        if not lowered.startswith(prefix.lower()):
            continue
        stamp, sep, tail = title[len(prefix) :].partition(" - ")
        if sep and _MACHINE_TIMESTAMP.match(stamp.strip()) and tail.strip():
            return tail.strip()
    return title


def headline(title: str, sequential_id: int | None, site_names: Iterable[str]) -> str:
    text = strip_title_prefix(title, site_names)
    if text:
        return text
    return f"Work order #{sequential_id}" if sequential_id else "Work order"


def scrub_description(text: str | None) -> str:
    """Remove the requester-contact trailer MaintainX appends at approval (it holds an email)."""
    text = (text or "").strip()
    cut = text.lower().find(_CONTACT_MARKER)
    if cut != -1:
        text = text[:cut].strip()
    lines = text.split("\n")
    # Safety net if the marker wording changes: a final line that is one bare email address.
    if len(lines) > 1 and "@" in lines[-1] and " " not in lines[-1].strip():
        text = "\n".join(lines[:-1]).strip()
    return text


def status_label(status: str | None, sub_status: str | None = None) -> str:
    if sub_status in ("CANCELED", "SKIPPED"):
        return _STATUS_LABELS[sub_status]
    if not status:
        return "Unknown"
    return _STATUS_LABELS.get(status, status.replace("_", " ").title())


def format_local(when: datetime, tz: tzinfo) -> str:
    local = when.astimezone(tz)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return (
        f"{_MONTHS[local.month - 1]} {local.day}, {local.year} "
        f"{hour}:{local.minute:02d} {meridiem} {local.tzname()}"
    )


def format_date(iso: str, tz: tzinfo) -> str:
    local = parse_iso(iso).astimezone(tz)
    return f"{_MONTHS[local.month - 1]} {local.day}, {local.year}"


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def multiline(text: str) -> str:
    return esc(text).replace("\r\n", "\n").replace("\n", "<br>")


@dataclass(frozen=True)
class RootView:
    status_label: str
    as_of_text: str
    sequential_id: int | None
    requester: str | None
    description: str
    images_html: str
    link_url: str
    extra_photos: int = 0


def root_html(view: RootView) -> str:
    parts = [
        f"<p><b>Status:</b> {esc(view.status_label)} <i>(as of {esc(view.as_of_text)})</i></p>"
    ]
    meta = [f"WO #{view.sequential_id}" if view.sequential_id else "Work order"]
    if view.requester:
        meta.append(f"Requested by {esc(view.requester)}")
    parts.append(f"<p>{' · '.join(meta)}</p>")
    if view.description:
        parts.append(f"<p>{multiline(view.description)}</p>")
    if view.images_html:
        parts.append(f"<p>{view.images_html}</p>")
    if view.extra_photos:
        noun = "photo" if view.extra_photos == 1 else "photos"
        parts.append(f"<p><i>{view.extra_photos} more {noun} in the portal</i></p>")
    parts.append(f'<p><a href="{esc(view.link_url)}">Open work order in the Team Portal</a></p>')
    return "".join(parts)
