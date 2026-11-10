"""Row types for the operational schema.

Field names and order match the columns of the ``procurement`` tables in
``database/postgres/migrations``. The loader relies on this to build COPY statements.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import astuple, dataclass, field, fields
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar
from uuid import UUID


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    tenant_name: str
    status: str
    is_synthetic: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Agency:
    agency_id: UUID
    tenant_id: str
    agency_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Supplier:
    supplier_id: UUID
    tenant_id: str
    supplier_name: str
    category: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Solicitation:
    solicitation_id: UUID
    tenant_id: str
    agency_id: UUID
    title: str
    status: str
    published_at: datetime | None
    closing_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Award:
    award_id: UUID
    tenant_id: str
    solicitation_id: UUID
    supplier_id: UUID
    award_amount: Decimal
    currency_code: str
    awarded_at: datetime
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PurchaseOrder:
    po_id: UUID
    tenant_id: str
    agency_id: UUID
    supplier_id: UUID
    award_id: UUID | None
    order_date: date
    amount: Decimal
    currency_code: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class Invoice:
    invoice_id: UUID
    tenant_id: str
    po_id: UUID
    invoice_date: date
    invoice_amount: Decimal
    paid_amount: Decimal
    currency_code: str
    payment_status: str
    paid_at: datetime | None
    created_at: datetime
    updated_at: datetime


Row = Tenant | Agency | Supplier | Solicitation | Award | PurchaseOrder | Invoice


def column_names(row_type: type) -> list[str]:
    return [f.name for f in fields(row_type)]


@dataclass
class Dataset:
    """All generated rows, in foreign-key load order."""

    tenants: list[Tenant] = field(default_factory=list)
    agencies: list[Agency] = field(default_factory=list)
    suppliers: list[Supplier] = field(default_factory=list)
    solicitations: list[Solicitation] = field(default_factory=list)
    awards: list[Award] = field(default_factory=list)
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)

    ROW_TYPES: ClassVar[dict[str, type]] = {
        "tenants": Tenant,
        "agencies": Agency,
        "suppliers": Supplier,
        "solicitations": Solicitation,
        "awards": Award,
        "purchase_orders": PurchaseOrder,
        "invoices": Invoice,
    }

    def tables(self) -> Iterator[tuple[str, type, list]]:
        for table, row_type in self.ROW_TYPES.items():
            yield table, row_type, getattr(self, table)

    def extend(self, other: Dataset) -> None:
        for table, _, rows in other.tables():
            getattr(self, table).extend(rows)

    def counts(self) -> dict[str, int]:
        return {table: len(rows) for table, _, rows in self.tables()}

    def fingerprint(self) -> str:
        """SHA-256 over every row in load order; equal fingerprints mean identical datasets."""
        digest = hashlib.sha256()
        for table, _, rows in self.tables():
            digest.update(table.encode())
            for row in rows:
                digest.update(_canonical(astuple(row)).encode())
        return digest.hexdigest()


def _canonical(values: tuple) -> str:
    def fmt(value: object) -> str:
        if value is None:
            return "\\N"
        if isinstance(value, datetime | date):
            return value.isoformat()
        return str(value)

    return "\x1f".join(fmt(v) for v in values) + "\n"
