"""CheckpointStore against moto's DynamoDB (test double; no AWS calls)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from procurelake.cdc.checkpoint import CheckpointStore, LeaseHeldError

TABLE = "procurelake-dev-cdc-checkpoints"


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")
        client.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


def test_lease_excludes_a_second_writer_until_released_or_expired(ddb):
    clock = Clock()
    a, b = CheckpointStore(ddb, TABLE, clock), CheckpointStore(ddb, TABLE, clock)
    lease = a.acquire_lease("silver_awards", "writer-a", ttl_seconds=60)
    with pytest.raises(LeaseHeldError):
        b.acquire_lease("silver_awards", "writer-b")
    b.acquire_lease("silver_invoices", "writer-b")  # other tables are independent
    assert a.acquire_lease("silver_awards", "writer-a").owner == "writer-a"  # re-entrant
    a.release_lease(lease)
    b.acquire_lease("silver_awards", "writer-b", ttl_seconds=60)
    clock.t += 61
    a.acquire_lease("silver_awards", "writer-a")  # expired lease can be taken over


def test_checkpoint_advances_only_with_a_valid_lease(ddb):
    clock = Clock()
    store = CheckpointStore(ddb, TABLE, clock)
    lease = store.acquire_lease("silver_awards", "writer-a", ttl_seconds=60)
    files = [f"bronze/postgres_cdc/load_id=x/procurement/awards/2026{i:04d}.parquet" for i in range(150)]
    store.record_applied(lease, files, "20261110T000000Z-aaaaaaaa", "seq-9", events=300)
    assert store.applied_files("silver_awards") == set(files)  # > 99 files: several transactions
    assert store.checkpoint("silver_awards")["last_sequence"] == "seq-9"

    clock.t += 61
    CheckpointStore(ddb, TABLE, clock).acquire_lease("silver_awards", "writer-b")
    with pytest.raises(LeaseHeldError):
        store.record_applied(lease, ["late.parquet"], "20261110T000001Z-bbbbbbbb", "seq-10", events=1)
    assert "late.parquet" not in store.applied_files("silver_awards")
    assert store.checkpoint("silver_awards")["last_sequence"] == "seq-9"


def test_release_after_takeover_is_harmless(ddb):
    clock = Clock()
    store = CheckpointStore(ddb, TABLE, clock)
    lease = store.acquire_lease("silver_awards", "writer-a", ttl_seconds=10)
    clock.t += 11
    store.acquire_lease("silver_awards", "writer-b")
    store.release_lease(lease)  # does not remove writer-b's lease
    with pytest.raises(LeaseHeldError):
        store.acquire_lease("silver_awards", "writer-c")
