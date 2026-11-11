"""Write decided reviews to Silver: ``silver_contract_intelligence`` (Iceberg, through Athena).

Grain: one row per (document_id, extraction_id, field_name). Every decided extraction is
written, approved or rejected, so Silver is the full audit of what the model said, what the
checks found and what the reviewer decided. Gold keeps approved values only.

Rows are written with one Athena ``MERGE`` keyed on the grain, so re-applying a decision is
idempotent. The table is created on first use (Athena Iceberg DDL) at
``s3://<lake>/silver/contract_intelligence/``; the volume is a handful of rows per document,
so no partitioning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from procurelake.ai.review import ReviewItem
from procurelake.ai.schema import ALL_FIELDS
from procurelake.common.athena import run_query

TABLE = "silver_contract_intelligence"
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

# (column, Athena type) in table order.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("document_id", "string"),
    ("extraction_id", "string"),
    ("field_name", "string"),
    ("document_type", "string"),
    ("extracted_value", "string"),
    ("evidence_page", "int"),
    ("evidence_quote", "string"),
    ("check_status", "string"),
    ("check_reasons", "string"),
    ("check_notes", "string"),
    ("review_status", "string"),
    ("review_action", "string"),
    ("final_value", "string"),
    ("final_page", "int"),
    ("final_quote", "string"),
    ("currency", "string"),
    ("amount_type", "string"),
    ("reviewer", "string"),
    ("reviewer_arn", "string"),
    ("reviewed_at", "timestamp"),
    ("source_url", "string"),
    ("landing_page", "string"),
    ("license_basis", "string"),
    ("pdf_sha256", "string"),
    ("pdf_s3_key", "string"),
    ("usaspending_award_id", "string"),
    ("ocr_pages", "string"),
    ("model_id", "string"),
    ("response_model", "string"),
    ("model_request_id", "string"),
    ("prompt_version", "string"),
    ("schema_version", "string"),
    ("extracted_at", "timestamp"),
)
KEY = ("document_id", "extraction_id", "field_name")
_SQL_TYPES = {"string": "varchar", "int": "integer", "timestamp": "timestamp(6)"}


def _check_identifier(name: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def literal(value, athena_type: str) -> str:
    """A typed SQL literal. Strings are single-quoted with quotes doubled; every value is cast,
    so NULLs carry their type inside ``VALUES``."""
    sql_type = _SQL_TYPES[athena_type]
    if value is None:
        return f"CAST(NULL AS {sql_type})"
    if athena_type == "int":
        return f"CAST({int(value)} AS integer)"
    if athena_type == "timestamp":
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        if moment.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        text = moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        return f"TIMESTAMP '{text}'"
    text = str(value)
    if "\x00" in text:
        raise ValueError("NUL character in string value")
    return "'" + text.replace("'", "''") + "'"


@dataclass(frozen=True)
class SilverTarget:
    athena: object
    glue: object
    workgroup: str
    database: str
    location: str  # s3://<lake>/silver/contract_intelligence/


def rows_for(item: ReviewItem) -> list[dict]:
    """Silver rows for a decided review item."""
    if item.status not in ("approved", "rejected"):
        raise ValueError(f"{item.document_id}/{item.extraction_id} is not decided ({item.status})")
    record = item.record
    prov = record["provenance"]
    decisions = record.get("decisions") or {}
    extraction = item.extraction
    rows = []
    for name in ALL_FIELDS:
        ev = extraction[name]
        check = item.checks[name]
        decision = decisions.get(name)
        action = decision["action"] if decision else "not_extracted"
        if item.status == "rejected":
            action = "document_rejected"
        final = {"value": None, "page": None, "quote": None}
        currency = ev.get("currency")
        amount_type = ev.get("amount_type")
        if item.status == "approved" and decision:
            if decision["action"] == "accept":
                final = {"value": ev["value"], "page": ev["page"], "quote": ev["quote"]}
            elif decision["action"] == "correct":
                final = {"value": decision["value"], "page": decision["page"], "quote": decision.get("quote")}
                currency = decision.get("currency", currency)
                amount_type = decision.get("amount_type", amount_type)
        if final["value"] is None:
            currency = amount_type = None
        rows.append(
            {
                "document_id": item.document_id,
                "extraction_id": item.extraction_id,
                "field_name": name,
                "document_type": extraction["document_type"],
                "extracted_value": ev["value"],
                "evidence_page": ev["page"],
                "evidence_quote": ev["quote"],
                "check_status": check["status"],
                "check_reasons": ",".join(check["reasons"]) or None,
                "check_notes": " | ".join(check["notes"]) or None,
                "review_status": item.status,
                "review_action": action,
                "final_value": final["value"],
                "final_page": final["page"],
                "final_quote": final["quote"],
                "currency": currency if name == "contract_amount" else None,
                "amount_type": amount_type if name == "contract_amount" else None,
                "reviewer": record["reviewer"],
                "reviewer_arn": record["reviewer_arn"],
                "reviewed_at": record["decided_at"],
                "source_url": prov["source_url"],
                "landing_page": prov.get("landing_page"),
                "license_basis": prov["license_basis"],
                "pdf_sha256": prov["pdf_sha256"],
                "pdf_s3_key": prov["pdf_key"],
                "usaspending_award_id": prov["usaspending_award_id"],
                "ocr_pages": ",".join(str(p) for p in prov["ocr_pages"]) or None,
                "model_id": record["model_id"],
                "response_model": record["response_model"],
                "model_request_id": record.get("request_id"),
                "prompt_version": record["prompt_version"],
                "schema_version": record["schema_version"],
                "extracted_at": record["extracted_at"],
            }
        )
    return rows


def create_table_sql(database: str, location: str) -> str:
    if not location.startswith("s3://") or "'" in location:
        raise ValueError(f"bad table location: {location!r}")
    columns = ",\n  ".join(f"{name} {athena_type}" for name, athena_type in COLUMNS)
    return (
        f"CREATE TABLE {_check_identifier(database)}.{TABLE} (\n  {columns}\n)\n"
        f"LOCATION '{location}'\n"
        "TBLPROPERTIES ('table_type' = 'ICEBERG', 'format' = 'parquet', 'write_compression' = 'zstd')"
    )


def merge_sql(database: str, rows: list[dict]) -> str:
    if not rows:
        raise ValueError("no rows to merge")
    names = [c for c, _ in COLUMNS]
    values = ",\n  ".join("(" + ", ".join(literal(row[c], t) for c, t in COLUMNS) + ")" for row in rows)
    on = " AND ".join(f"t.{k} = s.{k}" for k in KEY)
    updates = ", ".join(f"{c} = s.{c}" for c in names if c not in KEY)
    return (
        f"MERGE INTO {_check_identifier(database)}.{TABLE} t\n"  # noqa: S608 - literals built by literal()
        f"USING (VALUES\n  {values}\n) AS s({', '.join(names)})\n"
        f"ON {on}\n"
        f"WHEN MATCHED THEN UPDATE SET {updates}\n"
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(names)}) VALUES ({', '.join('s.' + c for c in names)})"
    )


def ensure_table(target: SilverTarget) -> bool:
    """Create the table if the catalog does not have it. Returns True if it was created."""
    try:
        target.glue.get_table(DatabaseName=target.database, Name=TABLE)
        return False
    except target.glue.exceptions.EntityNotFoundException:
        run_query(
            target.athena,
            create_table_sql(target.database, target.location),
            target.workgroup,
            target.database,
        )
        return True


def write_decision(target: SilverTarget, item: ReviewItem) -> int:
    ensure_table(target)
    rows = rows_for(item)
    run_query(target.athena, merge_sql(target.database, rows), target.workgroup, target.database)
    return len(rows)
