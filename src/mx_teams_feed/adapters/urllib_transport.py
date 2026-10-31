"""HttpTransport on the standard library. Fine for V3 and scripts; V4 should inject its own
`app.core.http` client instead so its sandbox outbound guard stays in force."""

from __future__ import annotations

import json as _json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..errors import TransientError
from ..ports import HttpResponse


class UrllibTransport:
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Any = None,
        content: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        sent_headers = dict(headers or {})
        data = content
        if json is not None:
            data = _json.dumps(json).encode("utf-8")
            sent_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=sent_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                headers_out = dict(response.headers.items())
                return HttpResponse(response.status, headers_out, response.read())
        except urllib.error.HTTPError as error:  # a real HTTP status: let the caller classify it
            with error:
                return HttpResponse(error.code, dict(error.headers.items()), error.read())
        except TimeoutError as exc:
            raise TransientError(f"{method} timed out") from exc
        except urllib.error.URLError as exc:
            raise TransientError(f"{method} connection failed: {exc.reason}") from exc
        except OSError as exc:  # connection reset mid-read and the like
            raise TransientError(f"{method} connection error: {exc}") from exc
