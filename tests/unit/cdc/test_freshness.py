from datetime import UTC, datetime, timedelta

import pytest

from procurelake.cdc.freshness import latency_summary, nearest_rank


def test_nearest_rank_returns_observed_values():
    values = list(range(1, 101))
    assert (nearest_rank(values, 95), nearest_rank(values, 50), nearest_rank(values, 100)) == (95, 50, 100)
    assert nearest_rank([7.0], 95) == 7.0
    assert nearest_rank([10, 20, 30, 40], 95) == 40  # ceil(0.95 * 4) = 4th value


def test_latency_summary_applies_the_15_minute_gate():
    t0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
    commits = [t0 + timedelta(seconds=i) for i in range(20)]
    ok = latency_summary(commits, t0 + timedelta(minutes=5))
    assert ok["meets_gate"] and ok["max_s"] == 300.0 and ok["p95_s"] == 300.0 - 1
    late = latency_summary(commits, t0 + timedelta(minutes=16))
    assert not late["meets_gate"]


def test_change_after_availability_is_an_error():
    t0 = datetime(2026, 11, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        latency_summary([t0 + timedelta(seconds=5)], t0)
