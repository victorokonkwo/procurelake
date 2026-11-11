"""Iceberg partition evolution with Spark: add a partition field, then optionally rewrite (ADR 0034).

Athena cannot change an Iceberg table's partition spec (it has no ``ADD PARTITION FIELD``), so
this runs in Spark (Glue 5.1, Iceberg 1.10):

1. ``ALTER TABLE t ADD PARTITION FIELD <transform>(<column>)``. Iceberg documents this as a
   metadata-only change: files written before it keep the old (unpartitioned) layout, and new
   writes use the new spec.
2. Optionally ``CALL <catalog>.system.rewrite_data_files(table => ..., options =>
   map('rewrite-all', 'true'))``, which rewrites every data file. The rewrite targets the table's
   current spec (option ``output-spec-id``, default the current spec).

Layout is measured before and after from the ``files`` metadata table: data files and bytes per
``spec_id``, so it shows exactly which files are still in the old layout.

Layout is read from the ``files`` metadata table only. Iceberg 1.10's ``partitions`` metadata
table failed while a table held files of two specs at once (``Cannot find source column for
partition field … identity(…)``, CI 2026-11-11), which is exactly the state between adding a field
and rewriting.

Only the transforms used here are accepted, and identifiers are checked, because the statements
are built as SQL text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
TRANSFORMS = ("years", "months", "days")
FIELD = re.compile(r"^(?P<table>[a-z_][a-z0-9_]*)=(?P<transform>[a-z]+)\((?P<column>[a-z_][a-z0-9_]*)\)$")


@dataclass(frozen=True)
class PartitionField:
    table: str
    transform: str
    column: str

    def __post_init__(self) -> None:
        if self.transform not in TRANSFORMS:
            raise ValueError(f"unsupported transform {self.transform!r}; use one of {TRANSFORMS}")
        for name in (self.table, self.column):
            if not IDENTIFIER.fullmatch(name):
                raise ValueError(f"unsafe identifier: {name!r}")

    @property
    def expression(self) -> str:
        return f"{self.transform}({self.column})"

    @classmethod
    def parse(cls, text: str) -> PartitionField:
        """``silver_invoices=months(invoice_date)``"""
        m = FIELD.fullmatch(text.strip())
        if not m:
            raise ValueError(f"expected table=transform(column), got {text!r}")
        return cls(m["table"], m["transform"], m["column"])


def parse_fields(csv: str) -> list[PartitionField]:
    fields = [PartitionField.parse(t) for t in csv.split(",") if t.strip()]
    tables = [f.table for f in fields]
    if len(set(tables)) != len(tables):
        raise ValueError("one partition field per table per run")
    return fields


@dataclass(frozen=True)
class Layout:
    """Data files and bytes per partition spec id, and the current spec's fields."""

    files_by_spec: dict[int, int]
    bytes_by_spec: dict[int, int]
    spec: list[str]

    @property
    def data_files(self) -> int:
        return sum(self.files_by_spec.values())


def _check(*names: str) -> None:
    for name in names:
        if not IDENTIFIER.fullmatch(name):
            raise ValueError(f"unsafe identifier: {name!r}")


def parse_describe_partitioning(rows: list[tuple]) -> list[str]:
    """The current spec's fields from ``DESCRIBE TABLE EXTENDED`` rows (col_name, data_type, ...).

    Spark prints partitioning in one of two forms:

    * specs with a transform under ``# Partitioning``, as ``Part <n>`` rows whose second cell
      is the field (``tenant_id``, ``months(invoice_date)``). Seen on Glue 5.1, 2026-11-11;
    * identity-only specs under ``# Partition Information``, after a ``# col_name`` header row,
      one column per row (``tenant_id``). On Glue the ``# Partitioning`` parser found no rows for
      the identity-only Silver tables, which is consistent with this form; the parse is checked
      against the table's own ``partitions`` metadata in the job report.

    An unpartitioned table gives ``[]``.
    """
    partitioning: list[str] = []
    information: list[str] = []
    section = None
    for r in rows:
        name = (r[0] or "").strip()
        if name in ("# Partitioning", "# Partition Information"):
            section = name
            continue
        if section == "# Partitioning":
            if name.startswith("Part "):
                partitioning.append((r[1] or "").strip())
                continue
            section = None
        elif section == "# Partition Information":
            if name == "# col_name":
                continue
            if name and not name.startswith("#"):
                information.append(name)
                continue
            section = None
    return partitioning or information


def partition_expressions(spark, fq_table: str) -> list[str]:
    rows = spark.sql(f"DESCRIBE TABLE EXTENDED {fq_table}").collect()
    return parse_describe_partitioning([tuple(r) for r in rows])


def layout(spark, fq_table: str) -> Layout:
    rows = spark.sql(
        f"SELECT spec_id, count(*) AS files, coalesce(sum(file_size_in_bytes), 0) AS bytes "  # noqa: S608
        f"FROM {fq_table}.files WHERE content = 0 GROUP BY spec_id"
    ).collect()
    return Layout(
        files_by_spec={int(r.spec_id): int(r.files) for r in rows},
        bytes_by_spec={int(r.spec_id): int(r.bytes) for r in rows},
        spec=partition_expressions(spark, fq_table),
    )


@dataclass
class EvolutionResult:
    table: str
    field: str
    added: bool  # False when the field was already in the spec (idempotent rerun)
    before: Layout
    after: Layout
    rewrite: dict | None = None


def evolve(spark, catalog: str, database: str, field: PartitionField, rewrite: bool) -> EvolutionResult:
    _check(catalog, database)
    fq = f"{catalog}.{database}.{field.table}"
    before = layout(spark, fq)
    added = field.expression not in before.spec
    if added:
        spark.sql(f"ALTER TABLE {fq} ADD PARTITION FIELD {field.expression}")
        logger.info("%s: added partition field %s", fq, field.expression)
    else:
        logger.info("%s: already partitioned by %s", fq, field.expression)
    rewrite_result = None
    if rewrite:
        row = spark.sql(
            f"CALL {catalog}.system.rewrite_data_files("
            f"table => '{database}.{field.table}', options => map('rewrite-all', 'true'))"
        ).collect()[0]
        rewrite_result = row.asDict()
        logger.info("%s: rewrite %s", fq, rewrite_result)
    return EvolutionResult(field.table, field.expression, added, before, layout(spark, fq), rewrite_result)
