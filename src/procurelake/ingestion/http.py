"""HTTP client with retries for public source APIs.

Retries connection errors, timeouts, HTTP 429, and HTTP 5xx with exponential backoff and full
jitter (``RetryPolicy``), honouring ``Retry-After`` when present. Other 4xx responses are
permanent and raise immediately. Response bodies are returned as raw bytes so they can be
landed in Bronze exactly as received.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from procurelake.common.retry import RetryPolicy

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "procurelake-ingestion/0.1 (+https://github.com/victorokonkwo/procurelake)"


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: bytes = b""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)
    attempts: int = 1

    def json(self) -> object:
        return json.loads(self.body)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form is not used by our sources; fall back to backoff


class HttpClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_seconds: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep

    def post_json(self, url: str, payload: dict) -> HttpResponse:
        return self._request("POST", url, json_body=payload)

    def get(self, url: str) -> HttpResponse:
        return self._request("GET", url)

    def _request(self, method: str, url: str, json_body: dict | None = None) -> HttpResponse:
        policy = self.retry_policy
        last_error: str = ""
        for attempt in range(1, policy.max_attempts + 1):
            retry_after: float | None = None
            try:
                response = self.session.request(method, url, json=json_body, timeout=self.timeout_seconds)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code < 400:
                    return HttpResponse(
                        status=response.status_code,
                        body=response.content,
                        headers=dict(response.headers),
                        attempts=attempt,
                    )
                if response.status_code not in RETRYABLE_STATUS:
                    raise HttpError(
                        f"{method} {url} failed with HTTP {response.status_code}",
                        status=response.status_code,
                        body=response.content,
                    )
                last_error = f"HTTP {response.status_code}"
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))

            if attempt == policy.max_attempts:
                break
            delay = policy.delay_before_retry(attempt, retry_after)
            logger.warning(
                "retrying %s %s after %s (attempt %d/%d, sleeping %.1fs)",
                method,
                url,
                last_error,
                attempt,
                policy.max_attempts,
                delay,
            )
            self._sleep(delay)

        raise HttpError(f"{method} {url} failed after {policy.max_attempts} attempts: {last_error}")
