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

## What this does not show

- **The failure path.** The SNS alert on a failed export was not triggered live.
- **A scheduled run.** The first scheduled run is at 06:00 on 2026-11-11.
- **Column-level controls.** None are applied; the filters include all columns.
- **The check after each run.** An alert when a filter, grant or opt-in is missing does not
  exist yet.
- **The deployed dbt runner image.** It still lacks the full-refresh guard; it has not been
  rebuilt or pushed.
