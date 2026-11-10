# Evidence: Week 5, CDC applied to Silver and Gold in dev (C3, C4 deployed)

**Scope:** the Bronze CDC files from the 2026-11-10 DMS session (ADR 0027) were applied to Silver
by the Glue job `procurelake-dev-apply-cdc` (ADR 0028), and Gold was rebuilt with dbt.

**Not covered yet:** the C5 freshness gate (95% of changes in Silver and Gold within 15 minutes).
That needs a live session with the CDC stack up, timed end to end.

## CDC apply run

| | |
|---|---|
| Glue run | `jr_b9586ad1…` SUCCEEDED, 164 s, 329 DPU-seconds |
| Full-load completeness | none claimed: the CDC stack was down, so no DMS task ARN was passed and no full-load tombstoning ran |
| Leases left after the run | 0 |

| Silver table | Events applied (checkpoint) | = full load + workload changes |
|---|---|---|
| silver_tenants | 2 | 2 + 0 |
| silver_agencies | 13 | 13 + 0 |
| silver_suppliers | 107 | 105 + 2 |
| silver_solicitations | 650 | 650 + 0 |
| silver_awards | 629 | 628 + 1 |
| silver_purchase_orders | 1,482 | 1,481 + 1 |
| silver_invoices | 2,860 | 2,823 + 37 |

## Silver (Athena, as the data-engineer role)

`silver_invoices`:
- **Rows:** 2,833 distinct keys, which is 2,823 + 10 inserts.
- **Last operation per row:** 2,806 rows were last written by an insert (I), 26 by an update (U) and 1 by a delete (D).
- **Tombstones:** 1.

Other checks:
- The deleted invoice `689f4835…` is a tombstone. It keeps `tenant_id = tenant_birch` even though
  the DMS delete row carried only the key.
- An inserted invoice is present.
- A renamed supplier shows its new name.
- Table properties: `procurelake.cdc_owned = true`, and `last_applied_batch_id` is the CDC batch.

## Gold (dbt build, 59/59 passed; staging filters tombstones)

| Check | Result |
|---|---|
| Deleted invoice in `fct_invoice` | 0 rows (2,832 invoices = 2,833 − 1 tombstone) |
| Award cancelled by the workload | `award_status = cancelled` |
| Awarded value, tenant_birch CAD | 80,803,419.91, down from 80,841,232.30 because of the cancelled award; Gold = Silver |
| Actual spend, tenant_alder CAD / tenant_birch CAD / tenant_birch USD | 58,909,553.93 / 39,008,474.78 / 7,990,520.58 (up by the workload's payments); Gold = Silver |

## Trace (`make test-aws`: 9 passed)

The largest `mart_awarded_value` cell is tenant_birch · CAD · 2024-05 · 9,288,650.19. The trace
follows it through:
1. Award `3dd42214…` in Gold.
2. `source_sequence = 20261110133903233521|0|LOAD00000001.parquet|000000000452`.
3. Row 452 of the DMS full-load file, whose values equal the Gold row.
4. The DynamoDB checkpoint table, which records that file as applied.
