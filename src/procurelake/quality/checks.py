"""Daily data-quality checks: Bronze-to-Silver reconciliation and Silver integrity (ADR 0040).

Run by the ``data_quality`` state machine after ``dbt test``. The checks:

1. **Latest Silver run, per source** (``pipeline_runs/silver/<source>/…/summary.json``, the
   newest succeeded or blocked run):
   - every table's ``input_rows`` equals the record count of the manifest it applied;
   - ``input_rows == valid_rows + rejected_rows``;
   - when rows were rejected, the quarantine prefix holds exactly ``rejected_rows`` JSON lines;
   - no table is ``blocked`` (reject ratio above its limit).
2. **Current Silver tables** (Athena): no primary key appears twice, and no row has a null
   ``tenant_id`` (for tables that have one).

Every check is recorded, passed or not, in a report written to the artifacts bucket. The
process exits non-zero when any check fails, so the state machine alerts.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from procurelake.common.athena import run_query
from procurelake.ingestion.landing import LandingStore
from procurelake.ingestion.manifest import IngestionManifest, load_manifests
from procurelake.lakehouse.silver.spec import SilverTableSpec

SILVER_RUNS_ROOT = "pipeline_runs/silver"
REPORTS_ROOT = "pipeline_runs/quality"


@dataclass(frozen=True)
class Check:
    name: str
    subject: str
    passed: bool
    detail: str


def latest_silver_summary(artifacts: LandingStore, source_system: str) -> tuple[str, dict] | None:
    """The newest Silver run summary for ``source_system`` by ``completed_at``."""
    prefix = f"{SILVER_RUNS_ROOT}/{source_system}/"
    summaries = []
    for key in artifacts.list_keys(prefix):
        if key.endswith("/summary.json"):
            body = json.loads(artifacts.get(key))
            summaries.append((body.get("completed_at", ""), key, body))
    if not summaries:
        return None
    _, key, body = max(summaries)
    return key, body


def summary_checks(
    summary: Mapping[str, Any],
    manifests: Mapping[tuple[str, str], IngestionManifest],
    quarantine_lines: Callable[[str], int],
    source_system: str,
) -> list[Check]:
    """Reconcile one Silver run summary. ``manifests`` maps (dataset, batch_id) to its manifest.

    ``source_system`` is passed in: summaries written before 2026-11-11 do not record it.
    """
    checks: list[Check] = []
    for t in summary.get("tables", []):
        table, batch = t["table"], t["batch_id"]
        subject = f"{source_system}/{table} batch {batch}"
        manifest = manifests.get((table, batch))
        recorded = (
            manifest.reconciliation.landed_record_count if manifest and manifest.reconciliation else None
        )
        checks.append(
            Check("manifest_count_equals_silver_input", subject, recorded == t["input_rows"],
                  f"manifest {recorded}, Silver read {t['input_rows']}")
        )  # fmt: skip
        total = t["valid_rows"] + t["rejected_rows"]
        split = f"{t['valid_rows']} valid + {t['rejected_rows']} rejected = {total}"
        checks.append(
            Check("input_equals_valid_plus_rejected", subject, total == t["input_rows"],
                  f"{split}, input {t['input_rows']}")
        )  # fmt: skip
        if t["rejected_rows"]:
            uri = t.get("quarantine_uri")
            found = quarantine_lines(uri) if uri else 0
            checks.append(
                Check("quarantine_holds_every_rejected_row", subject, found == t["rejected_rows"],
                      f"{found} lines under {uri}, {t['rejected_rows']} rejected")
            )  # fmt: skip
        checks.append(
            Check("table_not_blocked", subject, t["status"] != "blocked",
                  f"status {t['status']}, reject ratio {t.get('reject_ratio', 0):.4f}")
        )  # fmt: skip
    return checks


def _identifier(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier: {name}")
    return name


def integrity_sql(database: str, spec: SilverTableSpec) -> str:
    """One row: total rows, keys seen more than once, rows with a null tenant_id (or -1)."""
    db, table = _identifier(database), _identifier(spec.silver_table)
    keys = ", ".join(_identifier(k) for k in spec.primary_key)
    has_tenant = any(name == "tenant_id" for name, _ in spec.columns)
    nulls = "count_if(tenant_id is null)" if has_tenant else "-1"
    # Identifiers are validated above.
    return (
        f"select (select count(*) from {db}.{table}) as total_rows, "  # noqa: S608
        f"(select count(*) from (select {keys} from {db}.{table} group by {keys} having count(*) > 1)) "
        f"as duplicate_keys, (select {nulls} from {db}.{table}) as null_tenant_rows"
    )


def integrity_checks(
    athena: Any, workgroup: str, database: str, specs: Iterable[SilverTableSpec], sleep=None
) -> list[Check]:
    checks: list[Check] = []
    for spec in specs:
        kwargs = {"sleep": sleep} if sleep else {}
        r = run_query(athena, integrity_sql(database, spec), workgroup, database, **kwargs)
        total, dupes, nulls = (int(v) for v in r.rows[0])
        subject = f"{database}.{spec.silver_table} ({total} rows)"
        checks.append(Check("no_duplicate_primary_keys", subject, dupes == 0,
                            f"{dupes} keys on {spec.primary_key} appear more than once"))  # fmt: skip
        if nulls >= 0:
            checks.append(Check("no_null_tenant_id", subject, nulls == 0, f"{nulls} rows without tenant_id"))
    return checks


def manifests_by_batch(
    artifacts: LandingStore, source_system: str, datasets: Iterable[str]
) -> dict[tuple[str, str], IngestionManifest]:
    return {
        (m.dataset, m.batch_id): m
        for dataset in datasets
        for m in load_manifests(artifacts, source_system, dataset)
    }


def report(checks: list[Check], **context: Any) -> dict:
    failed = [c for c in checks if not c.passed]
    return {
        "report_format_version": "1",
        **context,
        "passed": not failed,
        "checks_total": len(checks),
        "checks_failed": len(failed),
        "failed": [asdict(c) for c in failed],
        "checks": [asdict(c) for c in checks],
    }
