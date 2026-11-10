"""HTTP client retry behaviour against a local server."""

from __future__ import annotations

import socket

import pytest
from werkzeug import Response

from procurelake.common.retry import RetryPolicy
from procurelake.ingestion.http import HttpClient, HttpError


def _client(sleeps: list[float], attempts: int = 3) -> HttpClient:
    return HttpClient(
        retry_policy=RetryPolicy(max_attempts=attempts, base_delay_seconds=0.5, max_delay_seconds=30),
        sleep=sleeps.append,
        timeout_seconds=2,
    )


def test_returns_raw_body_bytes(httpserver):
    httpserver.expect_request("/ok", method="POST").respond_with_data(
        b'{"k": "v"}', content_type="application/json"
    )
    response = _client([]).post_json(httpserver.url_for("/ok"), {"x": 1})
    assert response.body == b'{"k": "v"}'
    assert response.json() == {"k": "v"}
    assert response.attempts == 1


def test_retries_429_honouring_retry_after(httpserver):
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] == 1:
            return Response("slow down", status=429, headers={"Retry-After": "7"})
        return Response("{}", content_type="application/json")

    httpserver.expect_request("/rl").respond_with_handler(handler)
    sleeps: list[float] = []
    response = _client(sleeps).get(httpserver.url_for("/rl"))
    assert response.attempts == 2
    assert sleeps == [7.0]


def test_permanent_4xx_is_not_retried(httpserver):
    httpserver.expect_request("/bad").respond_with_data("nope", status=422)
    sleeps: list[float] = []
    with pytest.raises(HttpError) as exc:
        _client(sleeps).get(httpserver.url_for("/bad"))
    assert exc.value.status == 422
    assert sleeps == []


def test_gives_up_after_max_attempts(httpserver):
    httpserver.expect_request("/down").respond_with_data("err", status=503)
    sleeps: list[float] = []
    with pytest.raises(HttpError, match="after 3 attempts"):
        _client(sleeps).get(httpserver.url_for("/down"))
    assert len(sleeps) == 2


def test_connection_errors_are_retried():
    with socket.socket() as s:  # find a free port with nothing listening
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sleeps: list[float] = []
    with pytest.raises(HttpError, match="ConnectionError"):
        _client(sleeps).get(f"http://127.0.0.1:{port}/")
    assert len(sleeps) == 2
