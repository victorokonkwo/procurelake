"""CloudWatch metrics for CDC apply (Week 8 monitoring, implementation plan E).

Namespace ``ProcureLake/CDC``, dimensions ``Environment`` and ``Table`` (Silver table). Per
applied batch:

* ``AppliedEvents``: change events read (Count)
* ``RejectedEvents``: events quarantined with a reason (Count)
* ``HeldKeys``: keys held at their previous version because their latest event was rejected
* ``BlockedBatches``: 1 when the quality gate blocked the batch
* ``SourceToSilverSeconds``: from the oldest CDC event's **source commit** to the moment the
  batch was committed to Silver. It is the freshness SLI's first leg (ADR 0027 gate: 15 minutes
  to Gold), measured on the worst event of the batch. Full-load batches have no commit times
  and publish no latency.

Nothing is published for a table with no new files, so a quiet source and a stalled DMS task
look the same here. The DMS alarms in dev/cdc catch a stall.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from procurelake.cdc.apply import CdcApplyResult

NAMESPACE = "ProcureLake/CDC"
_BATCH_SIZE = 1000  # PutMetricData accepts up to 1,000 metrics per call


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:  # apply_events returns aware UTC times; refuse to guess otherwise
        raise ValueError(f"naive timestamp {ts!r}: pass timezone-aware datetimes")
    return ts.astimezone(UTC)


def metric_data(results: Iterable[tuple[CdcApplyResult, datetime]], environment: str) -> list[dict]:
    """PutMetricData entries for (result, time the batch was committed to Silver) pairs."""
    data: list[dict] = []
    for result, committed_at in results:
        if result.status == "empty":
            continue
        dims = [{"Name": "Environment", "Value": environment}, {"Name": "Table", "Value": result.table}]
        at = _utc(committed_at)

        def add(name: str, value: float, unit: str, dims=dims, at=at) -> None:
            data.append(
                {"MetricName": name, "Dimensions": dims, "Timestamp": at, "Value": value, "Unit": unit}
            )

        add("AppliedEvents", result.events, "Count")
        add("RejectedEvents", result.rejected_events, "Count")
        add("HeldKeys", result.held_keys, "Count")
        add("BlockedBatches", 1 if result.status == "blocked" else 0, "Count")
        if result.status == "applied" and result.oldest_cdc_commit_ts is not None:
            latency = (at - _utc(result.oldest_cdc_commit_ts)).total_seconds()
            add("SourceToSilverSeconds", max(0.0, latency), "Seconds")
    return data


def publish(cloudwatch, data: list[dict]) -> int:
    """Send ``data`` to CloudWatch in batches; returns the number of metrics sent."""
    for start in range(0, len(data), _BATCH_SIZE):
        cloudwatch.put_metric_data(Namespace=NAMESPACE, MetricData=data[start : start + _BATCH_SIZE])
    return len(data)
