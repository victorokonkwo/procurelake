"""Sprint 1 Day 5 completion gate: "Bronze data is queryable and test evidence recorded".

Every succeeded, settled USAspending batch listed in the manifests must be fully queryable
through the Glue table and the engineering Athena workgroup, with record and file counts that
match its manifest exactly, and with field values identical to the raw files in S3.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from procurelake.common.athena import run_query
from procurelake.ingestion.landing import S3LandingStore
from procurelake.ingestion.manifest import IngestionManifest, load_manifests

pytestmark = pytest.mark.aws

REPO = Path(__file__).resolve().parents[3]
FLATTENED_SQL = (
    REPO / "infrastructure/live/dev/foundation/queries/bronze_usaspending_awards_flattened.sql"
).read_text()
RAW_TABLE = "usaspending_spending_by_award_raw"

# Athena's StartQueryExecution has no bind parameters for these queries, so every value placed
# in SQL is checked against a strict pattern first. That includes ids from the external API.
_LITERAL_PATTERNS = {
    "date": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "batch_id": re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{8}"),
    "internal_id": re.compile(r"[A-Za-z0-9_.\-]{1,200}"),
}


def _lit(value: str, kind: str) -> str:
    if not _LITERAL_PATTERNS[kind].fullmatch(value):
        raise ValueError(f"refusing to place {kind} {value!r} in SQL")
    return f"'{value}'"


def _settled_batches(engineer_session, dev_env) -> list[IngestionManifest]:
    store = S3LandingStore(engineer_session.client("s3"), dev_env["PROCURELAKE_DEV_ARTIFACTS_BUCKET"])
    manifests = load_manifests(store, "usaspending", "spending_by_award")
    return [m for m in manifests if m.status == "succeeded" and m.window_settled]


def _query(engineer_session, dev_env, sql: str, evidence: dict, check: str):
    result = run_query(
        engineer_session.client("athena"),
        sql,
        workgroup=dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
        database=dev_env["PROCURELAKE_DEV_BRONZE_DATABASE"],
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


def test_workgroup_enforces_engine_encryption_and_scan_cutoff(engineer_session, dev_env):
    wg = engineer_session.client("athena").get_work_group(
        WorkGroup=dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"]
    )
    config = wg["WorkGroup"]["Configuration"]
    assert config["EnforceWorkGroupConfiguration"] is True
    assert config["EngineVersion"]["EffectiveEngineVersion"] == "Athena engine version 3"
    assert config["ResultConfiguration"]["EncryptionConfiguration"] == {
        "EncryptionOption": "SSE_KMS",
        "KmsKey": dev_env["PROCURELAKE_DEV_DATA_KMS_KEY_ARN"],
    }
    assert config["BytesScannedCutoffPerQuery"] == 1_073_741_824


def test_raw_table_is_registered_with_partition_projection(engineer_session, dev_env):
    table = engineer_session.client("glue").get_table(
        DatabaseName=dev_env["PROCURELAKE_DEV_BRONZE_DATABASE"], Name=RAW_TABLE
    )["Table"]
    assert [c["Name"] for c in table["StorageDescriptor"]["Columns"]] == ["raw_json"]
    assert table["Parameters"]["projection.enabled"] == "true"
    assert [p["Name"] for p in table["PartitionKeys"]] == ["ingestion_date"]


def test_every_settled_batch_is_fully_queryable(engineer_session, dev_env, evidence):
    batches = _settled_batches(engineer_session, dev_env)
    assert batches, "no succeeded, settled USAspending batch found; run `make ingest-usaspending` first"

    for manifest in batches:
        ingestion_date = manifest.ingestion_started_at.date().isoformat()
        sql = f"""
            WITH flattened AS ({FLATTENED_SQL})
            SELECT count(*) AS records,
                   count(DISTINCT generated_internal_id) AS unique_ids,
                   count(DISTINCT page) AS pages
            FROM flattened
            WHERE ingestion_date = {_lit(ingestion_date, "date")}
              AND batch_id = {_lit(manifest.batch_id, "batch_id")}
        """
        result = _query(engineer_session, dev_env, sql, evidence, f"batch {manifest.batch_id} counts")
        records, unique_ids, pages = (int(v) for v in result.rows[0])
        expected_pages = sum(1 for f in manifest.files if f.record_count > 0)
        assert records == manifest.reconciliation.landed_record_count
        assert unique_ids == manifest.reconciliation.unique_record_count
        assert pages == expected_pages


def test_query_values_match_raw_file_in_s3(engineer_session, dev_env, evidence):
    manifest = max(
        _settled_batches(engineer_session, dev_env), key=lambda m: m.reconciliation.landed_record_count
    )
    first_file = manifest.files[0]
    raw = engineer_session.client("s3").get_object(
        Bucket=dev_env["PROCURELAKE_DEV_RAW_BUCKET"], Key=first_file.key
    )
    record = json.loads(raw["Body"].read())["results"][0]

    sql = f"""
        WITH flattened AS ({FLATTENED_SQL})
        SELECT award_id, recipient_name, CAST(award_amount AS VARCHAR), awarding_agency, naics_code
        FROM flattened
        WHERE ingestion_date = {_lit(manifest.ingestion_started_at.date().isoformat(), "date")}
          AND batch_id = {_lit(manifest.batch_id, "batch_id")}
          AND generated_internal_id = {_lit(record["generated_internal_id"], "internal_id")}
    """
    result = _query(engineer_session, dev_env, sql, evidence, "field values vs raw file")
    assert len(result.rows) == 1
    award_id, recipient, amount, agency, naics = result.rows[0]
    assert award_id == record["Award ID"]
    assert recipient == record["Recipient Name"]
    assert agency == record["Awarding Agency"]
    assert naics == (record["NAICS"] or {}).get("code")
    assert round(float(amount), 2) == round(float(record["Award Amount"]), 2)


def test_query_results_are_encrypted_with_platform_key(engineer_session, dev_env, evidence):
    result = _query(
        engineer_session, dev_env, f"SELECT count(*) FROM {RAW_TABLE}", evidence, "results encryption"
    )
    bucket, key = result.output_location.removeprefix("s3://").split("/", 1)
    assert bucket == dev_env["PROCURELAKE_DEV_ARTIFACTS_BUCKET"]
    assert key.startswith(f"athena_results/{dev_env['PROCURELAKE_DEV_ATHENA_WORKGROUP']}/")
    head = engineer_session.client("s3").head_object(Bucket=bucket, Key=key)
    assert head["ServerSideEncryption"] == "aws:kms"
    assert head["SSEKMSKeyId"] == dev_env["PROCURELAKE_DEV_DATA_KMS_KEY_ARN"]


def test_ingestion_role_cannot_query(ingestion_session, dev_env):
    with pytest.raises(ClientError) as exc:
        ingestion_session.client("athena").start_query_execution(
            QueryString=f"SELECT 1 FROM {RAW_TABLE} LIMIT 1",
            WorkGroup=dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
            QueryExecutionContext={"Database": dev_env["PROCURELAKE_DEV_BRONZE_DATABASE"]},
        )
    assert exc.value.response["Error"]["Code"] == "AccessDeniedException"
