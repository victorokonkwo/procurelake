# Evidence: Sprint 1 Day 5, Bronze is queryable

**Gate:** "Bronze data is queryable and test evidence recorded."
**Result:** met. 6 of 6 integration tests passed against the deployed dev environment.

| | |
|---|---|
| Run | 2026-11-10T09:06:58Z, `make test-aws` (JUnit: 6 tests, 0 failures, 0 errors, 0 skipped, 5.26 s) |
| Environment | dev, `ca-central-1`. Queries ran as the `procurelake-dev-data-engineer` role in workgroup `procurelake-dev-engineering` |
| Table | `procurelake_dev_bronze.usaspending_spending_by_award_raw` (partition projection on `ingestion_date`) |
| Code | Commit that adds this file; tests in `tests/integration/aws/test_bronze_queryable.py` |

The values below are copied from `build/evidence/aws_integration.json`, which the test run wrote. That file is a build artefact and is not committed.

## Checks

| Test | What it proves | Outcome |
|---|---|---|
| `test_workgroup_enforces_engine_encryption_and_scan_cutoff` | The workgroup enforces its settings, uses engine v3, encrypts results with SSE-KMS and the dev data key, and has a 1 GiB scan cutoff | passed |
| `test_raw_table_is_registered_with_partition_projection` | The Glue table has a single `raw_json` column, projection is on, and `ingestion_date` is the partition key | passed |
| `test_every_settled_batch_is_fully_queryable` | For every succeeded, settled manifest, Athena's record, unique-ID and page counts equal the manifest's | passed |
| `test_query_values_match_raw_file_in_s3` | For one award, the fields Athena returns equal the raw JSON object in S3 | passed |
| `test_query_results_are_encrypted_with_platform_key` | The result object is under `athena_results/<workgroup>/` and SSE-KMS encrypted with the data key | passed |
| `test_ingestion_role_cannot_query` | The ingestion role gets `AccessDeniedException` from Athena | passed |

## Query evidence

| Check | Query execution ID | Data scanned | Engine time | Result |
|---|---|---|---|---|
| Batch `20261110T081346Z-184e6c15` (window 2026-10-23) counts | `1556e363-aaa3-4637-a905-c4afa6289de4` | 5,835,057 B | 768 ms | records **5,542**, unique IDs **5,542**, pages **56**, matching the manifest |
| Field values vs raw file | `1ab0212a-58dc-48b8-8650-ce8c691e65c6` | 5,835,057 B | 601 ms | `0029`, `ENSAFE INC`, `16115.05`, `Department of Defense`, NAICS `541620`, equal to page-00001 record 1 |
| Results encryption (whole-table row count) | `db48becf-bf0e-4f92-9e6d-f933d18669bc` | 5,835,057 B | 561 ms | **57** rows: 56 pages from 2026-10-23 plus 1 empty page from the unsettled 2026-11-08 batch |

## Observations

- Athena reads the `batch_id=<id>/` subfolders under each projected `ingestion_date=<date>/` partition. The batch-level counts depend on this and match exactly.
- Both batches were collected on the same `ingestion_date`, so every query scanned that whole partition (about 5.8 MB). Filtering by `ingestion_date` prunes only by collection date. Selecting by source window has to go through the manifest, or through `batch_id` parsed from `$path`.
