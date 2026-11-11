"""Batch selection is per source namespace (ADR 0034): bench batches never reach the dev job."""

from __future__ import annotations

import pytest

from procurelake.ingestion.manifest import IngestionManifest
from procurelake.lakehouse.silver.pipeline import select_snapshot_batch


def _m(source: str, batch: str, dataset: str) -> IngestionManifest:
    # model_construct: only the fields batch selection reads (no I/O, no full manifest needed).
    return IngestionManifest.model_construct(
        source_system=source, batch_id=batch, dataset=dataset, status="succeeded", window=None
    )


def test_latest_complete_batch_of_the_requested_source_only():
    manifests = [_m("postgres", "b1", t) for t in ("a", "b")] + [
        _m("postgres_bench", "b9", t) for t in ("a", "b")
    ]
    assert select_snapshot_batch(manifests, ["a", "b"]) == "b1"
    assert select_snapshot_batch(manifests, ["a", "b"], "postgres_bench") == "b9"
    with pytest.raises(LookupError):
        select_snapshot_batch(manifests[2:], ["a", "b"])  # only bench batches: dev finds none
