"""MaintainX webhook signature verification (fails closed).

MaintainX signs `"<t>.<raw body>"` with HMAC-SHA256 using the subscription secret and sends
`t=<unix seconds>,v1=<hex>` in `x-maintainx-webhook-body-signature`. It also sends a URI
signature, but that covers only the route URL — a single leaked header would authenticate any
forged body for the whole tolerance window — so it is never accepted on its own.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

BODY_HEADER = "x-maintainx-webhook-body-signature"
DEFAULT_TOLERANCE_SECONDS = 300
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def verify(
    raw_body: bytes,
    headers: Mapping[str, str],
    url: str,
    secrets: Sequence[str],
    now: datetime,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    secrets = [s for s in secrets if s]
    if not secrets:
        return False
    lowered = {k.lower(): v for k, v in headers.items()}
    if BODY_HEADER not in lowered:
        return False
    try:
        message = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return _check(lowered[BODY_HEADER], message, secrets, now, tolerance_seconds)


def _check(
    header: str, message: str, secrets: Sequence[str], now: datetime, tolerance: int
) -> bool:
    parsed = _parse(header)
    if parsed is None:
        return False
    timestamp, signatures = parsed
    if abs(now.timestamp() - timestamp) > tolerance:
        return False
    signed = f"{timestamp}.{message}".encode()
    for secret in secrets:
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, sig) for sig in signatures):
            return True
    return False


def _parse(header: str) -> tuple[int, list[str]] | None:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        name, _, value = part.strip().partition("=")
        if name == "t":
            value = value.strip()
            if not value.isdigit() or len(value) > 12:  # unix seconds; reject absurd values
                return None
            timestamp = int(value)
        elif name == "v1" and _HEX_SHA256.match(value.strip().lower()):
            signatures.append(value.strip().lower())
    if timestamp is None or not signatures:
        return None
    return timestamp, signatures
