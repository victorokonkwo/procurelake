"""Transactional workload for CDC (implementation plan C1): inserts, updates, cancellations and
hard deletes against a seeded operational database.

Every operation respects the schema's constraints and the generator's business rules:

* ``pay_invoice``: pending -> paid (paid_amount = invoice_amount, paid_at set)
* ``partially_pay_invoice``: pending -> partially_paid (half, rounded down to the cent)
* ``cancel_invoice``: pending -> cancelled
* ``delete_invoice``: hard delete of a pending invoice
* ``new_invoice``: insert a pending invoice on an open purchase order, in its currency
* ``cancel_award``: active -> cancelled
* ``rename_supplier``: change a supplier's registered name
* ``settle_purchase_order``: in **one transaction**, pay every pending invoice of an open
  purchase order and close it. Two tables change together, which exercises the documented
  limit that DMS Parquet output does not preserve cross-table transaction order.

Runs are deterministic for a given database state and seed: candidates are ordered by primary
key and picked with a seeded RNG. Every operation returns a ``Change`` per row it touched, with
the row's after-image (``None`` for deletes) and the transaction's commit-side timestamp, so CDC
tests can check exactly what Silver and Gold must show afterwards.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Literal

import psycopg
from psycopg.rows import dict_row

OPERATIONS = (
    "pay_invoice",
    "partially_pay_invoice",
    "cancel_invoice",
    "delete_invoice",
    "new_invoice",
    "cancel_award",
    "rename_supplier",
    "settle_purchase_order",
)
# Relative frequencies: payments and new invoices dominate, as in a real payables flow.
WEIGHTS = (6, 2, 1, 1, 4, 1, 1, 2)


@dataclass(frozen=True)
class Change:
    operation: str
    table: str
    kind: Literal["I", "U", "D"]
    key: str
    tenant_id: str
    after: dict | None
    transaction_id: int
    committed_at: datetime


@dataclass
class WorkloadResult:
    seed: int
    changes: list[Change] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for c in self.changes:
            counts[c.kind] = counts.get(c.kind, 0) + 1
        return counts


class _NoCandidate(Exception):
    pass


def _pick(cur, rng: random.Random, sql: str, params: tuple = ()) -> dict:
    rows = cur.execute(sql, params).fetchall()
    if not rows:
        raise _NoCandidate
    return rows[rng.randrange(len(rows))]


def _tx_meta(cur) -> tuple[int, datetime]:
    row = cur.execute("SELECT txid_current() AS txid, clock_timestamp() AS ts").fetchone()
    return row["txid"], row["ts"]


def _pending_invoice(cur, rng):
    return _pick(
        cur, rng, "SELECT * FROM procurement.invoices WHERE payment_status = 'pending' ORDER BY invoice_id"
    )


def _update_invoice(cur, invoice_id, status: str, paid: Decimal, paid_at_now: bool) -> dict:
    return cur.execute(
        """
        UPDATE procurement.invoices
        SET payment_status = %s, paid_amount = %s,
            paid_at = CASE WHEN %s THEN clock_timestamp() ELSE NULL END
        WHERE invoice_id = %s
        RETURNING *
        """,
        (status, paid, paid_at_now, invoice_id),
    ).fetchone()


def _apply(cur, rng: random.Random, operation: str) -> list[tuple[str, str, str, str, dict | None]]:
    """Run one operation; return (table, kind, key, tenant_id, after) per changed row."""
    if operation == "pay_invoice":
        inv = _pending_invoice(cur, rng)
        row = _update_invoice(cur, inv["invoice_id"], "paid", inv["invoice_amount"], True)
        return [("invoices", "U", str(inv["invoice_id"]), inv["tenant_id"], row)]
    if operation == "partially_pay_invoice":
        inv = _pick(
            cur,
            rng,
            "SELECT * FROM procurement.invoices WHERE payment_status = 'pending' "
            "AND invoice_amount >= 0.02 ORDER BY invoice_id",
        )
        half = (inv["invoice_amount"] / 2).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        row = _update_invoice(cur, inv["invoice_id"], "partially_paid", half, True)
        return [("invoices", "U", str(inv["invoice_id"]), inv["tenant_id"], row)]
    if operation == "cancel_invoice":
        inv = _pending_invoice(cur, rng)
        row = _update_invoice(cur, inv["invoice_id"], "cancelled", Decimal("0"), False)
        return [("invoices", "U", str(inv["invoice_id"]), inv["tenant_id"], row)]
    if operation == "delete_invoice":
        inv = _pending_invoice(cur, rng)
        cur.execute("DELETE FROM procurement.invoices WHERE invoice_id = %s", (inv["invoice_id"],))
        return [("invoices", "D", str(inv["invoice_id"]), inv["tenant_id"], None)]
    if operation == "new_invoice":
        po = _pick(cur, rng, "SELECT * FROM procurement.purchase_orders WHERE status = 'open' ORDER BY po_id")
        share = Decimal(rng.randint(5, 30)) / Decimal(100)
        amount = max(Decimal("0.01"), (po["amount"] * share).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
        row = cur.execute(
            """
            INSERT INTO procurement.invoices
                (invoice_id, tenant_id, po_id, invoice_date, invoice_amount, paid_amount, currency_code,
                 payment_status)
            VALUES (%s, %s, %s, current_date, %s, 0, %s, 'pending')
            RETURNING *
            """,
            (
                uuid.UUID(int=rng.getrandbits(128), version=4),
                po["tenant_id"],
                po["po_id"],
                amount,
                po["currency_code"],
            ),
        ).fetchone()
        return [("invoices", "I", str(row["invoice_id"]), row["tenant_id"], row)]
    if operation == "cancel_award":
        award = _pick(cur, rng, "SELECT * FROM procurement.awards WHERE status = 'active' ORDER BY award_id")
        row = cur.execute(
            "UPDATE procurement.awards SET status = 'cancelled' WHERE award_id = %s RETURNING *",
            (award["award_id"],),
        ).fetchone()
        return [("awards", "U", str(award["award_id"]), award["tenant_id"], row)]
    if operation == "rename_supplier":
        sup = _pick(cur, rng, "SELECT * FROM procurement.suppliers ORDER BY supplier_id")
        suffix = rng.choice(["Inc.", "Ltd.", "Group", "Holdings", "Services"])
        base = sup["supplier_name"].rsplit(" (", 1)[0]
        row = cur.execute(
            "UPDATE procurement.suppliers SET supplier_name = %s WHERE supplier_id = %s RETURNING *",
            (f"{base} ({suffix})", sup["supplier_id"]),
        ).fetchone()
        return [("suppliers", "U", str(sup["supplier_id"]), sup["tenant_id"], row)]
    if operation == "settle_purchase_order":
        po = _pick(
            cur,
            rng,
            """
            SELECT po.* FROM procurement.purchase_orders po
            WHERE po.status = 'open' AND EXISTS (
                SELECT 1 FROM procurement.invoices i
                WHERE i.po_id = po.po_id AND i.payment_status = 'pending')
            ORDER BY po.po_id
            """,
        )
        changed = []
        pending = cur.execute(
            "SELECT * FROM procurement.invoices WHERE po_id = %s AND payment_status = 'pending' "
            "ORDER BY invoice_id",
            (po["po_id"],),
        ).fetchall()
        for inv in pending:
            row = _update_invoice(cur, inv["invoice_id"], "paid", inv["invoice_amount"], True)
            changed.append(("invoices", "U", str(inv["invoice_id"]), inv["tenant_id"], row))
        row = cur.execute(
            "UPDATE procurement.purchase_orders SET status = 'closed' WHERE po_id = %s RETURNING *",
            (po["po_id"],),
        ).fetchone()
        changed.append(("purchase_orders", "U", str(po["po_id"]), po["tenant_id"], row))
        return changed
    raise ValueError(f"unknown operation {operation!r}")


def run_workload(conn: psycopg.Connection, seed: int, operations: int) -> WorkloadResult:
    """Apply ``operations`` operations, each in its own committed transaction."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test workload, not crypto
    result = WorkloadResult(seed=seed)
    for _ in range(operations):
        operation = rng.choices(OPERATIONS, weights=WEIGHTS)[0]
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            try:
                rows = _apply(cur, rng, operation)
            except _NoCandidate:
                result.skipped[operation] = result.skipped.get(operation, 0) + 1
                continue
            txid, ts = _tx_meta(cur)
        for table, kind, key, tenant, after in rows:
            result.changes.append(Change(operation, table, kind, key, tenant, after, txid, ts))
    return result
