# ADR 0024: Silver framework (contract-driven validation, quarantine, Iceberg snapshot apply)

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `src/procurelake/lakehouse/silver/`, `configs/pipelines/silver/`

## Context
Work package B1 asks for Glue Spark jobs that conform schemas, reject malformed records, deduplicate on business keys, and write Iceberg tables. It also asks for "an explicit pipeline framework, with configuration for source, table, watermark, validation rules, and target location". Silver must be correct and idempotent, and must never lose good data because of bad input.

## Decision
1. **Spec = source contract + pipeline config.** Types, primary key, NOT NULL and allowed values come from `contracts/source/`. Row rules (Spark SQL booleans that mirror the database CHECK constraints), partitioning (`tenant_id`) and `max_reject_ratio` come from `configs/pipelines/silver/<table>.yaml`.
2. **Validation reasons, root cause first:**
   - Structural: `unparseable_json`, `type_mismatch:<col>`, `not_null:<col>`, `too_long:<col>`, `not_allowed:<col>`.
   - Business rules (`rule:<name>`) run only on structurally valid rows, so one bad value isn't reported twice.
   - A rule that evaluates to NULL is a failure.
3. **Deduplicate** on the primary key, keeping the latest `updated_at`. Older copies are rejected as `duplicate_key_superseded`.
4. **Quarantine:** every rejected row is written with its raw line, file, reasons and key under `<quarantine_root>/silver/<table>/batch_id=<id>/`.
5. **Quality gate:** if `rejected / input > max_reject_ratio` (tenants 0%, other tables 1%), Silver is not updated.
6. **Snapshot apply** (Iceberg `MERGE`, format v2, copy-on-write):
   - Changed rows (by `_row_hash`) are updated and new keys inserted.
   - Keys missing from the snapshot are deleted.
   - **Rejected keys keep their previous Silver version.**
   - Unchanged rows are not rewritten.
7. **Idempotency and ordering:** each table stores `procurelake.last_applied_batch_id`. The same batch is skipped, and an older batch raises `StaleBatchError`. DynamoDB checkpoints replace this in Week 6 for CDC.
8. **Integrity:** the files read must contain exactly the manifest's record count, otherwise `BronzeMismatchError`.
9. **Provenance columns:** `_batch_id`, `_source_file`, `_source_line_hash`, `_row_hash`, `_silver_updated_at`.
10. **Local testing** uses Spark 3.5.6, Iceberg 1.10.0 and Java 17, the versions Glue 5.1 bundles per the AWS Glue Iceberg documentation, with a Hadoop catalog. CI installs Temurin 17.

## Consequences
- **Tested locally end to end** (PostgreSQL → snapshot → Silver): every table reconciles with the source, values are exact, re-apply is a no-op, source updates and deletes propagate, stale batches are refused, the quality gate blocks, and Bronze/manifest mismatches are caught. The rejected-row retention rule was mutation-checked: without it, the row is deleted and the test fails.
- **Cross-table referential checks** (for example, an award's supplier existing in `silver_suppliers`) are not done in Silver. They belong in dbt relationship tests (Week 4).
- **Local quirk:** PySpark's `collect()` shows timestamps in the machine's local zone. Tests check stored instants inside Spark.
- **Deployed to AWS Glue 5.1 in ADR 0025** (job, Terraform, live run in dev).
