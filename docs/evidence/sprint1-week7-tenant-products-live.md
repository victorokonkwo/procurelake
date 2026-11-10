# Evidence: Week 7, tenant data products and isolation (live, dev)

**Scope:** work package D on 2026-11-10 (ADR 0030). The tenants are `tenant_alder` and
`tenant_birch`, both fictional and synthetic, not SOVRA customers. Covered:
- the four tenant-facing Gold marts, governed by Lake Formation row filters;
- daily Parquet exports to the products bucket;
- the internal cross-tenant admin product.

`make test-aws` checks all of it as live integration tests:
`tests/integration/aws/test_tenant_isolation.py`.

**Result: the D3 gate is met.** `make test-aws` ran 27 tests and all passed (63.6 s): 18 in
`test_tenant_isolation.py` and the 9 earlier Bronze and Gold tests. Raw record:
`build/evidence/aws_integration.json` (not committed).

## Queries as each tenant (Athena, own workgroup)

| Mart | Alder sees | Birch sees | Data engineer (all rows) |
|---|---|---|---|
| `mart_awarded_value` | only Alder, 370 | only Birch, 220 | 370 + 220 |
| `mart_actual_spend` | only Alder, 1,173 | only Birch, 587 | 1,173 + 587 |
| `mart_procurement_cycle_time` | only Alder, 180 | only Birch, 128 | 180 + 128 |
| `mart_supplier_concentration` | only Alder, 62 | only Birch, 61 | 62 + 61 |

## Access that must fail

| Attempt (each tenant) | Result |
|---|---|
| Query `fct_contract_award`, `fct_invoice`, `dim_supplier` (not granted) | `PERMISSION_DENIED` |
| List the lake bucket (root and `gold/`), list the raw bucket, get a lake object | `AccessDenied` (all 4, both tenants) |
| Start a query in the other tenant's workgroup | `AccessDeniedException` |
| List, get or put in the other tenant's products prefix | `AccessDenied` |
| Delete one of its own exports | `AccessDenied` (bucket policy: tenants never delete) |

## Daily exports (`procurelake-dev-tenant-exports`)

- **Schedule:** EventBridge Scheduler, `cron(0 6 * * ? *)` America/Toronto.
- **What one run does:** for each tenant and mart, Athena `UNLOAD … WITH (format = 'PARQUET')`
  runs with the tenant role's credentials (Step Functions Task `Credentials`). Then
  `_manifest.json` is written, also as the tenant role. The state machine's own role can only
  assume the tenant roles and publish to SNS; it has no S3 access.
- **Layout:** `s3://<products>/<tenant>/<mart>/export_date=<date>/run=<execution>/`.

The run checked is `manual-20261110T211943Z`: SUCCEEDED, 8 exports. Each tenant read its own
export back:

| Mart | Alder export rows | Birch export rows |
|---|---|---|
| `mart_awarded_value` | 370, all `tenant_alder` | 220, all `tenant_birch` |
| `mart_actual_spend` | 1,173 | 587 |
| `mart_procurement_cycle_time` | 180 | 128 |
| `mart_supplier_concentration` | 62 | 61 |

The export row counts equal the data engineer's per-tenant counts. So the Lake Formation filter
applies to `UNLOAD`, and nothing is lost or added.

## Admin cross-tenant product (`procurelake-dev-analytics-admin`)

The role has `SELECT` on the four marts with no filter, its own workgroup, and the same S3 deny as
the tenants. In each mart it sees both tenants, with counts equal to the data engineer's.

## Issues found and fixed during the build

- **Double-encoded manifests.** The first export run (`manual-20261110T211156Z`) wrote each
  `_manifest.json` as a JSON *string*. The Step Functions `aws-sdk:s3:putObject` integration
  serializes a JSON `Body` itself, and the definition had serialized it first
  (`States.JsonToString`). It now passes the object. Those 8 manifests remain in the bucket
  (versioned; tenants cannot delete). The tests pick the newest run by key.
- **Admin grant drift.** Like the database grant, the admin's table grants showed a replacement
  on every plan: the principal-filtered ListPermissions adds `ALL`. The unfiltered listing shows
  `SELECT` only. The `permissions` drift is ignored.

## Access check after each dbt run (`procurelake-dev-tenant-access-check`)

The last step of `dbt_transformation`. It queries each of the four marts as each tenant role:
`count_if(tenant_id <> '<tenant>')` must be 0. A failed query means access was lost, as after a
full refresh. Either outcome fails the run and publishes to the alerts topic.

| Run | Result |
|---|---|
| Check on its own (`manual-20261110T2200…`) | SUCCEEDED, 8 of 8 checks |
| `dbt_transformation` `manual-20261110T221043Z`, new image `sha256:5dd2a00d…` | SUCCEEDED in 4 min 0 s: container exit 0, dbt 10 models + 65 tests pass (57.9 s), then the nested access check SUCCEEDED |

The runner image refuses a full refresh, checked locally on the deployed image (local image ID =
deployed digest). `--full-refresh`, `-f` and `DBT_FULL_REFRESH=true` each fail with
`ValueError … not allowed in the scheduled runner`, and the container exits 1 before dbt starts.

The first `dbt_transformation` update was rejected: "not authorized to create managed-rule". The
role's new `events:PutRule` statement had been written moments before and had not reached Step
Functions yet. A plain re-apply succeeded.

The export role could not publish to the KMS-encrypted alerts topic, so a failed export would
have sent no alert. The role now has the data key, as `cdc_processing` already did.

## Failure drill (`make tenant-access-drill`)

`scripts/aws/tenant_access_drill.py` breaks isolation on purpose for about a minute, then
restores it. Raw record: `build/evidence/tenant_access_drill_20261110T221655Z.json` (not committed).

| Step (UTC) | Result |
|---|---|
| 22:16:56 | Alder granted an unfiltered `SELECT` on `mart_supplier_concentration`, outside Terraform, so Alder also sees Birch's rows |
| Check `drill-broken-20261110T221655Z` | **FAILED**: `ForeignRowsVisible`, then `TenantAccessCheckFailed` |
| Alerts topic | 1 message published in the window. The KMS-encrypted topic accepts the check's alert |
| 22:18:17 | Grant revoked (81 s after it was given; the revoke is in a `finally` block) |
| Check `drill-restored-20261110T221655Z` | **SUCCEEDED** |
| Afterwards | No table-level grant for either tenant on that mart; `terraform plan` on `dev/security`: no changes |

## What this does not show

- **The failure path.** The SNS alert on a failed export was not triggered live.
- **A scheduled run.** The first scheduled run is at 06:00 on 2026-11-11.
- **Column-level controls.** None are applied; the filters include all columns.
