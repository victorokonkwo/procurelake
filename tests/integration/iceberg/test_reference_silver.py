"""USAspending reference batches -> Silver Iceberg (ADR 0044), on local Spark.

Bronze pages and manifests are written by a test helper in the connector's layout (one JSON page
per file with a ``results`` list; a manifest per day window), so each case controls exactly which
version of an award arrives in which batch.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from procurelake.contracts.reference import load_reference_contracts
from procurelake.ingestion.landing import LocalLandingStore
from procurelake.ingestion.manifest import (
    CollectionWindow,
    IngestionManifest,
    LandedFile,
    Reconciliation,
    write_manifest,
)
from procurelake.lakehouse.silver.reference import BronzeMismatchError, apply_batches, pending_batches
from procurelake.lakehouse.silver.reference_job import applied_batch_ids, run

pytestmark = pytest.mark.spark

REPO = Path(__file__).resolve().parents[3]
CONTRACT = load_reference_contracts(REPO / "contracts" / "reference")["usaspending_contract_awards"]


def record(gid: str, modified: str, amount=1000.5, **extra) -> dict:
    """A record shaped like the live API's (field names and nesting as observed 2026-11-13)."""
    rec = {
        "internal_id": 1,
        "Award ID": f"PIID-{gid}",
        "generated_internal_id": gid,
        "Recipient Name": "EXAMPLE RECIPIENT",
        "Recipient UEI": "ABCDEFGHIJK1",
        "Award Amount": amount,
        "Total Outlays": None,
        "Awarding Agency": "Department of Defense",
        "Awarding Agency Code": "097",
        "Awarding Sub Agency": "Defense Logistics Agency",
        "Awarding Sub Agency Code": "97AS",
        "Funding Agency": "Department of Defense",
        "Funding Sub Agency": "Defense Logistics Agency",
        "Contract Award Type": "PURCHASE ORDER",
        "Start Date": "2026-10-01",
        "End Date": "2027-09-30",
        "Base Obligation Date": "2026-10-01",
        "Last Modified Date": modified,
        "NAICS": {"code": "334511", "description": "NAVIGATION INSTRUMENTS"},
        "PSC": {"code": "J016", "description": "MAINT/REPAIR"},
        "Description": "TEST AWARD",
        "Place of Performance State Code": "VA",
        "Place of Performance Country Code": "USA",
    }
    rec.update(extra)
    return rec


class Lake:
    def __init__(self, tmp_path: Path, spark):
        self.raw = LocalLandingStore(tmp_path / "raw")
        self.art = LocalLandingStore(tmp_path / "art")
        self.quarantine = f"file://{tmp_path}/quarantine"
        self.namespace = f"lake.ref_{tmp_path.name.replace('-', '_')[-20:]}"
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {self.namespace}")
        self.table = f"{self.namespace}.{CONTRACT.silver_table}"
        self.spark = spark

    def batch(
        self,
        batch_id: str,
        window: date,
        pages: list[list[dict]],
        *,
        purpose=None,
        settled=True,
        status="succeeded",
        landed: int | None = None,
    ) -> IngestionManifest:
        files = []
        for i, results in enumerate(pages, start=1):
            prefix = f"bronze/usaspending/spending_by_award/ingestion_date=2026-11-13/batch_id={batch_id}"
            key = f"{prefix}/page-{i:05d}.json"
            stored = self.raw.put_new(
                key, json.dumps({"results": results, "page_metadata": {}}).encode(), "application/json"
            )
            files.append(
                LandedFile(
                    key=key,
                    uri=stored.uri,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256_hex,
                    record_count=len(results),
                    page=i,
                )
            )
        n = sum(len(p) for p in pages)
        m = IngestionManifest(
            source_system="usaspending",
            dataset="spending_by_award",
            schema_version=CONTRACT.bronze_schema_version,
            batch_id=batch_id,
            status=status,
            ingestion_started_at=datetime(2026, 11, 13, tzinfo=UTC),
            ingestion_completed_at=datetime(2026, 11, 13, tzinfo=UTC),
            window=CollectionWindow(date_type="last_modified_date", start_date=window, end_date=window),
            window_settled=settled,
            request={"purpose": purpose} if purpose else {},
            files=files,
            reconciliation=Reconciliation(
                expected_count_before=n,
                expected_count_after=n,
                landed_record_count=n if landed is None else landed,
                unique_record_count=n,
                passed=True,
                detail="test",
            ),
            code_version="test",
        )
        write_manifest(self.art, m)
        return m

    def rows(self) -> dict[str, dict]:
        return {r["generated_internal_id"]: r.asDict() for r in self.spark.table(self.table).collect()}


@pytest.fixture
def lake(tmp_path, spark):
    return Lake(tmp_path, spark)


def test_first_load_types_values_exactly_and_keeps_provenance(lake):
    m = lake.batch(
        "20261101T000000Z-aaaaaaaa", date(2026, 10, 20), [[record("A", "2026-10-20 08:00:00", 19760162.9)]]
    )
    result = apply_batches(lake.spark, CONTRACT, [m], lake.table, lake.quarantine)
    a = lake.rows()["A"]
    assert a["award_amount"] == Decimal("19760162.90")
    assert a["total_outlays"] is None
    assert a["last_modified_at"] == datetime(2026, 10, 20, 8, 0, 0)  # no time zone assumed
    assert (a["naics_code"], a["psc_code"], a["start_date"]) == ("334511", "J016", date(2026, 10, 1))
    assert a["currency_code"] == "USD"
    assert a["_batch_id"] == m.batch_id and a["_window_date"] == date(2026, 10, 20)
    assert a["_source_file"].endswith("page-00001.json") and a["_source_position"] == 0
    assert result.batches[0].new_keys == 1


def test_a_later_modification_replaces_and_an_older_one_never_does(lake):
    first = lake.batch(
        "20261101T000000Z-aaaaaaaa", date(2026, 10, 20), [[record("A", "2026-10-20 08:00:00", 100)]]
    )
    apply_batches(lake.spark, CONTRACT, [first], lake.table, lake.quarantine)
    newer = lake.batch(
        "20261102T000000Z-bbbbbbbb", date(2026, 10, 25), [[record("A", "2026-10-25 09:00:00", 200)]]
    )
    r = apply_batches(lake.spark, CONTRACT, [newer], lake.table, lake.quarantine)
    assert lake.rows()["A"]["award_amount"] == Decimal("200.00") and r.batches[0].changed_keys == 1
    # A re-sweep of the earlier day returns the old version: it must not regress the row.
    resweep = lake.batch(
        "20261108T000000Z-cccccccc",
        date(2026, 10, 20),
        [[record("A", "2026-10-20 08:00:00", 100)]],
        purpose="resweep",
    )
    r = apply_batches(lake.spark, CONTRACT, [resweep], lake.table, lake.quarantine)
    assert lake.rows()["A"]["award_amount"] == Decimal("200.00")
    assert (r.batches[0].older_than_silver, r.batches[0].purpose) == (1, "resweep")


def test_within_one_run_latest_modification_wins_then_later_batch_on_a_tie(lake):
    b1 = lake.batch(
        "20261101T000000Z-aaaaaaaa",
        date(2026, 10, 20),
        [[record("A", "2026-10-21 10:00:00", 300), record("B", "2026-10-20 10:00:00", 1)]],
    )
    b2 = lake.batch(
        "20261102T000000Z-bbbbbbbb",
        date(2026, 10, 20),
        [[record("A", "2026-10-20 10:00:00", 999), record("B", "2026-10-20 10:00:00", 2)]],
    )
    r = apply_batches(lake.spark, CONTRACT, [b1, b2], lake.table, lake.quarantine)
    rows = lake.rows()
    assert rows["A"]["award_amount"] == Decimal("300.00")  # newer modification, from the older batch
    assert rows["B"]["award_amount"] == Decimal("2.00")  # same modification: the later batch
    assert sum(b.superseded_in_run for b in r.batches) == 2


def test_resweep_counts_new_changed_and_unchanged_keys(lake):
    day = date(2026, 10, 20)
    apply_batches(
        lake.spark,
        CONTRACT,
        [
            lake.batch(
                "20261101T000000Z-aaaaaaaa",
                day,
                [[record("A", "2026-10-20 08:00:00"), record("B", "2026-10-20 08:00:00")]],
            )
        ],
        lake.table,
        lake.quarantine,
    )
    resweep = lake.batch(
        "20261108T000000Z-bbbbbbbb",
        day,
        [
            [
                record("A", "2026-10-20 08:00:00"),  # unchanged
                record("B", "2026-10-20 08:00:00", 5.25),  # same timestamp, different content, later batch
                record("C", "2026-10-20 23:00:00"),  # backfilled into the day after it was collected
            ]
        ],
        purpose="resweep",
    )
    (b,) = apply_batches(lake.spark, CONTRACT, [resweep], lake.table, lake.quarantine).batches
    assert (b.new_keys, b.changed_keys, b.unchanged_keys, b.older_than_silver) == (1, 1, 1, 0)
    assert lake.rows()["B"]["award_amount"] == Decimal("5.25")


def test_invalid_records_are_quarantined_with_reasons_never_rounded(lake, spark):
    good = [record(f"G{i}", "2026-10-20 08:00:00") for i in range(200)]
    bad = [
        record("X1", "2026-10-20 08:00:00", 10.005),  # three decimals: never silently rounded
        record("X2", "20/10/2026"),  # malformed timestamp on a required column
    ]
    m = lake.batch("20261101T000000Z-aaaaaaaa", date(2026, 10, 20), [good, bad])
    (b,) = apply_batches(spark, CONTRACT, [m], lake.table, lake.quarantine).batches
    assert (b.status, b.valid_records, b.rejected_records) == ("applied", 200, 2)
    reasons = {
        r["raw_record"] and json.loads(r["raw_record"])["generated_internal_id"]: r["reasons"]
        for r in spark.read.json(b.quarantine_uri).collect()
    }
    assert reasons == {"X1": ["type_mismatch:award_amount"], "X2": ["type_mismatch:last_modified_at"]}
    assert "X1" not in lake.rows()


def test_missing_required_field_is_not_null_violation(lake, spark):
    rec = record("A", "x")
    del rec["Last Modified Date"]
    m = lake.batch("20261101T000000Z-aaaaaaaa", date(2026, 10, 20), [[rec]])
    (b,) = apply_batches(spark, CONTRACT, [m], lake.table, lake.quarantine).batches
    assert b.status == "blocked"  # 1 of 1 rejected is above the 1% gate
    assert spark.read.json(b.quarantine_uri).first()["reasons"] == ["not_null:last_modified_at"]
    assert lake.rows() == {}


def test_bronze_that_disagrees_with_its_manifest_fails(lake, spark):
    m = lake.batch(
        "20261101T000000Z-aaaaaaaa", date(2026, 10, 20), [[record("A", "2026-10-20 08:00:00")]], landed=2
    )
    with pytest.raises(BronzeMismatchError):
        apply_batches(spark, CONTRACT, [m], lake.table, lake.quarantine)


def test_job_applies_pending_once_records_the_checkpoint_and_skips_unsettled(lake, spark):
    day = date(2026, 10, 20)
    lake.batch("20261101T000000Z-aaaaaaaa", day, [[record("A", "2026-10-20 08:00:00")]])
    lake.batch("20261101T000001Z-bbbbbbbb", day, [[record("B", "2026-10-20 08:00:00")]], settled=False)
    lake.batch("20261101T000002Z-cccccccc", day, [[record("C", "2026-10-20 08:00:00")]], status="failed")
    db = lake.namespace.split(".")[1]
    summary, key = run(spark, CONTRACT, lake.art, "lake", db, lake.quarantine)
    assert summary["applied_batch_ids"] == ["20261101T000000Z-aaaaaaaa"]
    assert key.startswith("pipeline_runs/silver/usaspending/reference/usaspending_contract_awards/")
    assert applied_batch_ids(lake.art, CONTRACT) == {"20261101T000000Z-aaaaaaaa"}
    again, _ = run(spark, CONTRACT, lake.art, "lake", db, lake.quarantine)
    assert (again["applied_batch_ids"], again["pending_before_run"]) == ([], 0)
    assert set(lake.rows()) == {"A"}


def test_job_caps_batches_per_run_and_leaves_the_rest(lake, spark):
    for i in range(3):
        lake.batch(
            f"2026110{i + 1}T000000Z-aaaaaaa{i}",
            date(2026, 10, 20 + i),
            [[record(f"K{i}", f"2026-10-{20 + i} 08:00:00")]],
        )
    db = lake.namespace.split(".")[1]
    first, _ = run(spark, CONTRACT, lake.art, "lake", db, lake.quarantine, max_batches=2)
    assert (len(first["applied_batch_ids"]), first["left_for_next_run"]) == (2, 1)
    second, _ = run(spark, CONTRACT, lake.art, "lake", db, lake.quarantine, max_batches=2)
    assert second["applied_batch_ids"] == ["20261103T000000Z-aaaaaaa2"]


def test_pending_ignores_other_schema_versions():
    m = IngestionManifest(
        source_system="usaspending",
        dataset="spending_by_award",
        schema_version="usaspending.other.v9",
        batch_id="20261101T000000Z-aaaaaaaa",
        status="succeeded",
        ingestion_started_at=datetime(2026, 11, 1, tzinfo=UTC),
        ingestion_completed_at=datetime(2026, 11, 1, tzinfo=UTC),
        window_settled=True,
        window=CollectionWindow(
            date_type="last_modified_date", start_date=date(2026, 10, 1), end_date=date(2026, 10, 1)
        ),
        code_version="t",
    )
    assert pending_batches([m], CONTRACT, set()) == []
