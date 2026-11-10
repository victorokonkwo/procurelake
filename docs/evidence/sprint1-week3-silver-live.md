# Evidence: Week 3, Silver Iceberg tables live in dev

**Gate:** "Query-ready Silver Iceberg tables."
**Result:** met for the seven operational (PostgreSQL) tables. USAspending is not yet in Silver
(open question 34).

| | |
|---|---|
| Date | 2026-11-10 |
| Environment | dev, `ca-central-1`; Glue job `procurelake-dev-bronze-to-silver` (Glue 5.1, G.1X × 2) |
| Input | Snapshot batch `20261110T093202Z-983c92e5` (ADR 0023): 5,702 rows across 7 tables |
| Code | Wheel SHA-256 `2b6cdbf0…` for the successful run (`a4cb502d…` for the first run). The commit adds this file |

## Runs

| Run | Wheel | Glue state | Execution time | DPU-seconds | Tables |
|---|---|---|---|---|---|
| `jr_78d5ebaf…` | `a4cb502d…` | FAILED (`SystemExit: 0`) | 188 s | 377 | all 7 **applied**, 0 rejected, summary `succeeded` |
| `jr_bafb29a7…` | `2b6cdbf0…` | **SUCCEEDED** | 163 s | 326 | all 7 **skipped** (batch already applied) |

- **The first run did its work.** Every table was applied, and the summary was written. Glue
  then marked it FAILED because the entrypoint called `sys.exit(0)`. The fix is in ADR 0025,
  item 1.
- **The second run is the same batch run again.** It changed nothing: `silver_awards` still has
  one Iceberg snapshot (`2244303972616039481`, `append`). This is idempotent re-apply, confirmed
  on AWS.
- **Cost of the two runs:** 703 DPU-seconds at $0.44/DPU-hour (AWS Pricing API, ca-central-1),
  about $0.09. This was not checked against the bill.

Run summaries:
`s3://<artifacts>/pipeline_runs/silver/postgres/batch_id=20261110T093202Z-983c92e5/run_id=<run>/summary.json`.

## Athena checks

These queries ran as `procurelake-dev-data-engineer` in workgroup `procurelake-dev-engineering`
against `procurelake_dev_silver`. Query execution IDs were not recorded for this run.

| Table | Rows in Silver | Rows in snapshot manifest | Distinct `_batch_id` |
|---|---|---|---|
| silver_tenants | 2 | 2 | 1 |
| silver_agencies | 13 | 13 | 1 |
| silver_suppliers | 105 | 105 | 1 |
| silver_solicitations | 650 | 650 | 1 |
| silver_awards | 628 | 628 | 1 |
| silver_purchase_orders | 1,481 | 1,481 | 1 |
| silver_invoices | 2,823 | 2,823 | 1 |
| **total** | **5,702** | **5,702** | |

Awarded value by tenant and currency. Awarded value is not spend; each currency is kept
separate. Silver matches `procurement.awards` in the local source PostgreSQL exactly:

| tenant | currency | awards | sum(award_amount), Silver = source |
|---|---|---|---|
| tenant_alder | CAD | 391 | 119,832,699.15 |
| tenant_birch | CAD | 197 | 82,094,667.52 |
| tenant_birch | USD | 40 | 13,444,097.89 |

Duplicate `award_id` keys in `silver_awards`: 0.
