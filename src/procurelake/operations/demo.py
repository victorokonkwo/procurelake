"""End-to-end demo helpers (implementation plan section 4, `make demo STEP=…`).

One award is followed through every layer: PostgreSQL → DMS → Bronze → Silver → Gold, then
checked from both tenants' side. This module holds the parts that can be tested without AWS:
the SQL the demo runs (keys validated, so nothing typed on the command line reaches a query
unchecked) and the selection of the award's change events from a DMS Parquet file.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

PICK_AWARD_SQL = """
select a.award_id, a.tenant_id, s.agency_id, a.supplier_id, a.award_amount, a.currency_code,
       a.status, a.awarded_at, a.updated_at
from procurement.awards a
join procurement.solicitations s
  on s.tenant_id = a.tenant_id and s.solicitation_id = a.solicitation_id
where a.tenant_id = %(tenant)s and a.status = 'active'
  and (%(award_id)s::uuid is null or a.award_id = %(award_id)s::uuid)
order by a.award_id
limit 1
"""

UPDATE_AWARD_SQL = """
update procurement.awards set award_amount = %(amount)s
where award_id = %(award_id)s::uuid and tenant_id = %(tenant)s
returning award_id, award_amount, currency_code, updated_at, clock_timestamp() as committed_at
"""

TENANTS = ("tenant_alder", "tenant_birch")


def checked_uuid(value: str) -> str:
    """Return ``value`` as a canonical UUID string, or raise ``ValueError``."""
    return str(uuid.UUID(str(value)))


def checked_amount(value: str) -> Decimal:
    """A positive amount with at most 2 decimals that fits ``numeric(18,2)``."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2 or amount >= Decimal("1e16"):
        raise ValueError(f"amount must be positive, at most 2 decimals and fit numeric(18,2): {value}")
    return amount


def _identifier(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier: {name}")
    return name


def silver_award_sql(silver_db: str, award_id: str) -> str:
    db, key = _identifier(silver_db), checked_uuid(award_id)
    # Identifiers validated above; the key is a canonical UUID.
    return (
        "select award_id, tenant_id, award_amount, currency_code, status, updated_at, "  # noqa: S608
        "_cdc_op, _source_commit_ts, _source_file, _batch_id, coalesce(_is_deleted, false) as is_deleted "
        f"from {db}.silver_awards where award_id = '{key}'"
    )


def gold_fact_sql(gold_db: str, award_id: str) -> str:
    db, key = _identifier(gold_db), checked_uuid(award_id)
    return (
        "select award_id, tenant_id, award_amount, currency_code, award_status, award_month "  # noqa: S608
        f"from {db}.fct_contract_award where award_id = '{key}'"
    )


def gold_mart_sql(gold_db: str, agency_id: str, supplier_id: str) -> str:
    """The award's group in ``mart_awarded_value``. Runs as a tenant: the row filter applies."""
    db, agency, supplier = _identifier(gold_db), checked_uuid(agency_id), checked_uuid(supplier_id)
    return (
        "select tenant_id, award_month, currency_code, award_count, awarded_value "  # noqa: S608
        f"from {db}.mart_awarded_value where agency_id = '{agency}' and supplier_id = '{supplier}' "
        "order by award_month"
    )


def award_events(rows: Iterable[Mapping[str, Any]], award_id: str) -> list[dict[str, Any]]:
    """The DMS change rows for ``award_id`` (``Op`` I/U/D), oldest commit first."""
    key = checked_uuid(award_id)
    found = [
        {
            "op": r.get("Op"),
            "commit_ts": r.get("_dms_commit_ts"),
            "award_amount": r.get("award_amount"),
            "status": r.get("status"),
        }
        for r in rows
        if str(r.get("award_id")) == key
    ]
    return sorted(found, key=lambda e: str(e["commit_ts"]))
