"""Shared CDC flow for the acceptance tests: full load, then real committed changes."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
from e2e_support import SPECS

from procurelake.data_generator.workload import run_workload

# The tables the CDC workload changes (procurelake.data_generator.workload).
CDC_TABLES = ("invoices", "awards", "purchase_orders", "suppliers")


def full_load(dms, source: str) -> datetime:
    """DMS full load of every CDC table, before any change is committed."""
    loaded_at = datetime.now(UTC)
    for table in CDC_TABLES:
        dms.full_load(source, SPECS[table], loaded_at)
    return loaded_at


def commit_workload(source: str, seed: int, operations: int):
    """Commit the CDC workload in PostgreSQL; returns its changes (real commit times)."""
    with psycopg.connect(source) as conn:
        return run_workload(conn, seed=seed, operations=operations)
