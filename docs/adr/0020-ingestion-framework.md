# ADR 0020: Ingestion framework and the USAspending connector

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `src/procurelake/ingestion/`, `src/procurelake/common/retry.py`, `src/procurelake/common/aws_clients.py`

## Context
Work package A2 requires a Python ingestion framework with pagination, retries with exponential backoff, checksums, raw-file preservation, ingestion manifests, and incremental collection windows. The public API payload and its manifest must be stored before any transformation. Sprint 1 Day 4's goal is "Raw source and metadata land in S3".

## Decision
1. **Raw first, manifest last.** Every source response is stored **byte for byte** in Bronze. The manifest is written only after all pages have landed, and downstream jobs consume only manifests with `status = "succeeded"`.
2. **Create-only landing.** S3 writes use conditional `IfNoneMatch="*"`, so S3 rejects an existing key with 412. Each write sends a SHA-256 checksum, and the checksum S3 computed is compared with the local one. Together with the IAM delete denial (ADR 0019), Bronze objects can be neither overwritten nor deleted by the pipeline roles. Both were verified against the dev bucket.
3. **Layout:**
   - Bronze: `bronze/<source>/<dataset>/ingestion_date=<UTC date>/batch_id=<id>/page-NNNNN.json`
   - Manifest: `pipeline_manifests/<source>/<dataset>/window_start=<date>/batch_id=<id>/manifest.json` in the artifacts bucket
   - Manifest contents: source, dataset, `schema_version`, batch ID, window, the exact request, files (key, URI, bytes, SHA-256, record count), reconciliation, `code_version` (package version plus git SHA), and any error.
4. **Retries:** connection errors, timeouts, 429 and 5xx are retried up to 6 attempts with full-jitter exponential backoff (1 s base, 60 s cap), honouring `Retry-After`. Other 4xx responses fail immediately.
5. **USAspending specifics.** I verified these against the live API and its published contracts on 2026-11-10:
   - Endpoint: `POST /api/v2/search/spending_by_award/` for contract types A–D, filtered on one UTC day of `last_modified_date`, sorted by `generated_internal_id` ascending (a total order).
   - `limit` above 100 returns 422.
   - **`page_metadata.hasNext` is `false` from page 100 onward even when more data exists.** Paging therefore stops at the first page shorter than `limit`. This was confirmed with a mutation test: trusting `hasNext` lands 10,000 of 10,250 records and the batch fails reconciliation.
   - **Reconciliation:** the batch succeeds only if the landed record count equals the count endpoint (`/spending_by_award_count/`) both before and after paging, and equals the number of unique `generated_internal_id` values.
6. **Settling period.** Recent days are incomplete at the source. On 2026-11-10, the daily counts for 11-05 to 11-09 were 136, 103, 0, 0 and 0, against thousands for older days. A day is collected only once it is `settle_days` (10) old. Earlier collection needs `--allow-unsettled` and is recorded as `window_settled = false`, which never completes a window. Manifests without the field are treated as unsettled.
7. **Incremental windows:** `--since` plans every day from that date up to `today − settle_days` that lacks a settled, succeeded batch. This fills gaps and retries failed days automatically.
8. **Runs act as a role.** The CLI assumes `procurelake-dev-ingestion` (`--role-arn`), so the run has only that role's rights.

## Consequences
- **First live result.** Window 2026-10-23: 56 pages and 5,542 records landed. The source count matched before and after, all IDs were unique, every page's SHA-256 matched, and every page was SSE-KMS encrypted with the data key. The run took about 27 s.
- **One premature batch remains in Bronze.** An earlier run for 2026-11-08, made before the settling period existed, landed 0 records and stays as an unsettled batch (immutability). It does not complete that window.
- **Re-collection of settled windows is still open.** Records whose last-modified date falls in an already-settled window but arrive later would be missed. Downstream deduplication must also cope with several batches for the same window. Both are open question 30.
- **The Canada Open Data CSV connector** (pinned snapshot ingestion) still needs to be built on this framework.
