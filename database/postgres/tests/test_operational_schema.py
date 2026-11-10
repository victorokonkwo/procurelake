"""Integration tests for the operational schema (Sprint 1, Day 1 definition of done:
"All source tables exist and have constraints")."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.postgres

EXPECTED_TABLES = {
    "tenants": "tenant_id",
    "agencies": "agency_id",
    "suppliers": "supplier_id",
    "solicitations": "solicitation_id",
    "awards": "award_id",
    "purchase_orders": "po_id",
    "invoices": "invoice_id",
    "contract_documents": "document_id",
}

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers
def _savepoint_raises(conn: psycopg.Connection, exc: type[Exception], query: str, params: tuple) -> None:
    """Assert that ``query`` fails with ``exc`` without aborting the outer test transaction."""
    with pytest.raises(exc), conn.transaction():
        conn.execute(query, params)


def _tenant(conn: psycopg.Connection, tenant_id: str) -> str:
    conn.execute(
        "INSERT INTO procurement.tenants (tenant_id, tenant_name, is_synthetic) VALUES (%s, %s, true)",
        (tenant_id, f"Synthetic {tenant_id}"),
    )
    return tenant_id


def _agency(conn, tenant_id: str, name: str = "Agency A") -> uuid.UUID:
    return conn.execute(
        "INSERT INTO procurement.agencies (tenant_id, agency_name) VALUES (%s, %s) RETURNING agency_id",
        (tenant_id, name),
    ).fetchone()[0]


def _supplier(conn, tenant_id: str) -> uuid.UUID:
    return conn.execute(
        "INSERT INTO procurement.suppliers (tenant_id, supplier_name, category) "
        "VALUES (%s, 'Supplier S', 'IT services') RETURNING supplier_id",
        (tenant_id,),
    ).fetchone()[0]


def _solicitation(conn, tenant_id: str, agency_id: uuid.UUID) -> uuid.UUID:
    return conn.execute(
        "INSERT INTO procurement.solicitations (tenant_id, agency_id, title, status, published_at) "
        "VALUES (%s, %s, 'Network refresh', 'published', %s) RETURNING solicitation_id",
        (tenant_id, agency_id, NOW - timedelta(days=30)),
    ).fetchone()[0]


def _award(conn, tenant_id, solicitation_id, supplier_id, currency="CAD") -> uuid.UUID:
    return conn.execute(
        "INSERT INTO procurement.awards "
        "(tenant_id, solicitation_id, supplier_id, award_amount, currency_code, awarded_at) "
        "VALUES (%s, %s, %s, 125000.00, %s, %s) RETURNING award_id",
        (tenant_id, solicitation_id, supplier_id, currency, NOW),
    ).fetchone()[0]


def _po(conn, tenant_id, agency_id, supplier_id, currency="CAD") -> uuid.UUID:
    return conn.execute(
        "INSERT INTO procurement.purchase_orders "
        "(tenant_id, agency_id, supplier_id, order_date, amount, currency_code) "
        "VALUES (%s, %s, %s, %s, 50000.00, %s) RETURNING po_id",
        (tenant_id, agency_id, supplier_id, date(2026, 1, 20), currency),
    ).fetchone()[0]


@pytest.fixture
def alder(conn):
    """A fully related graph of rows for tenant_alder."""
    t = _tenant(conn, "tenant_alder")
    agency = _agency(conn, t)
    supplier = _supplier(conn, t)
    solicitation = _solicitation(conn, t, agency)
    award = _award(conn, t, solicitation, supplier)
    po = _po(conn, t, agency, supplier)
    return {
        "tenant": t,
        "agency": agency,
        "supplier": supplier,
        "solicitation": solicitation,
        "award": award,
        "po": po,
    }


# --------------------------------------------------------------------------- structure
def test_all_source_tables_exist_with_expected_primary_keys(conn):
    rows = conn.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        WHERE tc.table_schema = 'procurement' AND tc.constraint_type = 'PRIMARY KEY'
        """
    ).fetchall()
    assert dict(rows) == EXPECTED_TABLES


def test_every_tenant_owned_table_has_non_null_tenant_id(conn):
    rows = conn.execute(
        """
        SELECT table_name, is_nullable FROM information_schema.columns
        WHERE table_schema = 'procurement' AND column_name = 'tenant_id'
        """
    ).fetchall()
    assert {name for name, _ in rows} == set(EXPECTED_TABLES)
    assert all(nullable == "NO" for _, nullable in rows)


def test_every_table_has_updated_at_trigger(conn):
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT event_object_table FROM information_schema.triggers "
            "WHERE trigger_schema = 'procurement' AND action_timing = 'BEFORE' "
            "AND event_manipulation = 'UPDATE'"
        ).fetchall()
    }
    assert triggers == set(EXPECTED_TABLES)


# --------------------------------------------------------------------------- happy path
def test_valid_related_graph_and_invoice_can_be_inserted(conn, alder):
    conn.execute(
        "INSERT INTO procurement.invoices "
        "(tenant_id, po_id, invoice_date, invoice_amount, paid_amount, currency_code, "
        "payment_status, paid_at) "
        "VALUES (%s, %s, %s, 10000.00, 10000.00, 'CAD', 'paid', %s)",
        (alder["tenant"], alder["po"], date(2026, 2, 1), NOW + timedelta(days=20)),
    )
    conn.execute(
        "INSERT INTO procurement.contract_documents (tenant_id, award_id, document_s3_key) "
        "VALUES (%s, %s, 'documents/tenant_alder/contract-001.pdf')",
        (alder["tenant"], alder["award"]),
    )
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM procurement.invoices), "
        "(SELECT count(*) FROM procurement.contract_documents)"
    ).fetchone()
    assert counts == (1, 1)


# --------------------------------------------------------------------------- tenant isolation
def test_award_cannot_reference_supplier_of_another_tenant(conn, alder):
    birch = _tenant(conn, "tenant_birch")
    birch_supplier = _supplier(conn, birch)
    _savepoint_raises(
        conn,
        errors.ForeignKeyViolation,
        "INSERT INTO procurement.awards "
        "(tenant_id, solicitation_id, supplier_id, award_amount, currency_code, awarded_at) "
        "VALUES (%s, %s, %s, 1.00, 'CAD', %s)",
        (alder["tenant"], alder["solicitation"], birch_supplier, NOW),
    )


def test_solicitation_cannot_reference_agency_of_another_tenant(conn, alder):
    birch = _tenant(conn, "tenant_birch")
    _savepoint_raises(
        conn,
        errors.ForeignKeyViolation,
        "INSERT INTO procurement.solicitations (tenant_id, agency_id, title) VALUES (%s, %s, 'x')",
        (birch, alder["agency"]),
    )


def test_unknown_tenant_is_rejected(conn):
    _savepoint_raises(
        conn,
        errors.ForeignKeyViolation,
        "INSERT INTO procurement.agencies (tenant_id, agency_name) VALUES (%s, 'x')",
        ("tenant_unknown",),
    )


@pytest.mark.parametrize("bad_id", ["alder", "Tenant_Alder", "tenant_", "tenant-alder"])
def test_tenant_id_format_enforced(conn, bad_id):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.tenants (tenant_id, tenant_name, is_synthetic) VALUES (%s, 'x', true)",
        (bad_id,),
    )


# --------------------------------------------------------------------------- monetary rules
@pytest.mark.parametrize(
    ("currency", "expected"),
    [
        ("cad", errors.CheckViolation),
        ("CA", errors.CheckViolation),  # char(3) pads to 'CA ', which fails the format check
        ("12A", errors.CheckViolation),
        ("CADX", errors.StringDataRightTruncation),  # rejected by the char(3) length itself
    ],
)
def test_currency_code_format_enforced(conn, alder, currency, expected):
    _savepoint_raises(
        conn,
        expected,
        "INSERT INTO procurement.awards "
        "(tenant_id, solicitation_id, supplier_id, award_amount, currency_code, awarded_at) "
        "VALUES (%s, %s, %s, 1.00, %s, %s)",
        (alder["tenant"], alder["solicitation"], alder["supplier"], currency, NOW),
    )


@pytest.mark.parametrize("amount", ["0", "-1.00"])
def test_award_amount_must_be_positive(conn, alder, amount):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.awards "
        "(tenant_id, solicitation_id, supplier_id, award_amount, currency_code, awarded_at) "
        "VALUES (%s, %s, %s, %s, 'CAD', %s)",
        (alder["tenant"], alder["solicitation"], alder["supplier"], amount, NOW),
    )


def test_invoice_currency_must_match_purchase_order(conn, alder):
    _savepoint_raises(
        conn,
        errors.ForeignKeyViolation,
        "INSERT INTO procurement.invoices (tenant_id, po_id, invoice_date, invoice_amount, currency_code) "
        "VALUES (%s, %s, %s, 100.00, 'USD')",
        (alder["tenant"], alder["po"], date(2026, 2, 1)),
    )


def test_invoice_cannot_be_overpaid(conn, alder):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.invoices "
        "(tenant_id, po_id, invoice_date, invoice_amount, paid_amount, currency_code, "
        "payment_status, paid_at) "
        "VALUES (%s, %s, %s, 100.00, 150.00, 'CAD', 'paid', %s)",
        (alder["tenant"], alder["po"], date(2026, 2, 1), NOW),
    )


@pytest.mark.parametrize(
    ("status", "paid", "paid_at_set"),
    [
        ("paid", "50.00", True),  # paid but not fully
        ("pending", "10.00", False),  # pending with money paid
        ("partially_paid", "0", True),  # partial with nothing paid
        ("paid", "100.00", False),  # paid without paid_at
    ],
)
def test_payment_status_must_agree_with_amounts(conn, alder, status, paid, paid_at_set):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.invoices "
        "(tenant_id, po_id, invoice_date, invoice_amount, paid_amount, currency_code, "
        "payment_status, paid_at) "
        "VALUES (%s, %s, %s, 100.00, %s, 'CAD', %s, %s)",
        (alder["tenant"], alder["po"], date(2026, 2, 1), paid, status, NOW if paid_at_set else None),
    )


# --------------------------------------------------------------------------- lifecycle rules
def test_published_solicitation_requires_published_at(conn, alder):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.solicitations (tenant_id, agency_id, title, status) "
        "VALUES (%s, %s, 'x', 'published')",
        (alder["tenant"], alder["agency"]),
    )


def test_solicitation_closing_must_follow_publication(conn, alder):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.solicitations "
        "(tenant_id, agency_id, title, status, published_at, closing_at) "
        "VALUES (%s, %s, 'x', 'published', %s, %s)",
        (alder["tenant"], alder["agency"], NOW, NOW - timedelta(days=1)),
    )


def test_document_key_must_not_include_scheme(conn, alder):
    _savepoint_raises(
        conn,
        errors.CheckViolation,
        "INSERT INTO procurement.contract_documents (tenant_id, award_id, document_s3_key) "
        "VALUES (%s, %s, 's3://bucket/doc.pdf')",
        (alder["tenant"], alder["award"]),
    )


def test_parent_with_children_cannot_be_hard_deleted(conn, alder):
    _savepoint_raises(
        conn,
        errors.ForeignKeyViolation,
        "DELETE FROM procurement.suppliers WHERE supplier_id = %s",
        (alder["supplier"],),
    )


# --------------------------------------------------------------------------- CDC support
def test_updated_at_advances_on_update(conn, migrated_dsn):
    # Needs separate committed transactions: now() is fixed within one transaction.
    tenant_id = f"tenant_t{uuid.uuid4().hex[:8]}"
    with psycopg.connect(migrated_dsn, autocommit=True) as c:
        c.execute(
            "INSERT INTO procurement.tenants (tenant_id, tenant_name, is_synthetic) VALUES (%s, %s, true)",
            (tenant_id, tenant_id),
        )
        try:
            before = c.execute(
                "SELECT updated_at FROM procurement.tenants WHERE tenant_id = %s", (tenant_id,)
            ).fetchone()[0]
            c.execute(
                "UPDATE procurement.tenants SET status = 'suspended' WHERE tenant_id = %s", (tenant_id,)
            )
            after = c.execute(
                "SELECT updated_at FROM procurement.tenants WHERE tenant_id = %s", (tenant_id,)
            ).fetchone()[0]
        finally:
            c.execute("DELETE FROM procurement.tenants WHERE tenant_id = %s", (tenant_id,))
    assert after > before
