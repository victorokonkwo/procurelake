"""Acceptance test 8: tenant isolation. "Query as Tenant Alder → no Birch data is visible."

On the deployed dev environment (ADR 0030), each fictional tenant assumes its own role and
queries Athena in its own workgroup, through Lake Formation row filters:

* every tenant-facing mart returns only that tenant's rows, and exactly as many as the data
  engineer (not governed by Lake Formation) sees for that tenant;
* the daily exports, read back as the tenant, hold only that tenant's rows;
* the cross-tenant admin product sees every tenant (the isolation is per role, not global).

Run with `make test-aws` (needs dev/security applied). Concurrency is covered by
`make bench-concurrent-tenants` (docs/benchmarks/2026-11-12-replay-concurrency-cdc.md §2).
"""

from __future__ import annotations

import json
import os

import pytest
from security_support import MARTS, latest_run_prefix

from procurelake.common.athena import run_query
from procurelake.common.aws_clients import build_session

pytestmark = pytest.mark.aws


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


@pytest.mark.parametrize("mart", MARTS)
def test_exports_hold_only_the_tenants_rows(mart, tenants, engineer_counts, products_bucket, evidence):
    import io

    import pyarrow.parquet as pq

    for tenant, ctx in tenants.items():
        s3 = ctx["session"].client("s3")  # the tenant reads its own export
        prefix = latest_run_prefix(s3, products_bucket, tenant, mart)
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
