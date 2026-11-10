"""Freshness measurement for the C5 gate (owner decision: 95% of source changes visible in
Silver and Gold within 15 minutes; ADR 0027).

A change's latency is the time from its source commit to the moment the layer that shows it
became available: the end of the CDC apply run for Silver, the end of ``dbt build`` for Gold.
Percentiles use the nearest-rank method (no interpolation), so every reported value is a
latency that was actually observed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

GATE_SECONDS = 15 * 60
GATE_PERCENTILE = 95


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("no values")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile / 100 * len(ordered))) - 1]


def latency_summary(commits: Sequence[datetime], available_at: datetime) -> dict:
    latencies = [(available_at - c).total_seconds() for c in commits]
    if any(x < 0 for x in latencies):
        raise ValueError("a change committed after the layer became available")
    p95 = nearest_rank(latencies, GATE_PERCENTILE)
    return {
        "changes": len(latencies),
        "min_s": round(min(latencies), 1),
        "p50_s": round(nearest_rank(latencies, 50), 1),
        "p95_s": round(p95, 1),
        "max_s": round(max(latencies), 1),
        "gate_s": GATE_SECONDS,
        "meets_gate": p95 <= GATE_SECONDS,
    }
