"""Week 7 completion gate (implementation plan D3), on the deployed dev environment (ADR 0030).

Each synthetic tenant role (fictional tenants, not SOVRA customers) is assumed and queries Athena
in its own workgroup:

* every tenant-facing mart returns only that tenant's rows, and exactly as many as the data
  engineer (not governed by Lake Formation) sees for that tenant;
* Gold tables that were not granted (facts, dimensions) fail;
* direct S3 access to the lakehouse and raw buckets fails;
* the other tenant's Athena workgroup cannot be used.

Run with `make test-aws` (needs dev/security applied).
"""

from __future__ import annotations

import json
import os

import pytest
from botocore.exceptions import ClientError

from procurelake.common.athena import AthenaQueryError, run_query
from procurelake.common.aws_clients import build_session

pytestmark = pytest.mark.aws

MARTS = [
    "mart_awarded_value",
    "mart_actual_spend",
    "mart_procurement_cycle_time",
    "mart_supplier_concentration",
]
DENIED = r"PERMISSION_DENIED|AccessDenied|not authorized"
NOT_GRANTED = ["fct_contract_award", "fct_invoice", "dim_supplier"]


@pytest.fixture(scope="module")
def tenants(dev_env) -> dict[str, dict]:
    try:
        roles = json.loads(os.environ["PROCURELAKE_DEV_TENANT_ROLE_ARNS"])
        workgroups = json.loads(os.environ["PROCURELAKE_DEV_TENANT_WORKGROUPS"])
    except KeyError as missing:
        pytest.fail(f"missing {missing} (dev/security outputs; use `make test-aws`)")
    return {
        t: {
            "session": build_session(role_arn=roles[t], session_name=f"it-{t.replace('_', '-')}"),
            "workgroup": workgroups[t],
        }
        for t in roles
    }


@pytest.fixture(scope="module")
def engineer_counts(engineer_session, dev_env) -> dict[str, dict[str, int]]:
    athena, gold = engineer_session.client("athena"), dev_env["PROCURELAKE_DEV_GOLD_DATABASE"]
    counts: dict[str, dict[str, int]] = {}
    for mart in MARTS:
        result = run_query(
            athena,
            f"select tenant_id, count(*) from {gold}.{mart} group by 1",
            dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
            gold,
        )
        counts[mart] = {tenant: int(n) for tenant, n in result.rows}
    return counts


@pytest.mark.parametrize("mart", MARTS)
def test_each_tenant_sees_only_its_own_rows(mart, tenants, engineer_counts, dev_env, evidence):
    gold = dev_env["PROCURELAKE_DEV_GOLD_DATABASE"]
    assert set(engineer_counts[mart]) >= set(tenants), f"{mart} must hold rows for every tenant"
    for tenant, ctx in tenants.items():
        result = run_query(
            ctx["session"].client("athena"),
            f"select tenant_id, count(*) from {gold}.{mart} group by 1",
            ctx["workgroup"],
            gold,
        )
        seen = {t: int(n) for t, n in result.rows}
        evidence["checks"].append(
            {"check": "tenant_rows", "tenant": tenant, "mart": mart, "seen": seen,
             "engineer_count": engineer_counts[mart][tenant], "query_id": result.query_execution_id}
        )  # fmt: skip
        assert seen == {tenant: engineer_counts[mart][tenant]}


@pytest.mark.parametrize("table", NOT_GRANTED)
def test_tables_not_granted_fail(table, tenants, dev_env, evidence):
    gold = dev_env["PROCURELAKE_DEV_GOLD_DATABASE"]
    for tenant, ctx in tenants.items():
        athena = ctx["session"].client("athena")
        with pytest.raises(AthenaQueryError, match=DENIED) as denied:
            run_query(athena, f"select count(*) from {gold}.{table}", ctx["workgroup"], gold)
        evidence["checks"].append(
            {"check": "table_denied", "tenant": tenant, "table": table, "error": str(denied.value)[:200]}
        )


def test_direct_s3_access_fails(tenants, dev_env, evidence):
    lake = dev_env["PROCURELAKE_DEV_RAW_BUCKET"].replace("-raw-", "-lake-")
    raw = dev_env["PROCURELAKE_DEV_RAW_BUCKET"]
    for tenant, ctx in tenants.items():
        s3 = ctx["session"].client("s3")
        calls = {
            "list_gold": lambda s3=s3: s3.list_objects_v2(Bucket=lake, Prefix="gold/", MaxKeys=1),
            "list_lake_root": lambda s3=s3: s3.list_objects_v2(Bucket=lake, MaxKeys=1),
            "list_raw": lambda s3=s3: s3.list_objects_v2(Bucket=raw, MaxKeys=1),
            "get_lake_object": lambda s3=s3: s3.get_object(Bucket=lake, Key="gold/any"),
        }
        for name, call in calls.items():
            with pytest.raises(ClientError) as err:
                call()
            code = err.value.response["Error"]["Code"]
            evidence["checks"].append({"check": "s3_denied", "tenant": tenant, "call": name, "code": code})
            assert code == "AccessDenied", f"{tenant} {name}: {code}"


def test_other_tenants_workgroup_fails(tenants, dev_env):
    gold = dev_env["PROCURELAKE_DEV_GOLD_DATABASE"]
    names = list(tenants)
    for tenant, other in zip(names, names[1:] + names[:1], strict=True):
        with pytest.raises(ClientError) as err:
            athena = tenants[tenant]["session"].client("athena")
            run_query(athena, "select 1", tenants[other]["workgroup"], gold)
        assert err.value.response["Error"]["Code"] in {"AccessDeniedException", "AccessDenied"}


# ----------------------------------------------------------------------------- exports + admin
@pytest.fixture(scope="module")
def products_bucket(dev_env) -> str:
    return dev_env["PROCURELAKE_DEV_RAW_BUCKET"].replace("-raw-", "-products-")


def _latest_run_prefix(s3, bucket: str, tenant: str, mart: str) -> str:
    """Newest export_date=/run= prefix of one tenant's mart (run after `tenant_exports`)."""
    keys = [
        o["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{tenant}/{mart}/")
        for o in page.get("Contents", [])
        if o["Key"].endswith("_manifest.json")
    ]
    if not keys:
        pytest.fail(f"no export of {tenant}/{mart}: run the tenant_exports state machine first")
    # Keys end in export_date=<date>/run=<execution name>/_manifest.json; the newest date wins,
    # then the newest run name (execution names start with a UTC timestamp or schedule time).
    return max(keys).rsplit("/", 1)[0] + "/"


@pytest.mark.parametrize("mart", MARTS)
def test_exports_hold_only_the_tenants_rows(mart, tenants, engineer_counts, products_bucket, evidence):
    import io

    import pyarrow.parquet as pq

    for tenant, ctx in tenants.items():
        s3 = ctx["session"].client("s3")  # the tenant reads its own export
        prefix = _latest_run_prefix(s3, products_bucket, tenant, mart)
        objects = s3.list_objects_v2(Bucket=products_bucket, Prefix=prefix)["Contents"]
        manifest_body = s3.get_object(Bucket=products_bucket, Key=f"{prefix}_manifest.json")["Body"].read()
        manifest = json.loads(manifest_body)
        tenant_ids: list[str] = []
        for o in objects:
            if not o["Key"].endswith("_manifest.json"):
                body = s3.get_object(Bucket=products_bucket, Key=o["Key"])["Body"].read()
                tenant_ids += pq.read_table(io.BytesIO(body)).column("tenant_id").to_pylist()
        evidence["checks"].append(
            {"check": "export_rows", "tenant": tenant, "mart": mart, "prefix": prefix,
             "rows": len(tenant_ids), "query_id": manifest["athena_query_execution_id"]}
        )  # fmt: skip
        assert manifest["tenant"] == tenant and manifest["source_table"].endswith(f".{mart}")
        assert set(tenant_ids) == {tenant}
        assert len(tenant_ids) == engineer_counts[mart][tenant]


def test_tenants_cannot_read_or_delete_across_exports(tenants, products_bucket, evidence):
    names = list(tenants)
    for tenant, other in zip(names, names[1:] + names[:1], strict=True):
        s3 = tenants[tenant]["session"].client("s3")
        other_s3 = tenants[other]["session"].client("s3")
        other_manifest = _latest_run_prefix(other_s3, products_bucket, other, MARTS[0]) + "_manifest.json"
        own_manifest = _latest_run_prefix(s3, products_bucket, tenant, MARTS[0]) + "_manifest.json"
        calls = [
            ("list_objects_v2", {"Prefix": f"{other}/"}),
            ("get_object", {"Key": other_manifest}),
            ("put_object", {"Key": f"{other}/intrusion.txt", "Body": b"x"}),
            ("delete_object", {"Key": own_manifest}),  # even its own exports
        ]
        for operation, kwargs in calls:
            with pytest.raises(ClientError) as err:
                getattr(s3, operation)(Bucket=products_bucket, **kwargs)
            code = err.value.response["Error"]["Code"]
            evidence["checks"].append(
                {"check": "export_denied", "tenant": tenant, "call": operation, "code": code}
            )
            assert code == "AccessDenied", f"{tenant} {operation}: {code}"


@pytest.mark.parametrize("mart", MARTS)
def test_admin_sees_every_tenant(mart, engineer_counts, dev_env, evidence):
    role = os.environ.get("PROCURELAKE_DEV_ADMIN_ROLE_ARN")
    workgroup = os.environ.get("PROCURELAKE_DEV_ADMIN_WORKGROUP")
    if not role or not workgroup:
        pytest.fail("missing PROCURELAKE_DEV_ADMIN_* (dev/security outputs; use `make test-aws`)")
    gold = dev_env["PROCURELAKE_DEV_GOLD_DATABASE"]
    athena = build_session(role_arn=role, session_name="it-analytics-admin").client("athena")
    result = run_query(athena, f"select tenant_id, count(*) from {gold}.{mart} group by 1", workgroup, gold)
    seen = {t: int(n) for t, n in result.rows}
    evidence["checks"].append({"check": "admin_rows", "mart": mart, "seen": seen})
    assert seen == engineer_counts[mart]
