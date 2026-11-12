"""CSV snapshot connector: reconciliation, pinning, create-only landing (no AWS; moto for S3)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from procurelake.common.retry import RetryPolicy
from procurelake.ingestion.connectors import canada_contracts
from procurelake.ingestion.connectors.csv_snapshot import CsvSnapshotConnector, CsvSource, check_csv
from procurelake.ingestion.http import HttpClient
from procurelake.ingestion.landing import LocalLandingStore, ObjectAlreadyExistsError, S3LandingStore
from procurelake.ingestion.manifest import load_manifests

GOOD = b'\xef\xbb\xbfid,org,note\r\n1,a,"multi\r\nline, quoted"\r\n2,a,x\r\n1,b,y\r\n'
SOURCE = CsvSource(
    source_system="test_src", dataset="things", schema_version="test.v1", file_name="things.csv",
    expected_columns=("id", "org", "note"), key_columns=("org", "id"),
)  # fmt: skip
CLOCK = lambda: datetime(2026, 11, 12, 18, 0, tzinfo=UTC)  # noqa: E731


def _write(tmp_path: Path, data: bytes, name: str = "in.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _connector(tmp_path: Path) -> tuple[CsvSnapshotConnector, LocalLandingStore, LocalLandingStore]:
    raw, artifacts = LocalLandingStore(tmp_path / "raw"), LocalLandingStore(tmp_path / "artifacts")
    return CsvSnapshotConnector(raw, artifacts, clock=CLOCK, workdir=tmp_path), raw, artifacts


def test_check_counts_records_with_quoted_newlines_and_composite_keys(tmp_path):
    check = check_csv(_write(tmp_path, GOOD), SOURCE)
    assert check.header == ["id", "org", "note"]  # byte-order mark stripped
    assert (check.records, check.unique_keys, check.malformed_count, check.empty_keys) == (3, 3, 0, 0)


def test_local_file_lands_byte_for_byte_with_a_succeeded_manifest(tmp_path):
    connector, raw, artifacts = _connector(tmp_path)
    manifest = connector.ingest(SOURCE, local_path=_write(tmp_path, GOOD))
    assert manifest.status == "succeeded", manifest.reconciliation.detail
    (landed,) = manifest.files
    assert landed.key.startswith("bronze/test_src/things/ingestion_date=2026-11-12/batch_id=")
    assert raw.get(landed.key) == GOOD
    assert landed.sha256 == hashlib.sha256(GOOD).hexdigest() == manifest.request["sha256"]
    assert manifest.reconciliation.landed_record_count == 3
    assert [m.batch_id for m in load_manifests(artifacts, "test_src", "things")] == [manifest.batch_id]


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"id,org,note\n1,a,x\n1,a,y\n", "1 duplicate keys"),
        (b"id,org,note\n1,a\n", "wrong field count"),
        (b"id,org,note\n,a,x\n", "empty key"),
        (b"id,org,extra\n1,a,x\n", "header differs"),
    ],
)
def test_reconciliation_failures_fail_the_batch_but_keep_the_raw_file(tmp_path, data, reason):
    connector, raw, _ = _connector(tmp_path)
    manifest = connector.ingest(SOURCE, local_path=_write(tmp_path, data))
    assert manifest.status == "failed"
    assert reason in manifest.reconciliation.detail
    assert raw.get(manifest.files[0].key) == data  # Bronze keeps what arrived (ADR 0020)


def test_pinned_snapshot_must_match(tmp_path):
    connector, _, _ = _connector(tmp_path)
    path = _write(tmp_path, GOOD)
    ok = connector.ingest(SOURCE, local_path=path, expect_sha256=hashlib.sha256(GOOD).hexdigest().upper())
    assert ok.status == "succeeded"
    bad = connector.ingest(SOURCE, local_path=path, expect_sha256="0" * 64)
    assert bad.status == "failed" and "pinned snapshot" in bad.reconciliation.detail


def test_download_records_headers_and_checks_length(tmp_path, httpserver):
    httpserver.expect_request("/data/things.csv").respond_with_data(
        GOOD,
        content_type="text/csv",
        headers={"ETag": '"abc"', "Last-Modified": "Thu, 12 Nov 2026 06:56:19 GMT"},
    )
    connector, raw, _ = _connector(tmp_path)
    connector.http = HttpClient(retry_policy=RetryPolicy(max_attempts=1), timeout_seconds=5)
    source = CsvSource(**{**SOURCE.__dict__, "url": httpserver.url_for("/data/things.csv")})
    manifest = connector.ingest(source)
    assert manifest.status == "succeeded", manifest.error
    assert manifest.request["etag"] == '"abc"'
    assert manifest.request["content_length"] == len(GOOD)
    assert raw.get(manifest.files[0].key) == GOOD


def test_exactly_one_of_url_or_path(tmp_path):
    connector, _, _ = _connector(tmp_path)
    with pytest.raises(ValueError, match="exactly one"):
        connector.ingest(SOURCE)


def test_canada_source_contract_matches_the_measured_file_shape():
    src = canada_contracts.SOURCE
    assert len(src.expected_columns) == 43
    assert src.expected_columns[-2:] == ("owner_org", "owner_org_title")
    assert src.key_columns == ("owner_org", "reference_number")
    assert src.url == src.published_url and src.url.startswith("https://open.canada.ca/")


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="ca-central-1")
        client.create_bucket(Bucket="raw", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"})
        yield client


def test_s3_multipart_landing_is_create_only_and_reports_whole_file_sha(tmp_path, s3):
    data = bytes(range(256)) * (5 * 1024 * 1024 // 256 * 2 + 3)  # just over two 5 MiB parts
    path = _write(tmp_path, data, "big.csv")
    store = S3LandingStore(s3, "raw")
    import procurelake.ingestion.landing as landing

    original = landing.MULTIPART_THRESHOLD
    landing.MULTIPART_THRESHOLD = 1024  # force the multipart path on a small file
    try:
        stored = store.put_new_file("bronze/x/big.csv", path, "text/csv", part_size=5 * 1024 * 1024)
        assert stored.sha256_hex == hashlib.sha256(data).hexdigest()
        assert store.get("bronze/x/big.csv") == data
        with pytest.raises(ObjectAlreadyExistsError):
            store.put_new_file("bronze/x/big.csv", path, "text/csv", part_size=5 * 1024 * 1024)
        assert s3.list_multipart_uploads(Bucket="raw").get("Uploads", []) == []  # aborted
    finally:
        landing.MULTIPART_THRESHOLD = original


def test_s3_small_file_uses_single_put(tmp_path, s3):
    store = S3LandingStore(s3, "raw")
    stored = store.put_new_file("bronze/x/small.csv", _write(tmp_path, GOOD), "text/csv")
    assert stored.sha256_hex == hashlib.sha256(GOOD).hexdigest()
    assert store.get("bronze/x/small.csv") == GOOD
