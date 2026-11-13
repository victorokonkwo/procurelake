"""Acceptance test 9: unauthorized access. "Try direct access to restricted S3 data → access denied."

On the deployed dev environment (ADR 0008, ADR 0030), as each fictional tenant's role:

* direct S3 reads and listings of the lakehouse and raw buckets are denied (Lake Formation does
  not govern S3, so this path is closed separately);
* Gold tables that were not granted (facts, dimensions) are denied;
* the other tenant's Athena workgroup cannot be used;
* the other tenant's exports cannot be read or written, and no tenant can delete exports.

Run with `make test-aws`.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError
from security_support import DENIED, MARTS, NOT_GRANTED, latest_run_prefix

from procurelake.common.athena import AthenaQueryError, run_query

pytestmark = pytest.mark.aws


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


def test_tenants_cannot_read_or_delete_across_exports(tenants, products_bucket, evidence):
    names = list(tenants)
    for tenant, other in zip(names, names[1:] + names[:1], strict=True):
        s3 = tenants[tenant]["session"].client("s3")
        other_s3 = tenants[other]["session"].client("s3")
        other_manifest = latest_run_prefix(other_s3, products_bucket, other, MARTS[0]) + "_manifest.json"
        own_manifest = latest_run_prefix(s3, products_bucket, tenant, MARTS[0]) + "_manifest.json"
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
