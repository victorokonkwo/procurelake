"""HTTP client with retries for public source APIs.

Retries connection errors, timeouts, HTTP 429, and HTTP 5xx with exponential backoff and full
jitter (``RetryPolicy``), honouring ``Retry-After`` when present. ``min_interval_seconds`` paces
requests: each one starts at least that long after the previous one started. Other 4xx responses are
permanent and raise immediately. Response bodies are returned as raw bytes so they can be
landed in Bronze exactly as received.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class DownloadResult:
    """A file streamed to disk, with what the server said about it."""

    path: Path
    size_bytes: int
    sha256: str
    content_length: int | None
    etag: str | None
    last_modified: str | None
    final_url: str
    attempts: int


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
        min_interval_seconds: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.retry_policy = retry_policy or RetryPolicy()
        self.timeout_seconds = timeout_seconds
        self._sleep = sleep
        self.min_interval_seconds = min_interval_seconds
        self._monotonic = monotonic
        self._last_start: float | None = None

    def _pace(self) -> None:
        """Wait until ``min_interval_seconds`` has passed since the previous request started."""
        if self.min_interval_seconds and self._last_start is not None:
            wait = self._last_start + self.min_interval_seconds - self._monotonic()
            if wait > 0:
                self._sleep(wait)
        self._last_start = self._monotonic()

    def post_json(self, url: str, payload: dict) -> HttpResponse:
        return self._request("POST", url, json_body=payload)

    def get(self, url: str) -> HttpResponse:
        return self._request("GET", url)

    def head(self, url: str) -> HttpResponse:
        """HEAD with redirects followed (requests does not follow them for HEAD by default)."""
        return self._request("HEAD", url)

    def download_to(self, url: str, dest: Path, chunk_bytes: int = 1024 * 1024) -> DownloadResult:
        """Stream ``url`` to ``dest`` (overwritten on each attempt), with the same retry rules.

        A body shorter or longer than ``Content-Length`` counts as a failed attempt.
        """
        policy = self.retry_policy
        last_error = ""
        for attempt in range(1, policy.max_attempts + 1):
            retry_after: float | None = None
            self._pace()
            try:
                with self.session.get(url, stream=True, timeout=self.timeout_seconds) as response:
                    if response.status_code >= 400 and response.status_code not in RETRYABLE_STATUS:
                        raise HttpError(
                            f"GET {url} failed with HTTP {response.status_code}", response.status_code
                        )
                    if response.status_code < 400:
                        digest, size = hashlib.sha256(), 0
                        with dest.open("wb") as handle:
                            for block in response.iter_content(chunk_bytes):
                                handle.write(block)
                                digest.update(block)
                                size += len(block)
                        length = response.headers.get("Content-Length")
                        expected = int(length) if length and length.isdigit() else None
                        if expected is None or size == expected:
                            return DownloadResult(
                                path=dest,
                                size_bytes=size,
                                sha256=digest.hexdigest(),
                                content_length=expected,
                                etag=response.headers.get("ETag"),
                                last_modified=response.headers.get("Last-Modified"),
                                final_url=response.url,
                                attempts=attempt,
                            )
                        last_error = f"body {size} bytes, Content-Length {expected}"
                    else:
                        last_error = f"HTTP {response.status_code}"
                        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            except (
                requests.ConnectionError,
                requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt == policy.max_attempts:
                break
            delay = policy.delay_before_retry(attempt, retry_after)
            logger.warning(
                "retrying GET %s after %s (attempt %d/%d)", url, last_error, attempt, policy.max_attempts
            )
            self._sleep(delay)
        raise HttpError(f"GET {url} failed after {policy.max_attempts} attempts: {last_error}")

    def _request(self, method: str, url: str, json_body: dict | None = None) -> HttpResponse:
        policy = self.retry_policy
        last_error: str = ""
        for attempt in range(1, policy.max_attempts + 1):
            retry_after: float | None = None
            self._pace()
            try:
                response = self.session.request(
                    method, url, json=json_body, timeout=self.timeout_seconds, allow_redirects=True
                )
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
