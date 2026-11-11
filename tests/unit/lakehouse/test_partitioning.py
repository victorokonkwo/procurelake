"""Partition-evolution inputs (no Spark): field parsing and validation, job arguments.

The Spark statements themselves run in tests/integration/iceberg/test_partition_evolution.py."""

from __future__ import annotations

import pytest

from procurelake.lakehouse.partitioning import PartitionField, parse_fields
from procurelake.lakehouse.partitioning_job import parse_args


def test_parses_table_transform_column():
    fields = parse_fields("silver_invoices=months(invoice_date), silver_awards=months(awarded_at)")
    assert fields == [
        PartitionField("silver_invoices", "months", "invoice_date"),
        PartitionField("silver_awards", "months", "awarded_at"),
    ]
    assert fields[0].expression == "months(invoice_date)"


@pytest.mark.parametrize(
    "bad",
    [
        "silver_invoices=bucket(invoice_date)",  # transform not offered
        "silver_invoices=months(invoice_date); drop table x",
        "silver_invoices=months(Invoice-Date)",
        "silver_invoices",
    ],
)
def test_rejects_unsupported_or_unsafe_fields(bad):
    with pytest.raises(ValueError):
        parse_fields(bad)


def test_one_field_per_table_per_run():
    with pytest.raises(ValueError, match="one partition field"):
        parse_fields("silver_invoices=months(invoice_date),silver_invoices=days(invoice_date)")


def test_job_arguments_default_to_no_rewrite_and_ignore_glue_arguments():
    args = parse_args(
        ["--JOB_RUN_ID", "jr_1", "--JOB_NAME", "x", "--database", "procurelake_dev_bench_silver",
         "--fields", "silver_invoices=months(invoice_date)", "--report-bucket", "art"]
    )  # fmt: skip
    assert (args.rewrite, args.catalog, args.job_run_id) == ("false", "glue_catalog", "jr_1")


def test_describe_parser_reads_both_spark_forms():
    from procurelake.lakehouse.partitioning import parse_describe_partitioning

    head = [("invoice_id", "string", None), ("tenant_id", "string", None), ("", "", "")]
    identity = [*head, ("# Partition Information", "", ""), ("# col_name", "data_type", "comment"),
                ("tenant_id", "string", None), ("", "", ""), ("# Metadata Columns", "", "")]  # fmt: skip
    transform = [*head, ("# Partitioning", "", ""), ("Part 0", "tenant_id", ""),
                 ("Part 1", "months(invoice_date)", ""), ("", "", ""),
                 ("# Metadata Columns", "", "")]  # fmt: skip
    assert parse_describe_partitioning(identity) == ["tenant_id"]
    assert parse_describe_partitioning(transform) == ["tenant_id", "months(invoice_date)"]
    assert parse_describe_partitioning([*head, ("# Metadata Columns", "", "")]) == []
