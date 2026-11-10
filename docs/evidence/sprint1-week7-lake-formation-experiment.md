# Evidence: Week 7, Lake Formation tenant isolation experiment on Gold (live, dev)

**Scope:** one table, `procurelake_dev_gold.mart_awarded_value`, governed by Lake Formation in
hybrid access mode for the two synthetic tenant roles (`procurelake-dev-tenant-alder`,
`procurelake-dev-tenant-birch`). Fictional tenants, synthetic data. Run on 2026-11-10 from
`dev/security` (ADR 0030). The harness assumed each role and ran Athena queries in its own
workgroup, plus direct S3 calls.

**Result:** all five checks pass with the final design (marts merged, never dropped). The
first design, full table rebuilds, failed check 5.

## Checks

| # | Check | Result |
|---|---|---|
| 1 | Alder queries `mart_awarded_value` | Only `tenant_alder`: 370 rows. The data engineer sees 370 Alder + 220 Birch |
| 1 | Birch queries `mart_awarded_value` | Only `tenant_birch`: 220 rows |
| 2 | Either tenant queries `fct_contract_award` (not granted) | FAILED: `PERMISSION_DENIED … s3:GetObject … explicit deny in an identity-based policy` |
| 3 | Either tenant lists `s3://<lake>/gold/` and the raw bucket | `AccessDenied` (both) |
| 4 | `dbt build` as the data engineer on the registered location | PASS (59/59 with `table` marts; 75/75 with merge marts) |
| 5 | Tenant access after a rebuild, `table` marts | **FAILED**: see below |
| 5 | Tenant access after a rebuild, merge marts | **PASS**: checks 1–3 identical after `dbt build`; `terraform plan` on `dev/security`: no changes |

Athena query IDs from the first passing run: Alder `0dee6dea-d1bb-4aa8-9f49-d7361bee0cfa`,
Birch `485a649a-5759-4e07-aec2-4c21abfa697e`.

## Check 5 with `table` marts: grants lost on rebuild

dbt's Iceberg `table` materialization builds a new table and renames it into place. After one
`dbt build`:
- `mart_awarded_value` had a new create time (15:21:15 local) and a new S3 location.
- `list-data-cells-filter` and `list-lake-formation-opt-ins` returned nothing for the table.
- `terraform plan` wanted to recreate both filters, both filter grants and both opt-ins.
- Both tenants' queries failed with `PERMISSION_DENIED` on the new table's metadata file.

So the failure is closed: no rows leaked, and access was lost until someone re-granted. With
dbt scheduled every 5 minutes, that would lock tenants out after every run.

## Check 5 with merge marts

After switching the four marts to incremental `merge` (ADR 0030):

| Step | Result |
|---|---|
| First merge build | PASS=75. All four marts kept create time and location (15:21:15/16); no `__dbt_tmp` table left behind |
| Vanished-key delete | Inserted 1 row with an invented key (`agency_id = 'vanished-key-test'`): 591 rows. After `dbt build`: 590 rows, 0 invented rows, same create time |
| Re-grant | `terraform apply` of `dev/security`: 6 added (the objects lost above) |
| Checks 1–3, then `dbt build` (PASS=75), then checks 1–3 again | Identical results both times; `mart_awarded_value` create time unchanged |
| `terraform plan` on `dev/security` after the rebuild | `No changes` |

## Issues found during the experiment

- **Filter apply error.** The AWS provider reported "inconsistent result after apply" on
  `column_wildcard.excluded_column_names` (planned null, returned empty). Fixed with
  `excluded_column_names = []`.
- **Repeated grant replacement.** The database `DESCRIBE` grant was replaced on every plan.
  `ListPermissions` filtered to a tenant role returns `["ALL","DESCRIBE"]`, while the unfiltered
  listing shows that role with `DESCRIBE` only. The `permissions` drift is ignored.
- **Opt-in concurrency.** Two opt-ins created in parallel failed with
  `ConcurrentModificationException`. Apply this root with `-parallelism=1`.
- **Athena results bucket check.** Tenant queries first failed with "Unable to verify/create
  output bucket". `s3:GetBucketLocation` had been placed under an `s3:prefix` condition, and that
  request never carries a prefix. It now has its own statement. Listing the bucket is still
  limited to the tenant's own results prefix.

## What this does not show

- Only `mart_awarded_value` is filtered. The other marts, the admin cross-tenant product and the
  exports are the rest of Week 7.
- Column-level controls: the filters include all columns.
- The check after each run, which alerts if any Lake Formation object is missing, does not exist yet.
- The scheduled (Step Functions) dbt runs were not part of this experiment. The runner image
  that refuses full refresh has not been rebuilt or pushed yet.
