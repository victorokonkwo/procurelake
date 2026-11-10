"""Ingestion manifests: the record of what was landed, from where, and whether it is complete.

The manifest is written **after** every raw file has landed, and it is the only signal that
downstream (Silver) jobs consume. Only batches with ``status == "succeeded"`` are processed. A
``failed`` manifest records why reconciliation failed. Its raw files stay in Bronze, unchanged,
for investigation.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from procurelake.ingestion.landing import LandingStore

MANIFEST_FORMAT_VERSION = "1"
MANIFEST_ROOT = "pipeline_manifests"


class LandedFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    uri: str
    size_bytes: int
    sha256: str
    record_count: int
    page: int | None = None


class CollectionWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    date_type: str
    start_date: date
    end_date: date


class Reconciliation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_count_before: int | None
    expected_count_after: int | None
    landed_record_count: int
    unique_record_count: int
    passed: bool
    detail: str


class IngestionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_format_version: str = MANIFEST_FORMAT_VERSION
    source_system: str
    dataset: str
    schema_version: str
    batch_id: str
    status: Literal["succeeded", "failed"]
    ingestion_started_at: datetime
    ingestion_completed_at: datetime
    window: CollectionWindow | None = None
    # True when the window had passed the source's settling period at collection time.
    # Defaults to False so manifests written before this field existed count as unsettled.
    window_settled: bool = False
    request: dict = Field(default_factory=dict)
    files: list[LandedFile] = Field(default_factory=list)
    reconciliation: Reconciliation | None = None
    code_version: str
    error: str | None = None

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True).encode() + b"\n"


def manifest_key(manifest: IngestionManifest) -> str:
    window = manifest.window.start_date.isoformat() if manifest.window else "none"
    return (
        f"{MANIFEST_ROOT}/{manifest.source_system}/{manifest.dataset}/"
        f"window_start={window}/batch_id={manifest.batch_id}/manifest.json"
    )


def write_manifest(store: LandingStore, manifest: IngestionManifest) -> str:
    key = manifest_key(manifest)
    store.put_new(key, manifest.to_json_bytes(), "application/json")
    return key


def load_manifests(store: LandingStore, source_system: str, dataset: str) -> list[IngestionManifest]:
    prefix = f"{MANIFEST_ROOT}/{source_system}/{dataset}/"
    return [
        IngestionManifest.model_validate_json(store.get(k))
        for k in store.list_keys(prefix)
        if k.endswith("/manifest.json")
    ]


def succeeded_windows(manifests: list[IngestionManifest]) -> set[date]:
    """Start dates of single-day windows with at least one succeeded batch collected after the
    window had settled. Unsettled batches stay in Bronze but do not mark a window complete."""
    return {
        m.window.start_date
        for m in manifests
        if m.status == "succeeded"
        and m.window_settled
        and m.window is not None
        and m.window.start_date == m.window.end_date
    }
