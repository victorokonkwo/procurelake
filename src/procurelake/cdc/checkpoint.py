"""DynamoDB checkpoints and per-table writer leases for CDC apply (ADR 0028).

One DynamoDB table, keyed by ``pk`` (the Silver table) and ``sk``:

* ``sk = "lease"``: the current writer. Taken with a conditional write that succeeds only if
  there is no lease, the lease has expired, or the caller already holds it, so two writers can
  never apply to the same Silver table at once (open question 15, owner decision).
* ``sk = "file#<bronze key>"``: one item per Bronze CDC file that has been applied.
* ``sk = "checkpoint"``: the last successful apply (batch, sequence, file count).

File and checkpoint items are written **only after** the Iceberg write and its validation have
succeeded (AGENTS.md section 2.5). If a run dies before that, the next run re-reads the same
files; the apply is idempotent, so the replay changes nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from botocore.exceptions import ClientError

LEASE_SK = "lease"
CHECKPOINT_SK = "checkpoint"
FILE_PREFIX = "file#"


class LeaseHeldError(RuntimeError):
    """Another writer holds an unexpired lease on the table."""


@dataclass(frozen=True)
class Lease:
    table: str
    owner: str
    expires_at: int


class CheckpointStore:
    def __init__(self, dynamodb_client, table_name: str, clock=time.time):
        self._ddb = dynamodb_client
        self._table = table_name
        self._clock = clock

    # ------------------------------------------------------------------ lease
    def acquire_lease(self, silver_table: str, owner: str, ttl_seconds: int = 1800) -> Lease:
        now = int(self._clock())
        expires = now + ttl_seconds
        try:
            self._ddb.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": silver_table},
                    "sk": {"S": LEASE_SK},
                    "owner": {"S": owner},
                    "expires_at": {"N": str(expires)},
                },
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now OR #o = :owner",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":now": {"N": str(now)}, ":owner": {"S": owner}},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise LeaseHeldError(f"{silver_table}: another writer holds the lease") from exc
            raise
        return Lease(silver_table, owner, expires)

    def release_lease(self, lease: Lease) -> None:
        try:
            self._ddb.delete_item(
                TableName=self._table,
                Key={"pk": {"S": lease.table}, "sk": {"S": LEASE_SK}},
                ConditionExpression="#o = :owner",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":owner": {"S": lease.owner}},
            )
        except ClientError as exc:
            # The lease expired and was taken over: nothing of ours to release.
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    # ------------------------------------------------------------------ files and checkpoint
    def applied_files(self, silver_table: str) -> set[str]:
        keys: set[str] = set()
        paginator = self._ddb.get_paginator("query")
        for page in paginator.paginate(
            TableName=self._table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={":pk": {"S": silver_table}, ":prefix": {"S": FILE_PREFIX}},
            ProjectionExpression="sk",
        ):
            keys.update(item["sk"]["S"].removeprefix(FILE_PREFIX) for item in page["Items"])
        return keys

    def record_applied(
        self, lease: Lease, files: list[str], batch_id: str, last_sequence: str | None, events: int
    ) -> None:
        """Record ``files`` as applied and advance the checkpoint (call only after validation).

        Every write is a DynamoDB transaction that also checks this writer still holds an
        unexpired lease, so a writer whose lease was taken over cannot advance the checkpoint.
        Files go first and the checkpoint last; a crash in between only means the same files are
        re-read next time, and the idempotent apply makes that a no-op.
        """
        now = int(self._clock())
        stamp = datetime.now(UTC).isoformat()
        lease_check = {
            "ConditionCheck": {
                "TableName": self._table,
                "Key": {"pk": {"S": lease.table}, "sk": {"S": LEASE_SK}},
                "ConditionExpression": "#o = :owner AND expires_at >= :now",
                "ExpressionAttributeNames": {"#o": "owner"},
                "ExpressionAttributeValues": {":owner": {"S": lease.owner}, ":now": {"N": str(now)}},
            }
        }
        puts = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        "pk": {"S": lease.table},
                        "sk": {"S": f"{FILE_PREFIX}{key}"},
                        "batch_id": {"S": batch_id},
                        "applied_at": {"S": stamp},
                    },
                }
            }
            for key in files
        ]
        puts.append(
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        "pk": {"S": lease.table},
                        "sk": {"S": CHECKPOINT_SK},
                        "batch_id": {"S": batch_id},
                        "last_sequence": {"S": last_sequence or ""},
                        "files_in_batch": {"N": str(len(files))},
                        "events_in_batch": {"N": str(events)},
                        "updated_at": {"S": stamp},
                    },
                }
            }
        )
        chunk = 99  # TransactWriteItems allows 100 actions; one is the lease check
        try:
            for start in range(0, len(puts), chunk):
                self._ddb.transact_write_items(TransactItems=[lease_check, *puts[start : start + chunk]])
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise LeaseHeldError(f"{lease.table}: lease lost before the checkpoint was written") from exc
            raise

    def checkpoint(self, silver_table: str) -> dict | None:
        item = self._ddb.get_item(
            TableName=self._table, Key={"pk": {"S": silver_table}, "sk": {"S": CHECKPOINT_SK}}
        ).get("Item")
        if not item:
            return None
        return {k: next(iter(v.values())) for k, v in item.items()}
