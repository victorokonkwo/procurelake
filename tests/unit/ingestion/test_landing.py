"""Landing-store tests: create-only semantics and checksums (local FS and S3 via moto)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from procurelake.ingestion.landing import (
    LocalLandingStore,
    ObjectAlreadyExistsError,
    S3LandingStore,
    sha256_hex,
)


def test_local_store_writes_once_and_never_overwrites(tmp_path):
    store = LocalLandingStore(tmp_path)
    stored = store.put_new("bronze/x/page-00001.json", b'{"a":1}', "application/json")
    assert stored.sha256_hex == sha256_hex(b'{"a":1}')
    with pytest.raises(ObjectAlreadyExistsError):
        store.put_new("bronze/x/page-00001.json", b'{"a":2}', "application/json")
    assert store.get("bronze/x/page-00001.json") == b'{"a":1}'


def test_local_store_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        LocalLandingStore(tmp_path / "root").put_new("../escape.json", b"x", "application/json")


def test_local_store_lists_by_prefix(tmp_path):
    store = LocalLandingStore(tmp_path)
    store.put_new("a/1.json", b"1", "application/json")
    store.put_new("a/2.json", b"2", "application/json")
    store.put_new("b/1.json", b"3", "application/json")
    assert store.list_keys("a/") == ["a/1.json", "a/2.json"]


@pytest.fixture
def s3_store():
    with mock_aws():
        client = boto3.client("s3", region_name="ca-central-1")
        client.create_bucket(
            Bucket="test-raw", CreateBucketConfiguration={"LocationConstraint": "ca-central-1"}
        )
        yield S3LandingStore(client, "test-raw"), client


def test_s3_store_puts_with_sha256_checksum(s3_store):
    store, client = s3_store
    stored = store.put_new("bronze/x/page-00001.json", b'{"a":1}', "application/json")
    assert stored.uri == "s3://test-raw/bronze/x/page-00001.json"
    head = client.head_object(Bucket="test-raw", Key="bronze/x/page-00001.json", ChecksumMode="ENABLED")
    assert head["ContentType"] == "application/json"
    assert "ChecksumSHA256" in head
    assert store.get("bronze/x/page-00001.json") == b'{"a":1}'
    assert store.list_keys("bronze/") == ["bronze/x/page-00001.json"]


def test_s3_store_refuses_to_overwrite(s3_store):
    store, _ = s3_store
    store.put_new("bronze/x/page-00001.json", b'{"a":1}', "application/json")
    with pytest.raises(ObjectAlreadyExistsError):
        store.put_new("bronze/x/page-00001.json", b'{"a":2}', "application/json")
    assert store.get("bronze/x/page-00001.json") == b'{"a":1}'
