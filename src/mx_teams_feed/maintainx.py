"""The slice of the MaintainX REST API the feed needs, over an injected transport."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from ._http import raise_for_status
from .errors import NotFound, TransientError
from .ports import Clock, HttpTransport

DEFAULT_BASE_URL = "https://api.getmaintainx.com/v1"
# MaintainX allows 100 requests per 60 s per token; stay under it so the portal's own
# MaintainX traffic (UTrack pages) keeps some headroom.
DEFAULT_REQUESTS_PER_MINUTE = 60


class MaintainXClient:
    def __init__(
        self,
        transport: HttpTransport,
        api_key: str,
        clock: Clock,
        *,
        base_url: str = DEFAULT_BASE_URL,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        timeout: float = 20.0,
    ):
        self._transport = transport
        # Some stored keys already include "Bearer "; never send it twice (MaintainX 401s).
        key = api_key.strip()
        self._authorization = key if key.lower().startswith("bearer ") else f"Bearer {key}"
        self._clock = clock
        self._base = base_url.rstrip("/")
        self._rpm = requests_per_minute
        self._timeout = timeout
        self._sent: deque[datetime] = deque()

    # --- reads -------------------------------------------------------------------------

    def get_work_request(self, request_id: int) -> dict[str, Any]:
        return self._get(f"/workrequests/{request_id}")["workRequest"]

    def get_work_order(self, work_order_id: int) -> dict[str, Any]:
        return self._get(f"/workorders/{work_order_id}", {"expand": "location"})["workOrder"]

    def get_user(self, user_id: int) -> dict[str, Any]:
        return self._get(f"/users/{user_id}")["user"]

    def list_work_requests(
        self, statuses: Iterable[str], limit: int = 100, max_pages: int = 10
    ) -> list[dict[str, Any]]:
        statuses = list(statuses)
        results: list[dict[str, Any]] = []
        cursor = None
        for _ in range(max_pages):
            query: dict[str, Any] = {"statuses": statuses, "limit": limit}
            if cursor:
                query["cursor"] = cursor
            data = self._get("/workrequests", query)
            results.extend(data.get("workRequests") or [])
            cursor = data.get("nextCursor")
            if not cursor:
                break
        return results

    def get_subscription(self, subscription_id: int) -> dict[str, Any] | None:
        try:
            return self._get(f"/subscriptions/{subscription_id}")["subscription"]
        except NotFound:
            return None

    # --- subscription admin (used by host setup pages, not by the engine) --------------

    def create_subscription(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/subscriptions", json=body)

    def get_subscription_secret(self, subscription_id: int) -> str:
        return self._get(f"/subscriptions/{subscription_id}/secret")["secret"]

    def delete_subscription(self, subscription_id: int) -> None:
        self._request("DELETE", f"/subscriptions/{subscription_id}")

    # --- files -------------------------------------------------------------------------

    def download(self, presigned_url: str) -> tuple[bytes, str]:
        """Fetch an attachment. Presigned S3 URLs reject an Authorization header, and they
        expire after 60 minutes — a 403 means "re-fetch the record for a fresh URL"."""
        response = self._transport.send("GET", presigned_url, headers={}, timeout=self._timeout)
        if response.status in (400, 403):
            raise TransientError(f"attachment download: {response.status}", status=response.status)
        raise_for_status(response, "attachment download")
        mime = (response.header("Content-Type") or "application/octet-stream").split(";")[0]
        return response.body, mime.strip()

    # --- plumbing ----------------------------------------------------------------------

    def _get(self, path: str, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, query=query)

    def _request(
        self, method: str, path: str, *, query: dict[str, Any] | None = None, json: Any = None
    ) -> dict[str, Any]:
        self._throttle()
        url = self._base + path
        if query:
            url += "?" + urlencode(query, doseq=True)
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        response = self._transport.send(
            method, url, headers=headers, json=json, timeout=self._timeout
        )
        raise_for_status(response, f"MaintainX {method} {path}")
        return response.json() or {}

    def _throttle(self) -> None:
        window = timedelta(seconds=60)
        now = self._clock.now()
        while self._sent and now - self._sent[0] >= window:
            self._sent.popleft()
        if len(self._sent) >= self._rpm:
            self._clock.sleep((self._sent[0] + window - now).total_seconds())
            now = self._clock.now()
            while self._sent and now - self._sent[0] >= window:
                self._sent.popleft()
        self._sent.append(now)
