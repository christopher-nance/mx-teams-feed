"""Shared HTTP status → exception mapping."""

from __future__ import annotations

from .errors import CredentialsRejected, NotFound, PermanentError, TransientError
from .ports import HttpResponse

DEFAULT_RETRY_AFTER = 60.0
MAX_RETRY_AFTER = 3600.0  # never park an event longer, whatever a header claims


def raise_for_status(response: HttpResponse, what: str) -> None:
    status = response.status
    if status < 400:
        return
    detail = _detail(response)
    if status == 404:
        raise NotFound(f"{what}: 404 {detail}", status=404)
    if status == 401:
        raise CredentialsRejected(f"{what}: 401 {detail}")
    if status == 429 or status >= 500:
        raise TransientError(
            f"{what}: {status} {detail}", status=status, retry_after=retry_after(response)
        )
    raise PermanentError(f"{what}: {status} {detail}", status=status)


def retry_after(response: HttpResponse) -> float:
    for header in ("Retry-After", "X-Rate-Limit-Reset"):
        value = response.header(header)
        if value:
            try:
                return min(MAX_RETRY_AFTER, max(1.0, float(value)))
            except ValueError:
                continue
    return DEFAULT_RETRY_AFTER


def _detail(response: HttpResponse) -> str:
    # Error bodies can echo request data; keep logs short and free of payload contents.
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("code") or "")[:80]
        if isinstance(error, str):
            return error[:80]
    return ""
