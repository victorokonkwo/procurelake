"""CDC apply CloudWatch metrics (procurelake.cdc.metrics; Week 8)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from procurelake.cdc.apply import CdcApplyResult
from procurelake.cdc.metrics import NAMESPACE, metric_data, publish

T0 = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)


def result(status="applied", events=10, rejected=1, held=1, oldest=None) -> CdcApplyResult:
    return CdcApplyResult("silver_invoices", "b1", status, 2, events, rejected, held, 9, 0, "s", None, oldest)


def by_name(data: list[dict]) -> dict[str, dict]:
    return {d["MetricName"]: d for d in data}


def test_applied_batch_reports_counts_and_latency_from_oldest_commit():
    oldest = T0 - timedelta(seconds=150)
    data = by_name(metric_data([(result(oldest=oldest), T0)], "dev"))
    assert (
        data["SourceToSilverSeconds"]["Value"] == 150.0 and data["SourceToSilverSeconds"]["Unit"] == "Seconds"
    )
    assert (data["AppliedEvents"]["Value"], data["RejectedEvents"]["Value"], data["HeldKeys"]["Value"]) == (
        10,
        1,
        1,
    )
    assert data["BlockedBatches"]["Value"] == 0
    assert data["AppliedEvents"]["Dimensions"] == [
        {"Name": "Environment", "Value": "dev"},
        {"Name": "Table", "Value": "silver_invoices"},
    ]


def test_full_load_batch_has_no_latency():
    names = by_name(metric_data([(result(oldest=None), T0)], "dev"))
    assert "SourceToSilverSeconds" not in names and "AppliedEvents" in names


def test_blocked_batch_counts_but_no_latency():
    names = by_name(metric_data([(result(status="blocked", oldest=T0), T0)], "dev"))
    assert names["BlockedBatches"]["Value"] == 1 and "SourceToSilverSeconds" not in names


def test_empty_tables_publish_nothing():
    assert metric_data([(result(status="empty"), T0)], "dev") == []


def test_publish_batches_by_1000():
    calls = []

    class FakeCloudWatch:  # test double
        def put_metric_data(self, Namespace, MetricData):
            calls.append((Namespace, len(MetricData)))

    data = [{"MetricName": "AppliedEvents"}] * 2500
    assert publish(FakeCloudWatch(), data) == 2500
    assert calls == [(NAMESPACE, 1000), (NAMESPACE, 1000), (NAMESPACE, 500)]


def test_naive_timestamps_are_refused():
    import pytest

    with pytest.raises(ValueError, match="naive"):
        metric_data([(result(oldest=T0.replace(tzinfo=None)), T0)], "dev")
