"""Microsoft Graph channel messaging as the poster account (delegated permissions).

Scopes: ChannelMessage.Send (post, reply), ChannelMessage.ReadWrite (edit; admin consent),
ChannelMessage.Read.All (find a root post whose id was lost; admin consent),
Channel.ReadBasic.All (channel lookup by name). Graph allows ~1 write per second per user, and
every write here comes from one account, so writes are spaced `min_write_interval` apart.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ._http import raise_for_status
from .errors import CredentialsRejected
from .ports import Clock, GraphTokenProvider, HttpResponse, HttpTransport
from .render import parse_iso

DEFAULT_BASE_URL = "https://graph.microsoft.com/v1.0"
_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


@dataclass(frozen=True)
class HostedImage:
    """An inline image; reference it in the body as `<img src="../hostedContents/{id}/$value">`."""

    temp_id: str
    data: bytes
    mime_type: str


@dataclass(frozen=True)
class PostedMessage:
    id: str
    web_url: str | None
    body_html: str


def image_tags(body_html: str) -> list[str]:
    """The <img> tags of a posted message — Graph rewrites hosted-content placeholders into
    permanent graph.microsoft.com URLs, which later edits must re-send unchanged."""
    return _IMG_TAG.findall(body_html or "")


class GraphClient:
    def __init__(
        self,
        transport: HttpTransport,
        tokens: GraphTokenProvider,
        clock: Clock,
        *,
        base_url: str = DEFAULT_BASE_URL,
        min_write_interval: float = 1.25,
        timeout: float = 30.0,
    ):
        self._transport = transport
        self._tokens = tokens
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._interval = min_write_interval
        self._timeout = timeout
        self._last_write: datetime | None = None

    # --- messages ----------------------------------------------------------------------

    def post_message(
        self, team_id: str, channel_id: str, subject: str, html: str, images: Sequence[HostedImage]
    ) -> PostedMessage:
        body: dict[str, Any] = {"subject": subject, "body": _html(html)}
        if images:
            body["hostedContents"] = [
                {
                    "@microsoft.graph.temporaryId": image.temp_id,
                    "contentBytes": base64.b64encode(image.data).decode("ascii"),
                    "contentType": image.mime_type,
                }
                for image in images
            ]
        data = self._call("POST", _messages(team_id, channel_id), body).json() or {}
        return _posted(data)

    def update_message(
        self, team_id: str, channel_id: str, message_id: str, subject: str, html: str
    ) -> None:
        path = f"{_messages(team_id, channel_id)}/{_q(message_id)}"
        self._call("PATCH", path, {"subject": subject, "body": _html(html)})

    def post_reply(self, team_id: str, channel_id: str, message_id: str, html: str) -> str:
        path = f"{_messages(team_id, channel_id)}/{_q(message_id)}/replies"
        data = self._call("POST", path, {"body": _html(html)}).json() or {}
        return str(data.get("id", ""))

    # --- lookups -----------------------------------------------------------------------

    def find_channel_id(self, team_id: str, display_name: str) -> str | None:
        data = self._call("GET", f"/teams/{_q(team_id)}/channels").json() or {}
        wanted = display_name.strip().casefold()
        for channel in data.get("value") or []:
            if str(channel.get("displayName", "")).strip().casefold() == wanted:
                return channel.get("id")
        return None

    def find_root_message(
        self,
        team_id: str,
        channel_id: str,
        marker: str,
        top: int = 50,
        created_after: datetime | None = None,
    ) -> PostedMessage | None:
        """Recover a root post whose id we failed to save, by a marker unique to it (the work
        order's own link; "…wo=5" must not match "…wo=50"). Needs ChannelMessage.Read.All."""
        pattern = re.compile(re.escape(marker) + r"(?!\d)")
        data = self._call("GET", f"{_messages(team_id, channel_id)}?$top={top}").json() or {}
        for message in data.get("value") or []:
            if message.get("deletedDateTime"):
                continue
            created = message.get("createdDateTime")
            if created_after and created and parse_iso(created) < created_after:
                continue
            if pattern.search((message.get("body") or {}).get("content") or ""):
                return _posted(message)
        return None

    def refresh_token(self) -> None:
        """Exercise the poster's refresh token so it never idles out (90-day inactivity)."""
        self._tokens.access_token(force_refresh=True)

    # --- plumbing ----------------------------------------------------------------------

    def _call(self, method: str, path: str, json: Any = None) -> HttpResponse:
        token = self._tokens.access_token()
        response = self._send(method, path, json, token)
        if response.status == 401:
            token = self._tokens.access_token(force_refresh=True)
            response = self._send(method, path, json, token)
            if response.status == 401:
                raise CredentialsRejected(f"Graph {method}: 401 even after a token refresh")
        raise_for_status(response, f"Graph {method}")
        return response

    def _send(self, method: str, path: str, json: Any, token: str) -> HttpResponse:
        if method in _WRITE_METHODS:
            self._pace()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = self._transport.send(
            method, self._base + path, headers=headers, json=json, timeout=self._timeout
        )
        if method in _WRITE_METHODS:
            self._last_write = self._clock.now()
        return response

    def _pace(self) -> None:
        if self._last_write is None:
            return
        elapsed = (self._clock.now() - self._last_write).total_seconds()
        if elapsed < self._interval:
            self._clock.sleep(self._interval - elapsed)


def _messages(team_id: str, channel_id: str) -> str:
    return f"/teams/{_q(team_id)}/channels/{_q(channel_id)}/messages"


def _q(value: str) -> str:
    return quote(str(value), safe=":@.")


def _html(content: str) -> dict[str, str]:
    return {"contentType": "html", "content": content}


def _posted(data: dict[str, Any]) -> PostedMessage:
    return PostedMessage(
        id=str(data.get("id", "")),
        web_url=data.get("webUrl"),
        body_html=(data.get("body") or {}).get("content") or "",
    )
