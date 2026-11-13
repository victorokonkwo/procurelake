# ADR 0041: The end-to-end acceptance suite (plan §3): two tiers, and Silver schema evolution

- **Status:** Accepted (all 12 scenarios passing: local tier 2026-11-12, live tier 2026-11-13;
  see Consequences)
- **Date:** 2026-11-12
- **Implementation:**
  - Local tier: `tests/e2e/` (`conftest.py`, `e2e_support.py`, `cdc_flow.py`, six test modules)
    and `tests/integration/iceberg/test_schema_change.py`
  - Live tier: `tests/e2e/test_cdc_to_gold.py`, `tests/security/`, `tests/performance/`
  - Shared fixtures: the Spark session moved to the root `conftest.py`; the live-AWS fixtures
    moved to `tests/aws_fixtures.py`, registered as a pytest plugin
  - Code change: `procurelake.lakehouse.silver.writer.ensure_table` evolves the schema
    (`SchemaEvolutionError`)
  - Make: `test-acceptance`, `test-acceptance-live`; `test-aws` now also runs
    `tests/security` and `tests/performance`

## Context
The plan says the platform "isn't complete until all of these pass, with automated integration
tests and a small, reproducible e2e dataset". Most scenarios were covered somewhere, by
component integration tests or live harness scripts. Two gaps remained:
- the named locations (`tests/e2e`, `tests/security`, `tests/performance`) did not exist;
- test 6 (schema change) had no test at all.

Writing test 6 showed a real defect. A nullable column added to a contract, which ADR 0022
allows within a contract version, was never added to the existing Silver table. The Silver
Spark job then failed on the next snapshot.

## Decision
1. **Two tiers, one dataset.**
   - **Local tier:** PostgreSQL, Spark with Iceberg, and test doubles. CI runs it in the
     `test` job, which already has PostgreSQL 17 and Java 17.
   - **Live tier:** the deployed dev environment; opt-in.
   - Both start from the generator's seed-42 dataset: 5,702 rows over the two fictional
     tenants.
   - Gold runs only on Athena (dbt-athena), so Gold assertions are live.
2. **Real pipeline code everywhere:** the snapshot connector, the Silver job's `run_batch`,
   the CDC job's `run_cdc_table` with its DynamoDB lease and checkpoint, the AI pipeline,
   review and Silver rows.
   - Test doubles exist only in `tests/`: moto for DynamoDB, and the existing Bedrock and
     Textract fakes.
   - `DmsDouble` writes PostgreSQL's real committed rows as DMS-shaped Parquet, the shape
     DMS wrote in dev (ADR 0027).
   - Tests compare Silver with PostgreSQL's actual current rows on every non-timestamp column.
3. **Schema evolution in `ensure_table`** (both the snapshot and CDC paths):
   - a contract column missing from the table is added with Iceberg `ADD COLUMNS`
     (metadata only; existing rows read null);
   - a new `NOT NULL` column is refused before any write;
   - the table's `procurelake.contract_version` property is refreshed.
   Without the change, both tests fail with Spark errors; with it, both pass.
4. **Where each scenario lives:**

   | # | Scenario | Test | Tier |
   |---|---|---|---|
   | 1 | Full ingestion | `tests/e2e/test_full_ingestion.py` | local |
   | 2, 3 | CDC update, delete into Gold | `tests/e2e/test_cdc_to_gold.py` | live, dev/cdc up |
   | 4 | Replay (snapshot and CDC) | `tests/e2e/test_replay.py` | local |
   | 5 | Late arrivals | `tests/e2e/test_late_arrivals.py` | local |
   | 6 | Schema change | `tests/integration/iceberg/test_schema_change.py` | local |
   | 7 | Data quality | `tests/e2e/test_data_quality.py` (plan said `tests/integration/`; it needs the e2e fixtures) | local |
   | 8 | Tenant isolation | `tests/security/test_tenant_isolation.py` | live |
   | 9 | Unauthorized access | `tests/security/test_unauthorized_access.py` | live |
   | 10 | Pipeline failure | `tests/e2e/test_pipeline_recovery.py` | local |
   | 11 | AI missing fields | `tests/e2e/test_ai_document_to_gold.py` (fakes; live blocked on Bedrock) | local |
   | 12 | Performance | `tests/performance/test_query_performance.py` | live |

5. **Test 12 compares fact against mart, not partition pruning.**
   - By OQ13 (ADR 0034), Silver is partitioned by `tenant_id` only. The first version of the
     test measured pruning and found none: 2,600,139 bytes either way, as this morning's bench
     runs also show.
   - The optimization the platform does use for these questions is the Gold marts. The test
     asks each question of the fact table and of the mart, requires identical answers, and
     reports both measurements.
6. **Test 2–3 does not start the pipeline.** It commits one award update and one invoice
   delete in RDS, then polls Silver and Gold for up to 30 minutes while the schedule runs the
   pipeline, as in production.

## Consequences
- **Local tier, 2026-11-12:** all 10 tests pass, including the 2 schema-change tests. The
  full local suite gives 452 passed, plus 30 live tests skipped as opt-in, in 3 min 50 s.
  - Test 11 found that the review step demands an explicit decision on a field whose evidence
    check failed: "accept every passed field" does not let it through. The test asserts this.
- **Live tier, 2026-11-12:**
  - **Passed:** test 9, unauthorized access (6 tests), and test 12, performance:

    | Question (63 rows, same answer) | Fact | Mart | Bytes ratio |
    |---|---|---|---|
    | Monthly awarded value | 563,730 B, 1,022 ms | 500,409 B, 783 ms | 0.89 |
    | Monthly actual spend | 2,258,207 B, 978 ms | 1,279,796 B, 830 ms | 0.57 |

    All four queries are under Athena's 10 MB per-query minimum, so no cost is saved at this
    size.
  - **First run, test 8 failed on dev's data, not on isolation.** No tenant saw another's rows.
    - The CDC benchmark workloads (2026-11-12) had cancelled every award, so three marts held no
      `tenant_birch` rows. The test refuses to call isolation proven for a tenant with none.
    - The exports were written at 06:00, before those changes (1,162 rows exported against
      1,418 at test time).
- **Live tier, CDC session 2026-11-13** (fresh dev/cdc stack; RDS seeded with seed 42,
  fingerprint `350d064e…`; DMS full load complete at 00:47:10 UTC, applied by the 00:48 scheduled
  run):
  - **Tests 2–3 passed.** One `tenant_alder` award's amount was changed and one invoice deleted,
    committed at 00:57:40 UTC. Visibility, as polled:

    | Check | Visible at (UTC) | After commit |
    |---|---|---|
    | Silver award amount | 01:00:06 | 2 min 26 s |
    | Silver invoice tombstone | 01:00:08 | 2 min 29 s |
    | Gold `fct_contract_award` amount | 01:01:16 | 3 min 37 s |
    | Gold invoice absent from `fct_invoice` | 01:01:18 | 3 min 38 s |

    Evidence: `build/evidence/acceptance_cdc_to_gold.json` (not committed).
  - **Test 8: the full live suite (`make test-aws`) ran 28 passed and 1 failed.** The failure
    was Alder's `mart_actual_spend` export: 1,154 rows against 1,153 in the mart.
    - The exports had run at 01:01:26 UTC, inside the pipeline run that carried the delete
      (00:58:18–01:04:46), before that run rebuilt the mart.
    - Every export still held only its own tenant's rows.
    - The exports were rerun with the pipeline idle. All 12 tenant isolation tests then passed:
      each tenant sees only its own rows in all four marts, the exports hold only the tenant's
      rows and match the mart counts, and the admin sees both tenants.
  - **Test-ordering rule:** the export count check compares a daily snapshot with the live mart.
    Run `make tenant-exports-run` and test 8 only while `cdc_processing` is idle, never during
    a CDC test.

## Alternatives considered
- **A local Gold:** there is no local engine for dbt-athena. Re-implementing the marts in Spark
  would test a copy, not Gold. Gold stays live.
- **Real DMS in the local tier:** it needs RDS and DMS. The double writes the documented file
  shape from real committed rows, and the live tests 2–3 exercise real DMS.
