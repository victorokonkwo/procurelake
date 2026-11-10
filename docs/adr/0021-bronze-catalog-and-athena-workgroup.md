# ADR 0021: Bronze catalog tables and the engineering Athena workgroup

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `infrastructure/modules/glue_catalog/`, `infrastructure/modules/athena/`, `infrastructure/modules/iam/` (query policy), `infrastructure/live/dev/foundation/`

## Context
Sprint 1 Day 5 is to configure the Glue Catalog and Athena and add the first integration tests ("Bronze data is queryable and test evidence recorded"). The landed USAspending pages are single-line JSON documents. I checked all 56 pages of the 2026-10-23 batch: no embedded newlines and no `\x01` bytes. Their keys contain spaces (`"Award ID"`), and `NAICS`/`PSC` are nested objects.

## Decision
1. **A raw table that mirrors the source.** `procurelake_dev_bronze.usaspending_spending_by_award_raw` has a single `raw_json string` column: LazySimpleSerDe, delimiter `\x01`, one row per line, so one row per page. Parsing happens in SQL (`json_extract`, `UNNEST`), not in a SerDe. This keeps Bronze independent of the source schema and avoids column renames for keys with spaces.
2. **Partition projection** on `ingestion_date` (a date, from 2026-11-01 to NOW), with a location template matching the ingestion layout. No crawlers and no `MSCK REPAIR`. Athena reads the `batch_id=` subfolders inside each partition (verified).
3. **The saved query** `bronze-usaspending-awards-flattened` (SQL in `infrastructure/live/dev/foundation/queries/`) is the documented flattening. The integration tests run that same SQL, so the saved query is tested.
4. **Workgroup `procurelake-dev-engineering`:** engine v3. The configuration is enforced, so clients can't override the result location or encryption. Results are SSE-KMS encrypted with the data key under `artifacts/athena_results/<workgroup>/` (30-day expiry). The per-query scan cutoff is 1 GiB, and CloudWatch metrics are on.
5. **Least privilege.** Only `data_engineer` gets Athena access (this workgroup only) and Glue **read** access (Bronze database and its tables). Ingestion has none; a live test confirms `AccessDeniedException`.
6. **Lake Formation stays on default IAM-only mode** (`IAM_ALLOWED_PRINCIPALS`, no LF admins, as observed on 2026-11-10) until Week 7, when tenant isolation brings in LF permissions.
7. **Integration tests are opt-in** (`make test-aws`, which needs AWS credentials) and write their evidence to `build/evidence/aws_integration.json`. Day 5's evidence is recorded in `docs/evidence/sprint1-day5-bronze-queryable.md`.

## Consequences
- Bronze readers have to apply the manifest rule (`status = succeeded` and `window_settled`) themselves. The table shows every landed batch, including the unsettled 2026-11-08 page.
- Partitioning by `ingestion_date` prunes by collection date, not by source window.
- No secondary catalog (Silver or Gold) exists yet. Those databases arrive with Weeks 3–4.
