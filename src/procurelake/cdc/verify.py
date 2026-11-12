"""Check that CDC changes reached Silver and Gold (live verification, ADR 0028/0029).

For the latest change of every key: a delete must be a tombstone in Silver and absent from Gold;
any other change must show its after-image value in Silver (not deleted) and in Gold.
"""

from __future__ import annotations

import re

from procurelake.common.athena import run_query

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# table -> (primary key, Silver value column, Gold model, Gold value column)
CHECKS = {
    "invoices": ("invoice_id", "payment_status", "fct_invoice", "payment_status"),
    "awards": ("award_id", "status", "fct_contract_award", "award_status"),
    "suppliers": ("supplier_id", "supplier_name", "dim_supplier", "supplier_name"),
    "purchase_orders": ("po_id", "status", "fct_purchase_order", "po_status"),
}


# Athena caps a query string at 262,144 bytes; a quoted UUID plus separator is 40 bytes, so
# 2,000 keys (~80 KB) per query leaves ample room for the rest of the statement.
KEYS_PER_QUERY = 2000


def _keys_sql(keys: list[str]) -> str:
    for k in keys:
        if not _UUID.match(k):
            raise ValueError(f"refusing to place {k!r} in SQL")
    return ", ".join(f"'{k}'" for k in keys)


def check_layers(
    athena, workgroup: str, silver_db: str, gold_db: str, changes_list
) -> tuple[list[str], list[str]]:
    """Final state of every changed key: (Silver mismatches, Gold mismatches)."""
    latest = {}
    for c in changes_list:
        latest[(c.table, c.key)] = c
    silver_bad: list[str] = []
    gold_bad: list[str] = []
    for table, (pk, silver_col, gold_model, gold_col) in CHECKS.items():
        changes = {k: c for (t, k), c in latest.items() if t == table}
        if not changes:
            continue
        silver: dict = {}
        gold: dict = {}
        ordered = sorted(changes)
        for start in range(0, len(ordered), KEYS_PER_QUERY):
            keys = _keys_sql(ordered[start : start + KEYS_PER_QUERY])
            silver.update(
                (r[0], (r[1], r[2]))
                for r in run_query(
                    athena,
                    f"SELECT {pk}, {silver_col}, coalesce(_is_deleted, false) "  # noqa: S608
                    f"FROM silver_{table} WHERE {pk} IN ({keys})",
                    workgroup,
                    silver_db,
                ).rows
            )
            gold.update(
                (r[0], r[1])
                for r in run_query(
                    athena,
                    f"SELECT {pk}, {gold_col} FROM {gold_model} WHERE {pk} IN ({keys})",  # noqa: S608
                    workgroup,
                    gold_db,
                ).rows
            )
        for key, change in changes.items():
            if change.kind == "D":
                if silver.get(key, (None, "false"))[1] != "true":
                    silver_bad.append(f"silver_{table} {key}: not a tombstone")
                if key in gold:
                    gold_bad.append(f"{gold_model} {key}: deleted key still in Gold")
                continue
            want = str(change.after[silver_col])
            if silver.get(key) != (want, "false"):
                silver_bad.append(f"silver_{table} {key}: {silver.get(key)} != {(want, 'false')}")
            if gold.get(key) != want:
                gold_bad.append(f"{gold_model} {key}: {gold.get(key)} != {want}")
    return silver_bad, gold_bad
