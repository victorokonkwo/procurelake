"""Catalog governance (ADR 0042). Glue is a botocore Stubber (test double): every request is
validated against the real Glue API model, so a TableInput Glue would reject fails here."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import boto3
import pytest
from botocore.stub import ANY, Stubber

from procurelake.governance.catalog import (
    apply_metadata,
    check_gold,
    check_silver,
    gold_expected,
    silver_metadata,
    sync_silver,
)
from procurelake.lakehouse.maintenance_job import catalog_step, parse_args, report
from procurelake.lakehouse.silver.resources import load_silver_specs

DB = "procurelake_dev_silver"
SPECS = load_silver_specs()
AWARDS = SPECS["awards"]


def iceberg_table(name: str, version: str = "7", **extra) -> dict:
    """A Glue table as GetTable returns one for an Iceberg table (shape read from dev)."""
    return {
        "Name": name,
        "DatabaseName": DB,
        "CreateTime": datetime(2026, 11, 10, tzinfo=UTC),
        "UpdateTime": datetime(2026, 11, 12, tzinfo=UTC),
        "Retention": 0,
        "StorageDescriptor": {
            "Columns": [{"Name": "award_id", "Type": "string"}],
            "Location": f"s3://lake/silver/{name}",
        },
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {
            "metadata_location": f"s3://lake/silver/{name}/metadata/0007.metadata.json",
            "previous_metadata_location": f"s3://lake/silver/{name}/metadata/0006.metadata.json",
            "table_type": "ICEBERG",
        },
        "CreatedBy": "arn:aws:sts::123456789012:assumed-role/procurelake-dev-bronze-to-silver/x",
        "IsRegisteredWithLakeFormation": False,
        "CatalogId": "123456789012",
        "VersionId": version,
        **extra,
    }


@pytest.fixture
def glue():
    client = boto3.client(
        "glue", region_name="ca-central-1", aws_access_key_id="x", aws_secret_access_key="x"
    )
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def test_silver_metadata_comes_from_the_contract():
    meta = silver_metadata(AWARDS)
    c = AWARDS.contract
    assert meta.parameters["owner"] == c.owner == "procurement-platform"
    assert meta.parameters["data_classification"] == "synthetic"
    assert meta.parameters["contract_version"] == str(c.contract_version)
    assert meta.parameters["source_table"] == "procurement.awards"
    assert meta.parameters["tenant_scoped"] == "true"
    assert "AWARDED value, not actual" in meta.description  # the contract's wording, kept
    assert meta.parameters["comment"] == meta.description


def test_update_keeps_iceberg_parameters_and_passes_the_version_read(glue):
    client, stub = glue
    table = iceberg_table("silver_awards")
    expected = silver_metadata(AWARDS)
    stub.add_response("get_table", {"Table": table}, {"DatabaseName": DB, "Name": "silver_awards"})
    want_input = {
        "Name": "silver_awards",
        "StorageDescriptor": table["StorageDescriptor"],
        "TableType": "EXTERNAL_TABLE",
        "Retention": 0,
        "Description": expected.description,
        # Iceberg's pointer to the current snapshot is carried over untouched.
        "Parameters": {**table["Parameters"], **expected.parameters},
    }
    stub.add_response(
        "update_table",
        {},
        {"DatabaseName": DB, "TableInput": want_input, "VersionId": "7", "SkipArchive": True},
    )
    result = apply_metadata(client, DB, "silver_awards", expected)
    assert result.status == "updated"
    assert "Description" in result.changed and "owner" in result.changed


def test_concurrent_commit_is_reread_never_overwritten(glue):
    client, stub = glue
    expected = silver_metadata(AWARDS)
    before = iceberg_table("silver_awards", version="7")
    # A CDC commit lands between our read and our write: new metadata_location, version 8.
    after = iceberg_table("silver_awards", version="8")
    after["Parameters"]["metadata_location"] = "s3://lake/silver/silver_awards/metadata/0008.metadata.json"
    stub.add_response("get_table", {"Table": before}, {"DatabaseName": DB, "Name": "silver_awards"})
    stub.add_client_error(
        "update_table",
        service_error_code="ConcurrentModificationException",
        expected_params={"DatabaseName": DB, "TableInput": ANY, "VersionId": "7", "SkipArchive": True},
    )
    stub.add_response("get_table", {"Table": after}, {"DatabaseName": DB, "Name": "silver_awards"})
    stub.add_response(
        "update_table",
        {},
        {"DatabaseName": DB, "TableInput": ANY, "VersionId": "8", "SkipArchive": True},
    )
    sent = []
    client.meta.events.register(
        "before-parameter-build.glue.UpdateTable", lambda params, **_: sent.append(copy.deepcopy(params))
    )
    assert apply_metadata(client, DB, "silver_awards", expected).status == "updated"
    # The retry carries the new commit's metadata_location, never the stale one.
    assert sent[-1]["TableInput"]["Parameters"]["metadata_location"].endswith("0008.metadata.json")


def test_unchanged_table_is_not_written(glue):
    client, stub = glue
    expected = silver_metadata(AWARDS)
    table = iceberg_table("silver_awards", Description=expected.description)
    table["Parameters"].update(expected.parameters)
    stub.add_response("get_table", {"Table": table}, {"DatabaseName": DB, "Name": "silver_awards"})
    assert apply_metadata(client, DB, "silver_awards", expected).status == "unchanged"


def test_gives_up_and_reports_failed_under_constant_contention(glue):
    client, stub = glue
    for v in ("1", "2"):
        stub.add_response(
            "get_table",
            {"Table": iceberg_table("silver_awards", version=v)},
            {"DatabaseName": DB, "Name": "silver_awards"},
        )
        stub.add_client_error("update_table", service_error_code="ConcurrentModificationException")
    result = apply_metadata(client, DB, "silver_awards", silver_metadata(AWARDS), attempts=2)
    assert result.status == "failed"


def test_sync_and_check_report_a_table_not_built_yet(glue):
    client, stub = glue
    stub.add_client_error("get_table", service_error_code="EntityNotFoundException")
    assert [r.status for r in sync_silver(client, DB, [AWARDS])] == ["missing"]
    stub.add_client_error("get_table", service_error_code="EntityNotFoundException")
    assert [r.status for r in check_silver(client, DB, [AWARDS])] == ["missing"]


def test_check_silver_is_read_only_and_names_the_drift(glue):
    client, stub = glue
    stub.add_response(
        "get_table", {"Table": iceberg_table("silver_awards")}, {"DatabaseName": DB, "Name": "silver_awards"}
    )
    (result,) = check_silver(client, DB, [AWARDS])
    assert result.status == "drift"
    assert {"Description", "owner", "data_classification", "grain"} <= set(result.changed)


def test_gold_expected_and_check_from_a_manifest(glue):
    client, stub = glue
    manifest = {"nodes": {
        "model.p.mart_x": {"resource_type": "model", "name": "mart_x",
                           "config": {"materialized": "incremental", "meta": {
                               "grain": "g", "owner": "o", "freshness": "24h",
                               "data_classification": "synthetic"}}},
        "model.p.stg_y": {"resource_type": "model", "name": "stg_y", "config": {"materialized": "ephemeral"}},
        "test.p.t": {"resource_type": "test", "name": "t", "config": {}},
    }}  # fmt: skip
    expected = gold_expected(manifest)
    assert expected == {
        "mart_x": {"grain": "g", "owner": "o", "freshness": "24h", "data_classification": "synthetic"}
    }
    table = iceberg_table("mart_x", Description="x")
    table["Parameters"].update({"grain": "g", "owner": "o", "freshness": "24h"})
    stub.add_response("get_table", {"Table": table}, {"DatabaseName": "gold", "Name": "mart_x"})
    (result,) = check_gold(client, "gold", expected)
    assert result.status == "drift" and result.changed == ("data_classification",)


def test_maintenance_catalog_step_covers_only_contracted_silver_tables(glue):
    client, stub = glue
    args = parse_args([
        "--workgroup", "wg", "--silver-database", DB, "--gold-database", "g", "--report-bucket", "b",
        "--checkpoint-table", "c", "--silver-tables", "silver_awards,silver_contract_intelligence",
        "--silver-max-age-seconds", "1", "--silver-min-snapshots", "1",
        "--gold-max-age-seconds", "1", "--gold-min-snapshots", "1",
    ])  # fmt: skip
    expected = silver_metadata(AWARDS)
    table = iceberg_table("silver_awards", Description=expected.description)
    table["Parameters"].update(expected.parameters)
    # silver_contract_intelligence has no source contract (it is written by the AI pipeline).
    stub.add_response("get_table", {"Table": table}, {"DatabaseName": DB, "Name": "silver_awards"})
    results = catalog_step(client, args)
    assert [(r.table, r.status) for r in results] == [(f"{DB}.silver_awards", "unchanged")]
    body = report([], datetime.now(UTC), "r", results)
    assert body["catalog_failed"] == [] and json.dumps(body)
