"""Small Crunchbase API v4 HTTP client with an in-memory rate limiter.

Endpoint paths and field names follow the Crunchbase "Advanced Financials
Package" OpenAPI 3.0.1 spec (v1.1.0, https://data.crunchbase.com/docs):
server ``https://api.crunchbase.com/v4``, paths under ``/data``, and the API
key sent in the ``X-cb-user-key`` header. Crunchbase data is licensed, so
nothing is cached to disk.
"""

from collections import deque
from datetime import datetime, timedelta
import json
import os
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


BASE_URL = "https://api.crunchbase.com/v4/data"
MINUTE_LIMIT = 200
WINDOW_SECONDS = 60
AUTH_ERROR = {
    "error": "Crunchbase API key rejected (likely expired or rotated). Update CRUNCHBASE_API_KEY in Render.",
    "error_type": "auth_error",
}


def api_key_configured() -> bool:
    """Return True when CRUNCHBASE_API_KEY is set to a non-blank value."""
    return bool(os.environ.get("CRUNCHBASE_API_KEY", "").strip())


def entity_path(*parts: str) -> str:
    """Join path segments, percent-encoding each so IDs cannot alter the path."""
    return "/" + "/".join(quote(str(part), safe="") for part in parts)


def _provider_message(body: bytes) -> str:
    """Extract a readable message from a Crunchbase error body.

    Errors may be a JSON object, a JSON array of error objects, or text/plain
    containing a JSON array (gateway errors).
    """
    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text[:300] or "no error details"
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict):
            message = item.get("message") or item.get("error")
            if message:
                code = item.get("code")
                return f"{message} ({code})" if code else str(message)
    return text[:300] or "no error details"


class CrunchbaseClient:
    """Crunchbase client enforcing a rolling per-minute request limit."""

    def __init__(self):
        self._lock = Lock()
        self._minute_requests = deque()

    def reset_quota_for_tests(self) -> None:
        """Clear the rolling window for deterministic tests."""
        with self._lock:
            self._minute_requests.clear()

    def _reserve_request(self) -> dict | None:
        """Reserve one request or describe when capacity returns."""
        with self._lock:
            now_mono = time.monotonic()
            while self._minute_requests and now_mono - self._minute_requests[0] >= WINDOW_SECONDS:
                self._minute_requests.popleft()

            if len(self._minute_requests) >= MINUTE_LIMIT:
                retry_after = max(
                    1, int(WINDOW_SECONDS - (now_mono - self._minute_requests[0])) + 1
                )
                resets_at = datetime.now().astimezone() + timedelta(seconds=retry_after)
                return {
                    "error": "Crunchbase per-minute rate limit reached.",
                    "error_type": "quota_exhausted",
                    "quota": "per_minute",
                    "limit": MINUTE_LIMIT,
                    "retry_after_seconds": retry_after,
                    "resets_at": resets_at.isoformat(),
                }

            self._minute_requests.append(now_mono)
            return None

    def request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
        timeout: float = 20,
    ) -> dict:
        """Call a Crunchbase /data endpoint after enforcing the rate limit."""
        api_key = os.environ.get("CRUNCHBASE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "CRUNCHBASE_API_KEY is not set. Configure it before calling a Crunchbase tool."
            )

        quota_error = self._reserve_request()
        if quota_error:
            return quota_error

        url = f"{BASE_URL}{path}"
        if params:
            # The spec expects spaces in `order` as %20 and comma-separated
            # card_field_ids, so quote rather than plus-encode.
            url = f"{url}?{urlencode(params, quote_via=quote, safe=',')}"
        headers = {
            "User-Agent": "Finance-Open-Resource/1.0",
            "Accept": "application/json",
            "X-cb-user-key": api_key,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        http_request = Request(url, data=data, headers=headers, method=method)

        try:
            with urlopen(http_request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            with exc:
                message = _provider_message(exc.read() or b"")
            if exc.code in (401, 403):
                return dict(AUTH_ERROR)
            return {
                "error": f"Crunchbase returned HTTP {exc.code}: {message}",
                "error_type": "provider_error",
                "http_status": exc.code,
            }
        except (URLError, TimeoutError, OSError) as exc:
            return {"error": f"Could not reach Crunchbase: {exc}", "error_type": "provider_error"}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"error": "Crunchbase returned invalid JSON.", "error_type": "provider_error"}

        if not isinstance(payload, dict):
            return {"error": "Crunchbase returned an unexpected response.", "error_type": "provider_error"}
        return payload


_client = CrunchbaseClient()


def reset_quota_for_tests() -> None:
    """Reset the module client's rate-limit window for deterministic tests."""
    _client.reset_quota_for_tests()


def request(
    method: str,
    path: str,
    params: dict | None = None,
    body: dict | None = None,
    timeout: float = 20,
) -> dict:
    """Module-level request function used by server.py."""
    return _client.request(method, path, params, body, timeout)
