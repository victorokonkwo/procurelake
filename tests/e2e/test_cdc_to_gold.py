"""Acceptance tests 2 and 3: CDC update and CDC delete reach Gold. Live tier.

"Update a PostgreSQL award → the correct latest value appears in Gold."
"Delete a source record → the Gold current-state model no longer exposes it."

Needs the disposable dev/cdc stack up (RDS, DMS, the 5-minute ``cdc_processing`` schedule;
billed hourly, ADR 0027). Opt-in twice: the ``aws`` marker and ``PROCURELAKE_CDC_TESTS=1``;
`make test-acceptance-live` sets both from Terraform outputs.

One synthetic award's amount is changed and one synthetic invoice is deleted in RDS, each in its
own committed transaction. Nothing here starts the pipeline: the schedule runs it, as in
production. Silver and Gold are polled as the data engineer until:

- the award's new amount is in ``silver_awards`` and ``fct_contract_award``;
- the invoice is a tombstone in ``silver_invoices`` and absent from ``fct_invoice``.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from procurelake.common.athena import run_query
from procurelake.common.aws_clients import build_session

pytestmark = pytest.mark.aws

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "build" / "evidence" / "acceptance_cdc_to_gold.json"
TIMEOUT_S = 30 * 60
POLL_S = 30


@pytest.fixture(scope="module")
def cdc(engineer_session, dev_env):
    if os.environ.get("PROCURELAKE_CDC_TESTS") != "1":
        pytest.skip("needs the dev/cdc stack: run `make test-acceptance-live` while it is up")
    needed = ["PROCURELAKE_CDC_HOST", "PROCURELAKE_CDC_DATABASE", "PROCURELAKE_CDC_MASTER_SECRET_ARN"]
    missing = [k for k in needed if not os.environ.get(k)]
    if missing:
        pytest.fail(f"missing {missing} (dev/cdc outputs; use `make test-acceptance-live`)")
    # The RDS master secret is read with the operator's own credentials, as `make cdc-*` does;
    # the data engineer role has no access to it.
    secret = json.loads(
        build_session()
        .client("secretsmanager")
        .get_secret_value(SecretId=os.environ["PROCURELAKE_CDC_MASTER_SECRET_ARN"])["SecretString"]
    )
    dsn = psycopg.conninfo.make_conninfo(
        host=os.environ["PROCURELAKE_CDC_HOST"],
        port=int(os.environ.get("PROCURELAKE_CDC_PORT", "5432")),
        dbname=os.environ["PROCURELAKE_CDC_DATABASE"],
        user=secret["username"],
        password=secret["password"],
        sslmode="require",
        connect_timeout=15,
    )
    return {
        "dsn": dsn,
        "athena": engineer_session.client("athena"),
        "wg": dev_env["PROCURELAKE_DEV_ATHENA_WORKGROUP"],
        "silver": dev_env["PROCURELAKE_DEV_SILVER_DATABASE"],
        "gold": dev_env["PROCURELAKE_DEV_GOLD_DATABASE"],
    }


def _one(cdc, database: str, sql: str):
    rows = run_query(cdc["athena"], sql, cdc["wg"], database).rows
    return rows[0] if rows else None


def test_award_update_and_invoice_delete_reach_gold(cdc):
    with psycopg.connect(cdc["dsn"]) as conn, conn.cursor(row_factory=dict_row) as cur:
        award = cur.execute(
            "UPDATE procurement.awards SET award_amount = award_amount + 1.23 "
            "WHERE award_id = (SELECT award_id FROM procurement.awards WHERE tenant_id = 'tenant_alder' "
            "ORDER BY award_id LIMIT 1) RETURNING award_id::text, award_amount, clock_timestamp() AS at"
        ).fetchone()
        conn.commit()
        invoice = cur.execute(
            "DELETE FROM procurement.invoices WHERE invoice_id = "
            "(SELECT invoice_id FROM procurement.invoices ORDER BY invoice_id DESC LIMIT 1) "
            "RETURNING invoice_id::text, clock_timestamp() AS at"
        ).fetchone()
        conn.commit()
    award_id, amount, invoice_id = award["award_id"], Decimal(award["award_amount"]), invoice["invoice_id"]

    award_where, invoice_where = f"where award_id = '{award_id}'", f"where invoice_id = '{invoice_id}'"
    checks = {
        "silver award amount": (
            cdc["silver"],
            f"select award_amount from silver_awards {award_where}",
            lambda r: r is not None and Decimal(r[0]) == amount,
        ),
        "gold award amount": (
            cdc["gold"],
            f"select award_amount from fct_contract_award {award_where}",
            lambda r: r is not None and Decimal(r[0]) == amount,
        ),
        "silver invoice tombstone": (
            cdc["silver"],
            f"select _is_deleted from silver_invoices {invoice_where}",
            lambda r: r is not None and r[0] == "true",
        ),
        "gold invoice absent": (
            cdc["gold"],
            f"select count(*) from fct_invoice {invoice_where}",
            lambda r: r is not None and int(r[0]) == 0,
        ),
    }
    seen: dict[str, str] = {}
    deadline = time.monotonic() + TIMEOUT_S
    while len(seen) < len(checks) and time.monotonic() < deadline:
        for name, (database, sql, ok) in checks.items():
            if name not in seen and ok(_one(cdc, database, sql)):
                seen[name] = datetime.now(UTC).isoformat()
        if len(seen) < len(checks):
            time.sleep(POLL_S)

    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(
        json.dumps(
            {
                "award_id": award_id,
                "new_amount": str(amount),
                "award_committed_at": award["at"].isoformat(),
                "invoice_id": invoice_id,
                "invoice_deleted_at": invoice["at"].isoformat(),
                "visible_at": seen,
            },
            indent=2,
        )
        + "\n"
    )
    assert seen.keys() == checks.keys(), (
        f"not visible within {TIMEOUT_S} s: {sorted(set(checks) - set(seen))}"
    )
