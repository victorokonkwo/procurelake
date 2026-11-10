# ADR 0030: Tenant isolation with Lake Formation (hybrid mode) on Gold; marts merge, never drop

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:**
  - Terraform: `infrastructure/live/dev/security` (registration, tenant roles and workgroups, data cells filters, grants, opt-ins)
  - dbt: `dbt/dbt_project.yml` (marts: incremental `merge`), `dbt/macros/delete_vanished_keys.sql`
  - Runner guard: `jobs/containers/dbt_runner/entrypoint.py` (no full refresh)
  - Exports: `orchestration/step_functions/tenant_exports.asl.json`; products bucket policy in `infrastructure/modules/s3_lakehouse`
  - Live tests: `tests/integration/aws/test_tenant_isolation.py`
- **Evidence:** `docs/evidence/sprint1-week7-lake-formation-experiment.md`, `docs/evidence/sprint1-week7-tenant-products-live.md`

## Context
Work package D needs two synthetic tenant identities (`tenant_alder`, `tenant_birch`; fictional,
not SOVRA customers) that see only their own Gold rows. The owner chose on 2026-11-10: Lake
Formation, on Gold only, Athena as the query surface (dashboard deferred), daily Parquet exports.

Two risks were known before building:
- Athena's documentation says it "does not currently support DDL operations on Iceberg tables
  whose location is registered with Lake Formation". dbt builds Gold with CTAS and rename.
- Lake Formation permissions belong to catalog objects. What happens to them when dbt replaces
  a table was not documented anywhere we found.

So the owner approved an experiment first.

## Decision
1. **Register `s3://<lake>/gold` in hybrid access mode.** Only opted-in principals (the tenant
   roles, per table) are governed by Lake Formation. The pipeline (dbt, Glue) keeps IAM access,
   and the account defaults stay `IAM_ALLOWED_PRINCIPALS: ALL`. A custom registration role is
   used because the bucket is encrypted with a customer-managed KMS key.
2. **Tenant roles:** one row filter per tenant per table (`tenant_id = '<tenant>'`, all
   columns), `SELECT` through that filter with no grant option, `DESCRIBE` on the Gold database,
   their own Athena workgroup and results prefix, and an **explicit deny on `s3:*` for the lake
   and raw buckets**. Lake Formation does not govern direct S3 reads, so that path is closed
   separately (AGENTS.md §2.7).
3. **Tenant-facing marts are never dropped.** Measured live: a dbt `table` rebuild of an Iceberg
   model renames a new table into place. Lake Formation then deletes the old table's data cells
   filters, filter grants and opt-ins. Tenants were locked out (fail-closed, no rows leaked) until
   someone re-granted. Owner decision, 2026-11-10: the marts become **incremental Iceberg
   `merge`** models.
   - Each run still recomputes the whole mart. It is then MERGEd into the existing table, so the
     table, and with it its Lake Formation objects, is kept.
   - The post-hook `delete_vanished_keys` deletes target keys absent from the fresh result. It
     runs after the MERGE and before dbt drops `<model>__dbt_tmp`. This keeps each run exact,
     including groups that disappear (ADR 0026's delete-safety is kept).
   - Mart keys are tested `not_null`, because the MERGE matches keys with `=`.
   - `on_schema_change: fail`: a schema change stops the run instead of changing the table.
4. **A full refresh is an operator action, never scheduled.** The dbt runner refuses
   `--full-refresh`, `-f` and `DBT_FULL_REFRESH`. After a deliberate full refresh, the operator
   re-applies `dev/security` (`-parallelism=1`) to recreate the filters, grants and opt-ins.
5. **The four tenant-facing marts** are all filtered per tenant: `mart_awarded_value`,
   `mart_actual_spend`, `mart_procurement_cycle_time` and `mart_supplier_concentration`. Facts
   and dimensions are not granted.
6. **Daily Parquet exports run as the tenant.** The owner decided on a daily schedule. A Step
   Functions machine runs `UNLOAD` and writes the manifest with each tenant role's credentials
   (Task `Credentials`).
   - The Lake Formation filter therefore applies to the export: measured live, the export row
     counts equal the tenant's query counts.
   - The machine's own role can only assume the tenant roles and publish to SNS.
   - Exports land in `products/<tenant>/<mart>/export_date=/run=/`.
   - The products bucket policy denies each tenant role everything outside its own prefix, and
     denies tenant deletes. The roles are matched by name, so foundation does not depend on
     `dev/security`.
7. **Admin cross-tenant product.** `procurelake-dev-analytics-admin` has `SELECT` on the four
   marts with no filter, through Lake Formation, in its own workgroup, with the same S3 deny as
   the tenants. The dashboard was deferred by the owner; Athena is the query surface.

## Consequences
- In hybrid mode the data engineer's CTAS, MERGE and DELETE on the registered location work
  through IAM (live: `dbt build` PASS=75 on the registered location). The Athena DDL limitation
  applies to principals governed by Lake Formation, and the pipeline is not one.
- A tenant denied a non-opted-in table (`fct_contract_award`) is stopped by the IAM S3 deny,
  not by Lake Formation. Both layers are needed.
- Tenants can read Glue metadata (names and schemas) for every Gold table, not their rows. See
  open question 38.
- Provider and service behaviour, worked around in code:
  - `excluded_column_names = []` is needed in the filter's `column_wildcard`.
  - The database `DESCRIBE` grant and the admin's table `SELECT` grants ignore `permissions`
    drift, because ListPermissions filtered by principal reports `ALL`.
  - The Step Functions `aws-sdk:s3:putObject` integration serializes a JSON `Body` itself. Do
    not wrap the manifest in `States.JsonToString`.
  - Opt-ins must be applied with `-parallelism=1`: parallel creates hit
    `ConcurrentModificationException`.
- Each merge rewrites the mart's rows in new Iceberg snapshots, so the marts' storage and
  snapshot count grow until table maintenance (`OPTIMIZE`/`VACUUM`, Week 8) runs.
- Every `dbt_transformation` run ends with `tenant_access_check`, which queries every mart as
  every tenant and fails with an SNS alert if access was lost or a foreign row is visible. A
  dropped grant therefore cannot go unnoticed for longer than one run.
- Still open: column-level controls, and live runs of the failure paths (export failure, check
  failure).
