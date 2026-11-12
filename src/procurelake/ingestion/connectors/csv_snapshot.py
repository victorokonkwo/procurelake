"""Generic CSV snapshot connector: one published CSV file, landed byte for byte (ADR 0039).

A *snapshot* source publishes a whole file that is replaced in place (for example a
government open-data CSV). Each run lands the file exactly as served, so the Bronze copy is the
pinned snapshot later runs can be reproduced from. Flow:

1. **Fetch** to a local temporary file: streamed from an HTTPS URL with retries, or taken
   from a local path (an already pinned copy). The size is checked against ``Content-Length``,
   and the SHA-256, ``ETag`` and ``Last-Modified`` are recorded.
2. **Pin check** (optional): ``expect_sha256`` fails the batch if the file is not the expected
   snapshot. This is how a run is reproduced from a known download.
3. **Parse and reconcile** without transforming anything (RFC 4180, ``csv`` module; quoted
   newlines allowed):
   - the header must equal ``expected_columns`` when given;
   - every record must have exactly as many fields as the header;
   - when ``key_columns`` is given, no key may be empty and every key must be unique.
4. **Land** the original bytes in Bronze (create-only, multipart for large files), then write
   the manifest. The manifest is written for failed batches too, with the reason, and the raw
   file stays in Bronze for investigation (ADR 0020).
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from procurelake.ingestion.batch import code_version, new_batch_id
from procurelake.ingestion.http import HttpClient, HttpError
from procurelake.ingestion.landing import LandingStore, ObjectAlreadyExistsError, file_sha256_hex
from procurelake.ingestion.manifest import (
    IngestionManifest,
    LandedFile,
    Reconciliation,
    load_manifests,
    write_manifest,
)

logger = logging.getLogger(__name__)

# The csv module's default field limit (131,072 chars) is below some free-text comment fields.
FIELD_SIZE_LIMIT = 16 * 1024 * 1024


@dataclass(frozen=True)
class CsvSource:
    source_system: str
    dataset: str
    schema_version: str
    file_name: str
    url: str | None = None
    expected_columns: tuple[str, ...] | None = None
    key_columns: tuple[str, ...] = ()
    encoding: str = "utf-8-sig"
    license: str | None = None
    provenance_url: str | None = None
    # Where the source publishes the file. Kept when a pinned local copy is landed instead.
    published_url: str | None = None

    def __post_init__(self) -> None:
        for name in (self.source_system, self.dataset):
            if not name.replace("_", "").isalnum():
                raise ValueError(f"source_system and dataset must be [A-Za-z0-9_]: {name!r}")
        if "/" in self.file_name or not self.file_name:
            raise ValueError(f"file_name must be a plain file name: {self.file_name!r}")
        if self.expected_columns is not None and not set(self.key_columns) <= set(self.expected_columns):
            raise ValueError("key_columns must be among expected_columns")


@dataclass
class CsvCheck:
    header: list[str] = field(default_factory=list)
    records: int = 0
    malformed: list[int] = field(default_factory=list)  # 1-based record numbers, first 20
    malformed_count: int = 0
    empty_keys: int = 0
    unique_keys: int = 0


def check_csv(path: Path, source: CsvSource) -> CsvCheck:
    """Count records and check shape and keys. Keys are hashed, so memory stays bounded."""
    csv.field_size_limit(FIELD_SIZE_LIMIT)
    result = CsvCheck()
    seen: set[bytes] = set()
    with path.open("rb") as raw, io.TextIOWrapper(raw, encoding=source.encoding, newline="") as text:
        reader = csv.reader(text)
        result.header = next(reader, [])
        index = {name: i for i, name in enumerate(result.header)}
        missing = [c for c in source.key_columns if c not in index]
        if missing:
            raise ValueError(f"key columns not in header: {missing}")
        positions = [index[c] for c in source.key_columns]
        width = len(result.header)
        for number, row in enumerate(reader, start=1):
            result.records += 1
            if len(row) != width:
                result.malformed_count += 1
                if len(result.malformed) < 20:
                    result.malformed.append(number)
                continue
            if positions:
                key = [row[i] for i in positions]
                if any(not k.strip() for k in key):
                    result.empty_keys += 1
                    continue
                seen.add(hashlib.blake2b("\x1f".join(key).encode(), digest_size=16).digest())
    result.unique_keys = len(seen) if positions else result.records
    return result


def _verdict(check: CsvCheck, source: CsvSource, pin_ok: bool, length_ok: bool) -> tuple[bool, str]:
    if not length_ok:
        return False, "downloaded size does not match Content-Length"
    if not pin_ok:
        return False, "file is not the pinned snapshot (SHA-256 differs)"
    if source.expected_columns is not None and tuple(check.header) != source.expected_columns:
        extra = [c for c in check.header if c not in source.expected_columns]
        gone = [c for c in source.expected_columns if c not in check.header]
        return False, f"header differs from the contract (added {extra}, missing {gone}, or reordered)"
    if check.malformed_count:
        return False, f"{check.malformed_count} records have the wrong field count (first: {check.malformed})"
    if source.key_columns and check.empty_keys:
        return False, f"{check.empty_keys} records have an empty key {list(source.key_columns)}"
    if source.key_columns and check.unique_keys != check.records:
        dupes = check.records - check.unique_keys
        return False, f"{dupes} duplicate keys on {list(source.key_columns)}"
    keys = f"; {check.unique_keys} unique keys on {list(source.key_columns)}" if source.key_columns else ""
    return True, f"{check.records} records, every record has {len(check.header)} fields{keys}"


class CsvSnapshotConnector:
    def __init__(
        self,
        raw_store: LandingStore,
        manifest_store: LandingStore,
        http: HttpClient | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        workdir: Path | None = None,
    ):
        self.raw_store = raw_store
        self.manifest_store = manifest_store
        self.http = http or HttpClient(timeout_seconds=120)
        self.clock = clock
        self.workdir = workdir

    def unchanged_batch(self, source: CsvSource) -> str | None:
        """The latest succeeded batch if the published file still has its ETag and size.

        One HEAD request, so a scheduled run does not download and land an identical copy.
        Returns None when the source has no URL, no ETag, or anything differs.
        """
        if not source.url:
            return None
        done = [
            m
            for m in load_manifests(self.manifest_store, source.source_system, source.dataset)
            if m.status == "succeeded" and m.request.get("etag")
        ]
        if not done:
            return None
        latest = max(done, key=lambda m: m.batch_id)
        head = self.http.head(source.url).headers
        length = head.get("Content-Length")
        same_size = (
            length is not None and length.isdigit() and int(length) == latest.request.get("content_length")
        )
        return latest.batch_id if head.get("ETag") == latest.request["etag"] and same_size else None

    def ingest(
        self, source: CsvSource, local_path: Path | None = None, expect_sha256: str | None = None
    ) -> IngestionManifest:
        if (local_path is None) == (source.url is None):
            raise ValueError("give exactly one of a source URL or a local path")
        started = self.clock().replace(microsecond=0)
        batch_id = new_batch_id(started)
        key = (
            f"bronze/{source.source_system}/{source.dataset}/ingestion_date={started.date().isoformat()}"
            f"/batch_id={batch_id}/{source.file_name}"
        )
        request: dict = {
            "kind": "csv_snapshot",
            "url": source.url,
            "local_path": str(local_path) if local_path else None,
            "provenance_url": source.provenance_url,
            "published_url": source.published_url or source.url,
            "license": source.license,
            "expected_columns": list(source.expected_columns) if source.expected_columns else None,
            "key_columns": list(source.key_columns),
            "pinned_sha256": expect_sha256,
        }
        files: list[LandedFile] = []
        check = CsvCheck()
        error: str | None = None
        passed, detail = False, "run aborted before reconciliation completed"

        with tempfile.TemporaryDirectory(dir=self.workdir) as tmp:
            work = Path(tmp) / source.file_name
            try:
                length_ok = True
                if source.url:
                    download = self.http.download_to(source.url, work)
                    length_ok = download.content_length in (None, download.size_bytes)
                    sha = download.sha256
                    request |= {
                        "final_url": download.final_url,
                        "etag": download.etag,
                        "last_modified": download.last_modified,
                        "content_length": download.content_length,
                        "download_attempts": download.attempts,
                    }
                else:
                    shutil.copyfile(local_path, work)
                    sha = file_sha256_hex(work)
                request["sha256"] = sha
                pin_ok = expect_sha256 is None or expect_sha256.lower() == sha
                check = check_csv(work, source)
                passed, detail = _verdict(check, source, pin_ok, length_ok)
                stored = self.raw_store.put_new_file(key, work, "text/csv")
                if stored.sha256_hex != sha:
                    raise ValueError("landed file's SHA-256 differs from the fetched file")
                files.append(
                    LandedFile(
                        key=stored.key, uri=stored.uri, size_bytes=stored.size_bytes,
                        sha256=stored.sha256_hex, record_count=check.records,
                    )
                )  # fmt: skip
            except (
                HttpError,
                ObjectAlreadyExistsError,
                ValueError,
                UnicodeDecodeError,
                csv.Error,
                OSError,
            ) as exc:
                error = f"{type(exc).__name__}: {exc}"
                passed = False

        manifest = IngestionManifest(
            source_system=source.source_system,
            dataset=source.dataset,
            schema_version=source.schema_version,
            batch_id=batch_id,
            status="succeeded" if passed and error is None else "failed",
            ingestion_started_at=started,
            ingestion_completed_at=self.clock().replace(microsecond=0),
            window=None,
            window_settled=False,
            request=request | {"header": check.header, "malformed_records": check.malformed},
            files=files,
            reconciliation=Reconciliation(
                expected_count_before=None,
                expected_count_after=None,
                landed_record_count=check.records,
                unique_record_count=check.unique_keys,
                passed=passed and error is None,
                detail=detail if error is None else "run aborted before reconciliation completed",
            ),
            code_version=code_version(),
            error=error,
        )
        manifest_key = write_manifest(self.manifest_store, manifest)
        logger.info("batch %s %s: %s (%s)", batch_id, manifest.status, manifest_key, detail)
        return manifest
