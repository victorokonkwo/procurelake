"""Fixtures for the security acceptance tests (plan §3, tests 8 and 9), on deployed dev.

Each fictional tenant role (not a SOVRA customer) is assumed for real; the data engineer's
counts (not governed by Lake Formation) are the reference. Run with `make test-aws`.
"""

from __future__ import annotations

import json
import os

import pytest
from security_support import MARTS

from procurelake.common.athena import run_query
from procurelake.common.aws_clients import build_session


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


@pytest.fixture(scope="module")
def products_bucket(dev_env) -> str:
    return dev_env["PROCURELAKE_DEV_RAW_BUCKET"].replace("-raw-", "-products-")
