# ADR 0049: Catalog tightening: no `gold_staging` database; tenants see only their marts in Glue

- **Status:** Accepted (owner decision 2026-11-14; resolves open questions 36 and 38). Deployed to dev
  2026-11-14 (`gold_staging` deleted, after adding Glue user-defined functions to the deploy
  role's resources, bootstrap). `make test-aws`: 30 passed, including
  `test_catalog_shows_only_tenant_facing_marts`. A tenant's query on a table it is not granted
  now fails with `TABLE_NOT_FOUND` instead of access denied, because the table's metadata is
  hidden; `test_tables_not_granted_fail` accepts that only after confirming the table exists.
- **Date:** 2026-11-14
- **Implementation:**
  - Terraform, `gold_staging` removed: `infrastructure/modules/glue_catalog` (database and
    output), `infrastructure/live/dev/foundation/main.tf` (the data engineer's read and build
    grants)
  - Terraform, tenant catalog scope: `infrastructure/live/dev/security/main.tf`
    (`local.tenant_catalog_arns`, statement `ReadTenantFacingCatalog`)
  - Tests: `infrastructure/modules/glue_catalog/tests/glue_catalog.tftest.hcl`,
    `infrastructure/live/dev/foundation/tests/foundation.tftest.hcl`,
    `infrastructure/live/dev/security/tests/security.tftest.hcl` (offline);
    `tests/security/test_unauthorized_access.py::test_catalog_shows_only_tenant_facing_marts`
    (live, `make test-aws`)

## Context
Two catalog questions were left open after Gold and tenant isolation were built.

- **Open question 36.** ADR 0026 created `procurelake_<env>_gold_staging` for dbt staging and
  intermediate views. Staging became ephemeral (compiled into the Gold models as CTEs), so the
  database is empty. The data engineer still held read and table-write grants on it.
- **Open question 38.** The tenant roles' Glue statement allowed `GetTable`, `GetTables`,
  `GetDatabase`, `GetDatabases` and `GetPartitions` on `table/procurelake_dev_gold/*`. Facts and
  dimensions keep the account default `IAM_ALLOWED_PRINCIPALS: ALL` and the tenants are not
  opted in for them (ADR 0030), so IAM alone decided their metadata. A tenant could read the
  names and schemas of every Gold table, though not their rows.

On 2026-11-14 the owner decided: remove the database, and limit tenant catalog reads to the
tenant-facing marts.

## Decision
1. **Remove `procurelake_dev_gold_staging`.** The `aws_glue_catalog_database.gold_staging`
   resource and the `gold_staging_database_name` output are deleted, and the database is
   dropped from the data engineer's `glue_database_names` and `glue_writable_database_names`
   (dev/foundation). It was referenced nowhere else: not in another Terraform root, dbt, the
   Python package, scripts or tests (searched for `gold_staging`).
   - dbt is unaffected. `dbt_project.yml` has no `+schema`, the project has no
     `generate_schema_name` override, and the profile's only schema is
     `PROCURELAKE_GOLD_SCHEMA` (default `procurelake_dev_gold`). The compiled manifest puts all
     19 models in `procurelake_dev_gold`: 8 ephemeral staging models (not stored), 7 tables,
     4 incremental marts.
   - Its location `s3://<lake>/gold_staging/` was never written by dbt (ephemeral models create
     nothing), and it is outside the Lake Formation-registered `gold/` location.
2. **Tenant roles read Glue metadata only for the tenant-facing marts.** The statement
   `ReadTenantFacingCatalog` keeps the same five actions on three kinds of resource: the
   catalog, the Gold database, and one table ARN for each entry of `var.filtered_tables` (the
   four marts, the same list that drives the Lake Formation filters, grants and opt-ins). There
   is no `table/<gold>/*` wildcard and no Glue action in any other tenant statement.
   - Glue requires permission on a table and all its ancestors, so the catalog and database
     ARNs stay. Table ARNs are
     `arn:aws:glue:<region>:<account>:table/<database>/<table>`.
   - `GetTables` with named table ARNs returns only those tables. With no table ARN it returns an
     empty list, and without the database ARN it is denied (Glue identity-based policy examples,
     "Filter tables by GetTables permission").
   - Athena's documented minimum for `SHOW TABLES` is `GetDatabase` and `GetTables` on the
     catalog, the database and `table/<db>/*`. The wildcard there lists every table; with named
     ARNs, `SHOW TABLES` lists only the granted ones. No tenant workflow (queries, `UNLOAD`
     exports, the access check) lists tables; each one names a mart.
   - The marts are opted in for the tenants, so a tenant needs both IAM and Lake Formation
     permission on them (hybrid access mode). Their Lake Formation side (`DESCRIBE` on the
     database, `SELECT` through the row filter) is unchanged.
3. **The admin role is unchanged.** `procurelake-dev-analytics-admin` keeps `table/<gold>/*`
   reads. It is an internal role, and the owner's decision named the tenant roles.

## Alternatives considered
- **Accept schema visibility.** The marts' schemas are already visible to tenants; the facts'
  and dimensions' were not needed by any tenant workflow. Not chosen by the owner.
- **Lake Formation alone.** Opting the tenants in for every Gold table, with no grant on the
  facts and dimensions, would also hide them. It needs an opt-in per table, which dbt `table`
  rebuilds delete (ADR 0030), so every core rebuild would reset it. IAM scoping does not depend
  on table identity.
- **An explicit deny (`NotResource` the marts) on top of the allow list.** Nothing else grants
  the tenant roles Glue access, so the allow list already decides. A deny would be a second
  list to keep in step with `filtered_tables`, and how a deny interacts with the filtered
  `GetTables` result was not checked.
- **Keep `gold_staging` for future materialized intermediate models.** None exist; a database
  can be added again with the model that needs it.

## Consequences
- **Not deployed.** Offline only: `make tf-test` (mock provider), `make dbt-parse`,
  `make dbt-compile`. The new live test
  (`test_catalog_shows_only_tenant_facing_marts`: each tenant reads its four marts' schemas,
  is denied `GetTable` on `fct_contract_award`, `fct_invoice` and `dim_supplier` with
  `AccessDeniedException`, and `GetTables` lists exactly the four marts) runs with
  `make test-aws` after the apply. The existing isolation tests, the access check and the
  exports must also still pass then; that is the check that Athena needs nothing more than the
  named tables.
- **Expected plan, dev/foundation:** 1 to destroy (`module.catalog.aws_glue_catalog_database.gold_staging`),
  2 to change in place (`module.iam.aws_iam_role_policy.data_engineer_query[0]` and
  `data_engineer_build[0]`). Glue `DeleteDatabase` also deletes any tables in the database;
  run `aws glue get-tables --database-name procurelake_dev_gold_staging` before the apply and
  expect an empty list.
- **Expected plan, dev/security:** 2 to change in place (`aws_iam_role_policy.tenant` for each
  tenant). Nothing is created or destroyed; Lake Formation filters, grants and opt-ins are not
  touched.
- **Expected plan, dev/data_platform and dev/observability:** no changes from this ADR.
- **Deploy order.** One CI apply in the usual order (foundation, data_platform, security,
  observability) works. The database is defined and referenced only in dev/foundation, so no
  later root reads a removed output or grants on the removed database. The security change
  depends on nothing new.
- **New Gold tables are hidden from tenants by default.** A new tenant-facing mart must be added
  to `filtered_tables`, which adds its filters, grants, opt-ins and catalog read together.
- **A tenant using `SHOW TABLES` or the Athena console sees only the four marts.** Querying any
  other Gold table now fails at `glue:GetTable` instead of at the S3 deny (expected; not yet
  observed live). The S3 deny stays.
