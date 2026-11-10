"""Week 4 completion gate (implementation plan B3), on the deployed dev environment:

* "A Gold metric can be traced back to its originating source records": the largest
  mart_awarded_value cell is decomposed into its fct_contract_award rows, and each row's lineage
  leads to exactly one Bronze record whose values equal the Gold row:
  - snapshot batches (JSON Lines): the line with SHA-256 ``source_line_hash``, in a file listed
    with its checksum in a succeeded snapshot manifest;
  - CDC (AWS DMS Parquet, ADR 0028): the row at the position encoded in ``source_sequence``,
    in a file recorded as applied in the CDC checkpoint table.
* Gold and Silver reconcile: every Gold monetary total equals Silver per tenant and currency.

Run after `make dbt-build` (`make test-aws`).
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from procurelake.common.athena import run_query
from procurelake.common.aws_clients import build_session
from procurelake.ingestion.landing import S3LandingStore
from procurelake.ingestion.manifest import load_manifests

pytestmark = pytest.mark.aws

_PATTERNS = {
    "tenant": re.compile(r"tenant_[a-z][a-z0-9_]*"),
    "uuid": re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "currency": re.compile(r"[A-Z]{3}"),
    "date": re.compile(r"\d{4}-\d{2}-\d{2}"),
}


def _lit(value: str, kind: str) -> str:
    # Athena has no bind parameters here: every value placed in SQL is checked first.
    if not _PATTERNS[kind].fullmatch(value):
        raise ValueError(f"refusing to place {kind} {value!r} in SQL")
    return f"'{value}'"


def _query(engineer_session, dev_env, sql, evidence, check):
    result = run_query(
        engineer_session.client("athena"),
        sql,
        workgroup=dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
        database=dev_env["PROCURELAKE_DEV_GOLD_DATABASE"],
    )
    evidence["checks"].append(
        {
            "check": check,
            "query_execution_id": result.query_execution_id,
            "data_scanned_bytes": result.data_scanned_bytes,
            "engine_execution_ms": result.engine_execution_ms,
            "rows": result.rows[:5],
        }
    )
    return result


def test_gold_awarded_value_traces_to_bronze_lines(engineer_session, dev_env, evidence):
    top = _query(
        engineer_session,
        dev_env,
        "SELECT tenant_id, agency_id, supplier_id, cast(award_month AS varchar), currency_code, "
        "award_count, cast(awarded_value AS varchar) FROM mart_awarded_value "
        "ORDER BY awarded_value DESC, tenant_id, agency_id, supplier_id LIMIT 1",
        evidence,
        "gold: largest mart_awarded_value cell",
    ).rows[0]
    tenant, agency, supplier, month, currency, count, value = top
    facts = _query(
        engineer_session,
        dev_env,
        "SELECT award_id, cast(award_amount AS varchar), source_batch_id, source_file, source_line_hash, "
        "source_sequence "
        "FROM fct_contract_award WHERE award_status = 'active' "
        f"AND tenant_id = {_lit(tenant, 'tenant')} AND agency_id = {_lit(agency, 'uuid')} "
        f"AND supplier_id = {_lit(supplier, 'uuid')} AND currency_code = {_lit(currency, 'currency')} "
        f"AND award_month = DATE {_lit(month, 'date')}",
        evidence,
        "gold: fct_contract_award rows behind that cell",
    ).rows
    assert len(facts) == int(count)
    assert sum(Decimal(r[1]) for r in facts) == Decimal(value)

    s3 = engineer_session.client("s3")
    lines_by_file: dict[str, dict[str, str]] = {}
    traced = []
    for award_id, amount, batch_id, source_file, line_hash, sequence in facts:
        bucket, key = source_file.removeprefix("s3://").split("/", 1)
        if key.endswith(".parquet"):
            record = _cdc_record(s3, bucket, key, sequence)
        else:
            if source_file not in lines_by_file:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
                lines_by_file[source_file] = {
                    hashlib.sha256(ln.encode()).hexdigest(): ln for ln in body.splitlines()
                }
            line = lines_by_file[source_file].get(line_hash)
            assert line is not None, f"{award_id}: no Bronze line with hash {line_hash} in {source_file}"
            record = json.loads(line)
        assert record["award_id"] == award_id
        assert Decimal(str(record["award_amount"])) == Decimal(amount)
        assert (record["tenant_id"], record["supplier_id"], record["currency_code"], record["status"]) == (
            tenant,
            supplier,
            currency,
            "active",
        )
        traced.append(
            {
                "award_id": award_id,
                "award_amount": amount,
                "batch_id": batch_id,
                "file": source_file,
                "kind": "cdc" if key.endswith(".parquet") else "snapshot",
                "sequence": sequence,
            }
        )

    for t in traced:
        bucket, key = t["file"].removeprefix("s3://").split("/", 1)
        if t["kind"] == "cdc":
            _assert_cdc_file_applied(dev_env, key)
            continue
        store = S3LandingStore(s3, dev_env["PROCURELAKE_DEV_ARTIFACTS_BUCKET"])
        manifest = {m.batch_id: m for m in load_manifests(store, "postgres", "awards")}[t["batch_id"]]
        assert manifest.status == "succeeded"
        landed = {f.uri: f for f in manifest.files}
        assert t["file"] in landed
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert hashlib.sha256(data).hexdigest() == landed[t["file"]].sha256
    evidence["checks"].append(
        {"check": "trace: Gold cell -> Bronze record -> manifest/checkpoint", "cell": top, "awards": traced}
    )


def _cdc_record(s3, bucket: str, key: str, sequence: str) -> dict:
    """The DMS Parquet row a Silver sequence points at (commit ts | rank | file | row index)."""
    commit, _rank, file_name, row_index = sequence.split("|")
    assert key.rsplit("/", 1)[-1] == file_name
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    row = pq.read_table(pa.BufferReader(body)).slice(int(row_index), 1).to_pylist()[0]
    assert row["_dms_commit_ts"].replace("-", "").replace(" ", "").replace(":", "").replace(".", "") == commit
    return row


def _assert_cdc_file_applied(dev_env, key: str) -> None:
    # The operator's own identity reads the checkpoint table (workload roles do not need it).
    ddb = build_session(region="ca-central-1").client("dynamodb")
    item = ddb.get_item(
        TableName=dev_env["PROCURELAKE_DEV_CDC_CHECKPOINT_TABLE"],
        Key={"pk": {"S": "silver_awards"}, "sk": {"S": f"file#{key}"}},
    ).get("Item")
    assert item, f"{key} is not recorded as applied in the CDC checkpoint table"


@pytest.mark.parametrize(
    ("check", "gold_sql", "silver_sql"),
    [
        (
            "awarded value",
            "SELECT tenant_id, currency_code, cast(sum(awarded_value) AS varchar) FROM mart_awarded_value "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            "SELECT tenant_id, currency_code, cast(sum(award_amount) AS varchar) FROM {silver}.silver_awards "
            "WHERE status = 'active' AND NOT coalesce(_is_deleted, false) GROUP BY 1, 2 ORDER BY 1, 2",
        ),
        (
            "actual spend",
            "SELECT tenant_id, currency_code, cast(sum(actual_spend) AS varchar) FROM mart_actual_spend "
            "GROUP BY 1, 2 ORDER BY 1, 2",
            "SELECT tenant_id, currency_code, cast(sum(paid_amount) AS varchar) "
            "FROM {silver}.silver_invoices WHERE paid_amount > 0 AND NOT coalesce(_is_deleted, false) "
            "GROUP BY 1, 2 ORDER BY 1, 2",
        ),
    ],
)
def test_gold_totals_reconcile_with_silver(engineer_session, dev_env, evidence, check, gold_sql, silver_sql):
    silver_db = dev_env["PROCURELAKE_DEV_SILVER_DATABASE"]
    assert re.fullmatch(r"[a-z][a-z0-9_]*", silver_db)
    gold = _query(engineer_session, dev_env, gold_sql, evidence, f"reconcile {check}: gold").rows
    silver = _query(
        engineer_session, dev_env, silver_sql.format(silver=silver_db), evidence, f"reconcile {check}: silver"
    ).rows
    assert gold and gold == silver
