"""Catalog governance for Silver (ADR 0042): owner, classification, grain and description in Glue.

The source contracts (``contracts/source``) already declare each table's owner, data
classification, grain and description. This module writes them to the Silver Iceberg tables'
Glue entries, so anyone browsing the catalog (Athena, Lake Formation, the Glue console) sees who
owns a table and how it is classified. Gold gets the same metadata from dbt-athena's
``persist_docs`` (``dbt/dbt_project.yml``).

Only the table ``Description`` and ``Parameters`` are written, never the Glue ``Owner`` field or
column comments: an Iceberg commit rewrites the table's columns from the Iceberg schema, and the
contract parameters are the stable place.

Writes are safe against concurrent Iceberg commits. Glue ``UpdateTable`` replaces the whole table
definition, including ``metadata_location``; writing a stale copy would roll the table back to
an older snapshot. Each update therefore passes the ``VersionId`` it read: if a writer committed
in between, Glue rejects the update (``ConcurrentModificationException``), and the table is read
again and the update retried.

Athena maintenance (``OPTIMIZE``, ``VACUUM``, ``ALTER TABLE SET TBLPROPERTIES``) commits a Glue
table entry that keeps only Iceberg's own parameters, dropping every other one (seen on dev
2026-11-14: the Gold marts lost dbt's owner, grain, freshness and classification). The
maintenance job therefore saves each table's parameters first (``save_metadata``) and puts back
the ones the commit dropped (``restore_metadata``). Silver is then re-synced from its contracts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from procurelake.contracts.reference import ReferenceContract
from procurelake.lakehouse.silver.spec import SilverTableSpec

logger = logging.getLogger(__name__)

# Glue caps a table description at 2,048 characters.
_MAX_DESCRIPTION = 2048


@dataclass(frozen=True)
class TableMetadata:
    description: str
    parameters: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult:
    table: str
    status: str  # updated | unchanged | missing | failed
    changed: tuple[str, ...] = ()
    error: str | None = None


def silver_metadata(spec: SilverTableSpec) -> TableMetadata:
    """The catalog metadata a Silver table carries, from its source contract."""
    c = spec.contract
    description = " ".join(c.description.split())[:_MAX_DESCRIPTION]
    return TableMetadata(
        description=description,
        parameters={
            "comment": description,
            "owner": c.owner,
            "data_classification": c.data_classification,
            "grain": " ".join(c.grain.split()),
            "contract_name": c.name,
            "contract_version": str(c.contract_version),
            "source_system": c.source_system,
            "source_table": f"{c.physical.schema_name}.{c.physical.table}",
            "tenant_scoped": str(c.tenant_scoped).lower(),
        },
    )


def reference_metadata(c: ReferenceContract) -> TableMetadata:
    """The catalog metadata a reference Silver table carries (ADR 0044), from its contract."""
    description = " ".join(c.description.split())[:_MAX_DESCRIPTION]
    return TableMetadata(
        description=description,
        parameters={
            "comment": description,
            "owner": c.owner,
            "data_classification": c.data_classification,
            "grain": " ".join(c.grain.split()),
            "contract_name": c.name,
            "contract_version": str(c.contract_version),
            "source_system": c.source_system,
            "source_table": f"{c.source_system}/{c.dataset}",
            "tenant_scoped": "false",
            "currency_code": c.currency_code,
        },
    )


def expected_metadata(item: SilverTableSpec | ReferenceContract) -> TableMetadata:
    return reference_metadata(item) if isinstance(item, ReferenceContract) else silver_metadata(item)


def drift(table: dict[str, Any], expected: TableMetadata) -> tuple[str, ...]:
    """Names of the expected fields the Glue table does not carry as declared."""
    params = table.get("Parameters") or {}
    out = [k for k, v in expected.parameters.items() if params.get(k) != v]
    if (table.get("Description") or "") != expected.description:
        out.insert(0, "Description")
    return tuple(out)


def _table_input(glue, table: dict[str, Any]) -> dict[str, Any]:
    # UpdateTable takes a TableInput, which accepts only some of GetTable's fields.
    members = glue.meta.service_model.shape_for("TableInput").members
    return {k: v for k, v in table.items() if k in members}


def apply_metadata(
    glue,
    database: str,
    name: str,
    expected: TableMetadata,
    attempts: int = 5,
    fill_only: bool = False,
) -> SyncResult:
    """Write ``expected`` to one Glue table, without touching anything else it holds.

    With ``fill_only``, only parameters the table lacks are written (and the description only
    if it has none): a value set meanwhile, say by a dbt run, is never overwritten.
    """
    fq = f"{database}.{name}"
    wanted = expected
    for attempt in range(1, attempts + 1):
        try:
            table = glue.get_table(DatabaseName=database, Name=name)["Table"]
        except glue.exceptions.EntityNotFoundException:
            return SyncResult(fq, "missing")
        if fill_only:
            have = table.get("Parameters") or {}
            expected = TableMetadata(
                description=table.get("Description") or wanted.description,
                parameters={k: v for k, v in wanted.parameters.items() if k not in have},
            )
        changed = drift(table, expected)
        if not changed:
            return SyncResult(fq, "unchanged")
        table_input = _table_input(glue, table)
        table_input["Description"] = expected.description
        table_input["Parameters"] = {**(table.get("Parameters") or {}), **expected.parameters}
        try:
            glue.update_table(
                DatabaseName=database,
                TableInput=table_input,
                VersionId=table["VersionId"],
                SkipArchive=True,
            )
        except glue.exceptions.ConcurrentModificationException:
            logger.info("%s: a writer committed meanwhile (attempt %d); reading again", fq, attempt)
            continue
        return SyncResult(fq, "updated", changed)
    return SyncResult(fq, "failed", error=f"still concurrently modified after {attempts} attempts")


def sync_silver(
    glue, database: str, specs: Iterable[SilverTableSpec | ReferenceContract]
) -> list[SyncResult]:
    """Apply every contract's metadata to its Silver table. A table not built yet is ``missing``."""
    results = []
    for spec in specs:
        try:
            results.append(apply_metadata(glue, database, spec.silver_table, expected_metadata(spec)))
        except Exception as exc:
            results.append(SyncResult(f"{database}.{spec.silver_table}", "failed", error=repr(exc)))
    return results


# Parameters Iceberg itself maintains in the Glue entry; never saved or restored.
ICEBERG_PARAMETERS = frozenset({"metadata_location", "previous_metadata_location", "table_type"})


def save_metadata(glue, database: str, names: Iterable[str]) -> dict[str, TableMetadata]:
    """Each existing table's description and non-Iceberg parameters, before maintenance."""
    saved = {}
    for name in names:
        try:
            table = glue.get_table(DatabaseName=database, Name=name)["Table"]
        except glue.exceptions.EntityNotFoundException:
            continue
        params = {k: v for k, v in (table.get("Parameters") or {}).items() if k not in ICEBERG_PARAMETERS}
        saved[name] = TableMetadata(description=table.get("Description") or "", parameters=params)
    return saved


def restore_metadata(glue, database: str, saved: dict[str, TableMetadata]) -> list[SyncResult]:
    """Put back the saved parameters a commit dropped; values present now are kept."""
    results = []
    for name, meta in saved.items():
        try:
            results.append(apply_metadata(glue, database, name, meta, fill_only=True))
        except Exception as exc:
            results.append(SyncResult(f"{database}.{name}", "failed", error=repr(exc)))
    return results


def check_silver(
    glue, database: str, specs: Iterable[SilverTableSpec | ReferenceContract]
) -> list[SyncResult]:
    """Read-only: which Silver tables lack their contract's metadata (status ``drift``)."""
    results = []
    for spec in specs:
        fq = f"{database}.{spec.silver_table}"
        try:
            table = glue.get_table(DatabaseName=database, Name=spec.silver_table)["Table"]
        except glue.exceptions.EntityNotFoundException:
            results.append(SyncResult(fq, "missing"))
            continue
        changed = drift(table, expected_metadata(spec))
        results.append(SyncResult(fq, "drift" if changed else "unchanged", changed))
    return results


GOLD_META_KEYS = ("grain", "owner", "freshness", "data_classification")


def gold_expected(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Gold models' governance meta from a dbt manifest, as dbt-athena writes them to Glue."""
    out = {}
    for node in manifest["nodes"].values():
        if node["resource_type"] != "model" or node["config"].get("materialized") == "ephemeral":
            continue
        meta = node["config"].get("meta") or node.get("meta") or {}
        out[node["name"]] = {k: str(meta[k]) for k in GOLD_META_KEYS if k in meta}
    return out


def check_gold(glue, database: str, expected: dict[str, dict[str, str]]) -> list[SyncResult]:
    """Read-only: which Gold tables lack their dbt meta or description in Glue."""
    results = []
    for name, params in sorted(expected.items()):
        fq = f"{database}.{name}"
        try:
            table = glue.get_table(DatabaseName=database, Name=name)["Table"]
        except glue.exceptions.EntityNotFoundException:
            results.append(SyncResult(fq, "missing"))
            continue
        have = table.get("Parameters") or {}
        changed = [k for k, v in params.items() if have.get(k) != v]
        if not table.get("Description"):
            changed.insert(0, "Description")
        results.append(SyncResult(fq, "drift" if changed else "unchanged", tuple(changed)))
    return results
