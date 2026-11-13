"""Constants and helpers shared by the security acceptance tests (plan §3, tests 8 and 9)."""

from __future__ import annotations

import pytest

MARTS = [
    "mart_awarded_value",
    "mart_actual_spend",
    "mart_procurement_cycle_time",
    "mart_supplier_concentration",
]
DENIED = r"PERMISSION_DENIED|AccessDenied|not authorized"
NOT_GRANTED = ["fct_contract_award", "fct_invoice", "dim_supplier"]


def latest_run_prefix(s3, bucket: str, tenant: str, mart: str) -> str:
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
