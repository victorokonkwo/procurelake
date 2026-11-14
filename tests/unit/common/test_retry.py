"""Backoff policy tests."""

from __future__ import annotations

import random

import pytest

from procurelake.common.retry import RetryPolicy


def test_delay_is_bounded_by_exponential_ceiling():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=60)
    rng = random.Random(0)
    for attempt, ceiling in [(1, 1), (2, 2), (3, 4), (4, 8), (10, 60)]:
        for _ in range(200):
            assert 0 <= policy.delay_before_retry(attempt, rng=rng) <= ceiling


def test_retry_after_wins_when_larger_but_is_capped():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=30)
    assert policy.delay_before_retry(1, retry_after_seconds=10, rng=random.Random(0)) == 10
    assert policy.delay_before_retry(1, retry_after_seconds=500, rng=random.Random(0)) == 30


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_seconds": 0},
        {"base_delay_seconds": 5, "max_delay_seconds": 1},
        {"min_delay_seconds": -1},
        {"min_delay_seconds": 61},
    ],
)
def test_invalid_policies_rejected(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


def test_min_delay_is_a_floor_under_the_jitter():
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=600, min_delay_seconds=30)
    rng = random.Random(0)
    for attempt in range(1, 8):
        for _ in range(100):
            assert 30 <= policy.delay_before_retry(attempt, rng=rng) <= max(30, min(600, 2 ** (attempt - 1)))
