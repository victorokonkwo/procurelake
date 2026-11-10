"""CDC apply into Silver Iceberg (local Spark 3.5.6 + Iceberg 1.10.0; DynamoDB via moto).

Fixtures are Parquet files with the schema AWS DMS wrote in dev on 2026-11-10 (ADR 0027,
docs/evidence/sprint1-week5-cdc-bronze-live.md): ``Op`` string, ``_dms_commit_ts`` string,
then the source columns typed (uuid as string, numeric as decimal, timestamptz as UTC
timestamp). Deletes carry only the primary key, as DMS produced. File names follow DMS:
``LOAD00000001.parquet`` for the full load, ``YYYYMMDD-HHMMSSmmm.parquet`` for CDC.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from procurelake.cdc.apply import active_rows, apply_events
from procurelake.cdc.checkpoint import CheckpointStore
from procurelake.cdc.job import run_cdc_table
from procurelake.contracts.source import load_contracts
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.lakehouse.silver.spec import load_specs
from procurelake.lakehouse.silver.writer import CdcOwnedTableError, apply_snapshot

pytestmark = pytest.mark.spark

REPO = Path(__file__).resolve().parents[3]
SPECS = load_specs(load_contracts(REPO / "contracts" / "source"), REPO / "configs" / "pipelines" / "silver")
SPEC = SPECS["invoices"]
DMS_SCHEMA = (
    "Op string, _dms_commit_ts string, invoice_id string, tenant_id string, po_id string, "
    "invoice_date date, invoice_amount decimal(18,2), paid_amount decimal(18,2), currency_code string, "
    "payment_status string, paid_at timestamp, created_at timestamp, updated_at timestamp"
)
PO = "00000000-0000-4000-8000-0000000000p1".replace("p", "a")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _inv(op, ts, invoice_id, status="pending", amount="100.00", paid="0.00", tenant="tenant_alder"):
    if op == "D":
        return (op, ts, invoice_id, *([None] * 10))
    paid_at = T0 if status in ("paid", "partially_paid") else None
    return (
        op,
        ts,
        invoice_id,
        tenant,
        PO,
        T0.date(),
        Decimal(amount),
        Decimal(paid),
        "CAD",
        status,
        paid_at,
        T0,
        T0,
    )


class Bronze:
    """DMS-shaped files under <root>/bronze/postgres_cdc/load_id=<id>/procurement/invoices/."""

    def __init__(self, spark, root: Path):
        self.spark, self.root = spark, root
        self.store = LocalLandingStore(root)

    def write(self, load_id: str, name: str, rows: list[tuple]) -> str:
        key = f"bronze/postgres_cdc/load_id={load_id}/procurement/invoices/{name}"
        target = self.root / key
        tmp = self.root / f"_tmp_{uuid.uuid4().hex}"
        self.spark.createDataFrame(rows, DMS_SCHEMA).coalesce(1).write.parquet(str(tmp))
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(next(tmp.glob("part-*.parquet"))), target)
        shutil.rmtree(tmp)
        return key

    def uri(self, key: str) -> str:
        return self.store.uri_for(key)


@pytest.fixture
def env(spark, tmp_path):
    ns = f"lake.cdc_{tmp_path.name.replace('-', '_')[-20:]}"
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {ns}")
    return {
        "bronze": Bronze(spark, tmp_path / "raw"),
        "table": f"{ns}.silver_invoices",
        "q": (tmp_path / "q").as_uri(),
    }


def _apply(spark, env, keys, batch, **kw):
    return apply_events(
        spark, SPEC, env["table"], [env["bronze"].uri(k) for k in keys], batch, env["q"], **kw
    )


def _state(spark, env):
    rows = spark.table(env["table"]).collect()
    return {r.invoice_id: r for r in rows}


A, B, C, D = (f"00000000-0000-4000-8000-00000000000{i}" for i in range(1, 5))
LOAD_TS = "2026-11-10 13:39:03.233521"


def test_full_load_then_inserts_updates_and_deletes(spark, env):
    b = env["bronze"]
    load = b.write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A), _inv("I", LOAD_TS, B)])
    cdc = b.write(
        "20261110T130454Z",
        "20261110-134234825.parquet",
        [
            _inv("U", "2026-11-10 13:41:34.820217", A, "paid", paid="100.00"),
            _inv("I", "2026-11-10 13:41:35.100000", C),
            _inv("D", "2026-11-10 13:41:35.804219", B),
        ],
    )
    first = _apply(spark, env, [load], "20261110T134000Z-00000001")
    second = _apply(spark, env, [cdc], "20261110T134300Z-00000002")
    assert (first.status, first.applied_keys, second.applied_keys) == ("applied", 2, 3)
    # Freshness metric input (Week 8): full-load rows carry no commit time; CDC rows do.
    assert first.oldest_cdc_commit_ts is None
    assert second.oldest_cdc_commit_ts == datetime(2026, 11, 10, 13, 41, 34, 820217, tzinfo=UTC)

    state = _state(spark, env)
    assert state[A].payment_status == "paid" and state[A].paid_amount == Decimal("100.00")
    assert state[A]._cdc_op == "U" and not state[A]._is_deleted
    assert state[B]._is_deleted and state[B].tenant_id == "tenant_alder"  # tombstone keeps the row
    assert state[C]._cdc_op == "I"
    assert {r.invoice_id for r in active_rows(spark.table(env["table"])).collect()} == {A, C}


def test_replaying_and_reordering_files_changes_nothing(spark, env):
    b = env["bronze"]
    older = b.write(
        "20261110T130454Z", "20261110-134200000.parquet", [_inv("I", "2026-11-10 13:41:00.000001", A)]
    )
    newer = b.write(
        "20261110T130454Z",
        "20261110-134300000.parquet",
        [_inv("U", "2026-11-10 13:42:00.000001", A, "cancelled"), _inv("D", "2026-11-10 13:42:30.000000", A)],
    )
    _apply(spark, env, [newer], "20261110T134500Z-00000001")  # newer file first (out of order)
    after_newer = _state(spark, env)[A]
    assert after_newer._is_deleted

    _apply(spark, env, [older], "20261110T134600Z-00000002")  # the older insert must not resurrect A
    late = _state(spark, env)[A]
    assert late._is_deleted and late._source_sequence == after_newer._source_sequence
    assert late._batch_id == "20261110T134500Z-00000001"  # the older event changed nothing

    _apply(spark, env, [older, newer], "20261110T134700Z-00000003")  # full replay
    state = _state(spark, env)
    assert len(state) == 1 and state[A]._is_deleted
    assert state[A]._source_sequence.startswith("20261110134230")
    assert state[A]._batch_id == "20261110T134500Z-00000001"  # replay rewrote nothing


def test_same_transaction_changes_are_ordered_by_row_position(spark, env):
    ts = "2026-11-10 13:41:34.000000"  # one transaction updating the same row twice
    key = env["bronze"].write(
        "20261110T130454Z",
        "20261110-134234825.parquet",
        [_inv("I", ts, A), _inv("U", ts, A, "partially_paid", paid="40.00")],
    )
    _apply(spark, env, [key], "20261110T134500Z-00000001")
    assert _state(spark, env)[A].payment_status == "partially_paid"


def test_rejected_latest_event_holds_the_previous_version(spark, env):
    b = env["bronze"]
    ok = b.write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A, amount="50.00")])
    _apply(spark, env, [ok], "20261110T134000Z-00000001")
    bad = b.write(
        "20261110T130454Z",
        "20261110-134300000.parquet",
        [
            _inv("U", "2026-11-10 13:42:00.000000", A, "paid", amount="50.00", paid="50.00"),
            _inv("U", "2026-11-10 13:42:01.000000", A, "paid", amount="50.00", paid="20.00"),  # breaks a rule
        ]
        + [
            _inv("I", "2026-11-10 13:42:02.000000", f"00000000-0000-4000-8000-0000000001{i:02d}")
            for i in range(99)
        ],
    )
    result = _apply(spark, env, [bad], "20261110T134300Z-00000002")
    assert (result.status, result.rejected_events, result.held_keys) == ("applied", 1, 1)
    held = _state(spark, env)[A]
    assert held.payment_status == "pending" and held.paid_amount == Decimal("0.00")  # not the older 'paid'
    quarantined = spark.read.json(result.quarantine_uri).collect()
    assert [(q.key_invoice_id, q.reasons) for q in quarantined] == [(A, ["rule:payment_state_consistent"])]


def test_quality_gate_blocks_and_writes_nothing(spark, env):
    key = env["bronze"].write(
        "20261110T130454Z",
        "LOAD00000001.parquet",
        [_inv("I", LOAD_TS, A), _inv("I", LOAD_TS, B, amount="-1.00")],  # 50% rejected > 1%
    )
    result = _apply(spark, env, [key], "20261110T134000Z-00000001")
    assert result.status == "blocked" and not spark.catalog.tableExists(env["table"])


def test_complete_full_load_tombstones_keys_missing_from_it(spark, env):
    b = env["bronze"]
    first = b.write(
        "20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A), _inv("I", LOAD_TS, B)]
    )
    extra = b.write(
        "20261110T130454Z", "20261110-134200000.parquet", [_inv("I", "2026-11-10 13:41:00.000000", C)]
    )
    _apply(spark, env, [first], "20261110T134000Z-00000001", full_load_complete=True)
    _apply(spark, env, [extra], "20261110T134200Z-00000002")
    # Next session: the source was re-seeded, C no longer exists; a fresh full load has A, B.
    again = b.write(
        "20261111T090000Z",
        "LOAD00000001.parquet",
        [_inv("I", "2026-11-11 09:01:00.000000", k) for k in (A, B)],
    )
    result = _apply(spark, env, [again], "20261111T090500Z-00000003", full_load_complete=True)
    assert result.full_load_tombstones == 1
    state = _state(spark, env)
    assert state[C]._is_deleted and not state[A]._is_deleted and not state[B]._is_deleted


def test_snapshot_apply_is_refused_on_a_cdc_owned_table(spark, env):
    key = env["bronze"].write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A)])
    _apply(spark, env, [key], "20261110T134000Z-00000001")
    empty = spark.createDataFrame([], "invoice_id string")
    with pytest.raises(CdcOwnedTableError):
        apply_snapshot(
            spark, SPEC, env["table"], "20261110T140000Z-00000002", spark.table(env["table"]), empty
        )


# --------------------------------------------------------------------------- job: lease + checkpoint
@pytest.fixture
def checkpoints():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="ca-central-1")
        ddb.create_table(
            TableName="cp",
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
        yield CheckpointStore(ddb, "cp")


def _job(spark, env, checkpoints, **kw):
    return run_cdc_table(
        spark,
        SPEC,
        env["bronze"].store,
        "bronze/postgres_cdc",
        checkpoints,
        env["table"],
        env["q"],
        "it",
        **kw,
    )


def test_job_applies_only_new_files_and_advances_the_checkpoint(spark, env, checkpoints):
    b = env["bronze"]
    b.write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A)])
    [first] = _job(spark, env, checkpoints)
    assert first.files == 1 and checkpoints.checkpoint("silver_invoices")["batch_id"] == first.batch_id
    assert _job(spark, env, checkpoints) == []  # nothing new
    b.write(
        "20261110T130454Z", "20261110-134234825.parquet", [_inv("U", "2026-11-10 13:41:34.0", A, "cancelled")]
    )
    [second] = _job(spark, env, checkpoints)
    assert second.files == 1 and _state(spark, env)[A].payment_status == "cancelled"


def test_crash_after_merge_before_checkpoint_replays_safely(spark, env, checkpoints, monkeypatch):
    """C4: the input was read and merged, then the run died before recording the checkpoint."""
    b = env["bronze"]
    b.write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A), _inv("I", LOAD_TS, B)])
    b.write(
        "20261110T130454Z",
        "20261110-134234825.parquet",
        [_inv("D", "2026-11-10 13:41:35.0", B), _inv("I", "2026-11-10 13:41:36.0", C)],
    )

    def die(*_args, **_kwargs):
        raise RuntimeError("simulated crash after the Iceberg write")

    monkeypatch.setattr(checkpoints, "record_applied", die)
    with pytest.raises(RuntimeError, match="simulated crash"):
        _job(spark, env, checkpoints)
    assert (
        checkpoints.checkpoint("silver_invoices") is None
        and checkpoints.applied_files("silver_invoices") == set()
    )
    merged = _state(spark, env)
    monkeypatch.undo()

    [retry] = _job(spark, env, checkpoints)  # the lease was released by the failed run
    after = _state(spark, env)
    assert retry.status == "applied" and retry.files == 2
    assert {k: (r._source_sequence, r._is_deleted) for k, r in after.items()} == {
        k: (r._source_sequence, r._is_deleted) for k, r in merged.items()
    }
    active = active_rows(spark.table(env["table"]))
    assert sorted(r.invoice_id for r in active.collect()) == [A, C]  # no duplicate, B stays deleted
    assert len(checkpoints.applied_files("silver_invoices")) == 2


def test_job_defers_a_load_whose_full_load_is_still_running(spark, env, checkpoints):
    """Regression (found in review before the scheduled session): a run during the DMS full
    load must not record the load as applied without the complete-load tombstoning."""
    b = env["bronze"]
    b.write("20261110T130454Z", "LOAD00000001.parquet", [_inv("I", LOAD_TS, A), _inv("I", LOAD_TS, C)])
    _job(spark, env, checkpoints, complete_full_loads=frozenset({"20261110T130454Z"}))
    # New session: re-seeded source without C; its full load has landed but is not complete yet.
    b.write("20261111T090000Z", "LOAD00000001.parquet", [_inv("I", "2026-11-11 09:01:00.000000", A)])
    assert _job(spark, env, checkpoints, deferred_load_ids=frozenset({"20261111T090000Z"})) == []
    assert not any("20261111T090000Z" in k for k in checkpoints.applied_files("silver_invoices"))
    assert not _state(spark, env)[C]._is_deleted  # nothing applied yet
    # DMS now reports the full load complete: applied whole, and C is tombstoned.
    [result] = _job(spark, env, checkpoints, complete_full_loads=frozenset({"20261111T090000Z"}))
    assert result.full_load_tombstones == 1 and _state(spark, env)[C]._is_deleted
