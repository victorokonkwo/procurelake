# ADR 0026: dbt Gold on Athena (versions, materialization, deletes, runner)

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `dbt/`, `infrastructure/modules/{glue_catalog,iam}`, `scripts/aws/with_role.py`

## Context
Week 4 (work package B2) builds Gold with dbt on Athena engine v3 over the Silver Iceberg tables.
Four open questions blocked it. The project owner decided them on 2026-11-10: versions (2),
the late-arrival policy and materialization (8), delete semantics (9), and where dbt runs (16).

## Decision
1. **Versions:** `dbt-core==1.12.6` and `dbt-athena==1.11.1`, pinned in `uv.lock` in a separate
   `dbt` dependency group, so they are not in the Glue wheel or the default dev install. These
   were the latest releases on PyPI on 2026-11-10, and dbt-athena 1.11.1 declares
   `dbt-core>=1.8,<2`. There are no dbt packages; the one generic test (`unique_combination`)
   lives in `dbt/macros/`.
2. **Materialization:** staging is **ephemeral**; core and marts are **Iceberg tables rebuilt in
   full** each run (`table_type='iceberg'`, Parquet, `s3_data_naming='schema_table_unique'`).
   - At around 6,000 rows a full rebuild is exact and handles deletes, and no lookback window
     is needed yet.
   - `incremental_strategy='merge'`, with a documented late-arrival policy, comes in Week 6
     with CDC commit timestamps (open question 8 stays open for that).
   - dbt-athena requires a unique location for Iceberg `table` builds because it swaps tables
     by rename. A rebuild deletes the previous location; this was verified in dev.
3. **Why staging is ephemeral:** Athena stores views in Glue with Hive column types, and Hive
   cannot express `timestamp(6)`. The first build failed with
   `Unsupported Hive type: timestamp(6)`. Truncating to milliseconds would lose source
   precision, so staging is compiled into the Gold models as CTEs. Its keys are tested where they
   surface (dims and facts), and row counts are reconciled to Silver.
4. **Timestamps:** Silver instants (`timestamp(6) with time zone`) are stored in Gold as UTC
   `timestamp(6)` (macro `utc`).
5. **Deletes:** Gold is current state, and a key deleted at the source disappears (hard removal,
   mirroring Silver). History stays reachable through Iceberg snapshots.
6. **Lineage:** every dim and fact row carries `source_batch_id`, `source_file` and
   `source_line_hash` (the SHA-256 of the Bronze line) from Silver.
7. **Governance:**
   - Every core and mart model documents `meta.grain`, `meta.owner` and `meta.freshness` (a
     design target), and has tests. `tests/unit/dbt/test_gold_governance.py` enforces this,
     plus currency grouping and "award value is never labelled spend".
   - Data tests:
     - unique / not_null / relationships / accepted_values
     - unique grain on every mart
     - financial reconciliation to Silver, to the cent (awarded value, actual spend)
     - no Silver rows lost by fact joins
     - tenant consistency of fact references
     - supplier shares sum to 1
     - no award before publication
     - paid ≤ invoiced
     - tenants are synthetic
8. **Metric definitions:**
   - Awarded value counts **active** awards.
   - Actual spend is `paid_amount > 0` by month of `paid_at`. The source stores the cumulative
     amount paid and the latest payment time, not each payment.
   - Cycle time runs from `published_at` to `awarded_at`.
   - `dim_supplier` is one row per supplier registration (no cross-tenant matching; open question 35).
9. **Catalog and IAM:**
   - Terraform creates the Glue databases `procurelake_<env>_gold` (at `s3://<lake>/gold/`) and
     `procurelake_<env>_gold_staging`.
   - The data-engineer role gets Create/Update/DeleteTable and the table-version actions on those
     two databases only, and no CreateDatabase.
   - The dbt-athena docs give no IAM list, so the actions were taken from the Glue calls in the
     installed adapter's source. Lake Formation calls happen only when LF tags or grants are
     configured, and they are not.
10. **Runner:** `make dbt-build` runs dbt locally with temporary credentials for
    `procurelake-<env>-data-engineer` (`scripts/aws/with_role.py`). The container plus Step
    Functions runner is decided in Week 6 (open question 16, narrowed). CI runs `dbt parse` offline.

## Consequences
- **Evidence:** `docs/evidence/sprint1-week4-gold-live.md`.
- `procurelake_dev_gold_staging` is empty now that staging is ephemeral (open question 36).
- dbt-athena's Iceberg `table` rebuild drops and re-creates the target, so a consumer querying
  during a build can briefly miss the table (dbt-athena docs). This is acceptable for dev.
  Week 6 incremental merges avoid it.
