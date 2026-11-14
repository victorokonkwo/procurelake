# ADR 0044: USAspending in Silver (current state per award) and a weekly 30-day re-sweep

- **Status:** Accepted (owner decisions 2026-11-13; resolves open questions 30 and 34). Built and
  tested locally; not yet deployed (see Consequences).
- **Date:** 2026-11-13
- **Implementation:**
  - Contract: `contracts/reference/usaspending_contract_awards.yaml`, model
    `src/procurelake/contracts/reference.py` (bundled in the wheel)
  - Silver: `src/procurelake/lakehouse/silver/reference.py` (validate, deduplicate, MERGE),
    `reference_job.py` (pending batches, run summary), Glue entrypoint
    `jobs/glue/reference_to_silver.py`
  - Re-sweep: `resweep_days` and `ingest_day(purpose=…)` in the USAspending connector,
    `--resweep-days` in the ingestion CLI, task `resweep-usaspending` in the task runner
  - Terraform (`dev/data_platform`): `reference_silver.tf` (job and least-privilege policy), the
    batch ingestion machine's Silver step, the Sunday re-sweep schedule, the table in daily
    Iceberg maintenance
  - Governance: Glue metadata for the table (ADR 0042), dbt source tests
    (`dbt/models/staging/usaspending/`)
  - Make: `reference-silver-run`; `batch-ingestion-run TASK=resweep-usaspending`
  - Tests: `tests/integration/iceberg/test_reference_silver.py` (10, local Spark), connector,
    task runner, governance and Terraform tests

## Context
USAspending contract awards land in Bronze as one batch per UTC day of `last_modified_date`
(ADR 0020). Each batch is the raw API pages plus a manifest reconciled against the source count.
Only the operational tables reached Silver (Week 3). Two questions were open:
- **34:** what is the Silver grain, and how are batches deduplicated?
- **30:** records can change after the 10-day settling period, so should settled days be
  collected again?

Facts the design rests on:
- **An award changes by reappearing.** When it is modified, it shows up again in the batch for
  its new modification date. So the same `generated_internal_id` can sit in many batches.
- **The API reports no deletes.** It publishes current records only.
- **Field formats come from a real response** (the first 24,800 of the 75,201 records of the
  2026-10-30 window, read 2026-11-13; the API blocked the rest, see below):
  - `Last Modified Date` is `YYYY-MM-DD HH:MM:SS` with no time zone;
  - NAICS and PSC are `{code, description}` objects;
  - amounts are JSON numbers, none with more than 2 decimals in that sample.
- **Day sizes vary a lot.** 2026-10-23 had 5,542 records; 2026-10-30 had 75,201 (about 750 pages
  of about 100 KB).
- **The API blocks fast clients** (found on the first dev runs, 2026-11-13/14). An earlier draft of
  this ADR read the failures as the API dropping connections on large days. What happens instead:
  - after about 250 requests at 2 to 4 per second, the API closes every connection without a
    response, for about 5 minutes (measured: refused at 02:17 to 02:20 UTC, served at 02:21);
  - every large day therefore stopped at exactly 24,800 records (248 pages), from Fargate and
    from a workstation alike, and the next days in the same run failed too;
  - page 249 on its own was served normally, so there is no paging cap;
  - no rate-limit header is sent.

  The connector now spaces requests 1.25 s apart (240 per 5 minutes) and, when refused, waits 1 to
  5 minutes per retry over 8 attempts (at least 7 minutes in all), longer than a block. No batch
  failed this way reached Silver: each was recorded as failed and never applied.

## Decision
1. **Grain (owner): current state.** `silver_usaspending_contract_awards` has one row per
   `generated_internal_id`:
   - the latest `Last Modified Date` wins;
   - a tie goes to the later batch (batch IDs sort by collection time);
   - an older or identical record never changes the row, so replays and re-sweeps are safe.
2. **Reference contract, not a source contract.** Source contracts describe PostgreSQL tables and
   are checked against the live schema. The new reference contract maps API fields to columns
   instead (`source_field`, nested `NAICS.code`). It also fixes four things:
   - classification `public`;
   - `currency_code: USD`, added as a column, as AGENTS.md §2.4 requires;
   - `deletes: none`;
   - the reject threshold, 1%.
3. **Types are exact or rejected.**
   - Amounts are parsed as wide decimals and must fit `numeric(23,2)` without rounding.
     Otherwise the record is quarantined (`type_mismatch`), never silently rounded.
   - `last_modified_at` is an Iceberg timestamp without time zone, because none is published.
     Athena reads that type (AWS docs, "Supported data types for Iceberg tables in Athena").
4. **Upsert only, with an order guard.** A row is replaced only by a strictly newer
   `last_modified_at`, or by the same timestamp from a later batch with different content.
   After the MERGE, the job checks that no incoming key is newer than Silver.
5. **Checkpoint after the write.** Pending batches are the succeeded, settled manifests of the
   dataset and Bronze schema version. Applied batches are the ones listed in the job's earlier run
   summaries. A summary is written only after the MERGE and its check succeed, so a crash
   re-applies the same batches, which is idempotent. Unsettled batches are not applied.
6. **Quality gate per batch.** Rejected records go to
   `quarantine/silver/spending_by_award/batch_id=<id>/` with reasons. A batch over 1% rejected is
   blocked: it is not applied and not marked applied, and the run fails and alerts. Other batches
   in the same run still apply.
7. **Re-sweep (owner): weekly, trailing 30 settled days.**
   - Every Sunday at 04:00 Toronto time (after the 03:30 maintenance), the batch ingestion machine
     runs `resweep-usaspending`. It collects the 30 most recent settled days again as new batches
     marked `purpose: resweep`.
   - Silver then applies them.
   - Every run summary reports, per batch: new keys, changed keys, unchanged keys, and records
     older than Silver. This is the measurement open question 30 asked for. If the re-sweeps keep
     finding nothing new or changed, drop or shorten them (`schedule_usaspending_resweep`,
     `usaspending_resweep_days`).
8. **Orchestration.** After any USAspending ingestion (daily 07:00, or the re-sweep), the batch
   ingestion machine starts the Glue job synchronously.
   - Glue allows one run at a time, which keeps a single writer.
   - The step retries three times, 5 minutes apart and doubling, because the likely error is
     another run in progress. Then it alerts.
   - The daily maintenance compacts the table and syncs its catalog metadata (ADR 0033, 0042).
9. **No Gold yet.** Whether public data joins tenant products is open question 12. dbt only tests
   the Silver table as a source: unique and not-null key, not-null `last_modified_at`, currency
   USD.

## Consequences
- **Volume.** A re-sweep is 30 days of 5,500 to 75,000 records each. That is roughly 2,000 to
  20,000 pages and up to about 1 GB of new immutable Bronze per week, estimated from the two days
  above, not measured.
  - Cost is small: S3 storage and a 1 vCPU Fargate task for up to a few hours.
  - Paced at 1.25 s per request, a re-sweep takes about 40 minutes to 7 hours; a daily run of up
    to 3 days, up to about 50 minutes. A day that still fails is collected again by the next run.
  - The first weekly summaries will show the real sizes and findings.
- **Deploy order.** The dbt source tests fail until the table exists. After the data_platform
  apply, run `make batch-ingestion-run TASK=ingest-usaspending`, or `make reference-silver-run`,
  before the next `dbt build` or data-quality run. The first run applies every settled batch
  already in Bronze, up to 60 per run.
- **Not handled:**
  - source deletes, which the API does not expose;
  - unsettled batches, which stay in Bronze only;
  - the Canada contracts CSV, which stays in Bronze (no decision on its Silver grain).
- **Precision.** `numeric(23,2)` assumes the API publishes cents. A sub-cent amount is
  quarantined, and the batch gate shows it.
