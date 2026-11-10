# ADR 0028: CDC apply engine (ordering, tombstones, checkpoint and lease)

- **Status:** Accepted. Deployed 2026-11-10 (Glue `procurelake-<env>-apply-cdc`, DynamoDB `procurelake-<env>-cdc-checkpoints`); the live C5 gate follows
- **Date:** 2026-11-10
- **Implementation:** `src/procurelake/cdc/{apply,checkpoint,job}.py`, `src/procurelake/lakehouse/silver/{transform,writer}.py`

## Context
C3 and C4 of the implementation plan: apply DMS change events (ADR 0027) to Silver. The apply
must handle primary keys, operation types, commit ordering, duplicate input, hard deletes and
reprocessing. It may advance a checkpoint only after the Iceberg write and its validation, and it
must prevent overlapping writers. On 2026-11-10 the owner decided that CDC applies into the same
`silver_*` tables, and that the lock is a DynamoDB lease (open question 15).

## Decision
1. **Sequence.** Every event gets a sortable `_source_sequence`, built from:
   - the commit timestamp in microseconds (`_dms_commit_ts`)
   - full-load files before CDC files
   - the file name
   - the row position in the file (`_metadata.row_index`, verified on Spark 3.5.6 against a real
     DMS file)

   Two changes in one transaction are therefore ordered as DMS wrote them. Nothing relies on
   cross-table order, which DMS Parquet does not preserve.
2. **Idempotent, order-safe MERGE.**
   - An event changes a row only if its sequence is greater than the row's stored
     `_source_sequence`.
   - Replaying any file, or applying files out of order, cannot regress a row.
   - Mutation-checked: removing this guard lets an older insert resurrect a deleted row, and the
     test fails.
3. **Tombstones.**
   - A delete sets `_is_deleted = true` and keeps the row. A DMS delete carries only the key, so
     `tenant_id` comes from the stored row or from other events in the batch.
   - Silver is "current state with tombstones" (implementation plan C3); Gold filters them out,
     which keeps the hard-removal semantics of ADR 0026.
   - New Silver columns, added by Iceberg schema evolution: `_source_commit_ts`,
     `_source_sequence`, `_cdc_op`, `_is_deleted`.
4. **Validation.**
   - Inserts and updates use the same contract checks and rules as the snapshot path. The checks
     were refactored into `transform.with_reasons`, and the existing tests still pass.
   - Rejected events are quarantined under `quarantine/silver_cdc/`.
   - If a key's **latest** event is rejected, the key keeps its previous Silver version: no older
     event is applied in its place.
   - The `max_reject_ratio` quality gate blocks the apply.
5. **A complete full load is a snapshot.** Each CDC session re-seeds the disposable source (ADR 0027),
   so when an apply carries a whole, completed full load for a `load_id`, keys missing from it
   and older than it are tombstoned. One apply never spans two `load_id`s. The caller decides
   completeness: the Glue job will check DMS table statistics.
6. **Ownership.** A CDC apply marks the table `procurelake.cdc_owned = true`. After that, the
   snapshot apply refuses the table (`CdcOwnedTableError`), because snapshot semantics (hard
   delete of absent keys, no commit order) would undo CDC state. The snapshot job remains for
   tables CDC does not own.
7. **Checkpoint and lease (DynamoDB).** One table, keyed `pk = silver table`:
   - `sk = lease`: a conditional put. It succeeds only if there is no lease, the lease expired,
     or the caller already holds it.
   - `sk = file#<key>`: one item per applied Bronze file.
   - `sk = checkpoint`: the last successful batch.

   File and checkpoint writes are transactions that re-check the lease, so a writer whose lease
   was taken over cannot advance the checkpoint.
8. **Order of operations (C4).** Lease → list unapplied files → apply and validate → record the
   files and checkpoint → release (also on failure). A crash after the MERGE but before the
   checkpoint leaves the files unrecorded. The retry re-reads them, the MERGE is a no-op, and then
   it records them. This is tested by injecting the failure.

## Consequences
- **Local tests:** Spark 3.5.6 + Iceberg 1.10.0, with DynamoDB through moto in tests only.
  - 9 apply and job tests: full load, I/U/D, tombstones, out-of-order and replayed files,
    same-transaction ordering, held keys, the quality gate, full-load tombstoning, snapshot
    refusal, checkpoint advance, and crash-and-replay.
  - 3 checkpoint and lease tests.
- **Deployment (`live/dev/data_platform`):**
  - The DynamoDB checkpoint table is pay-per-request, KMS-encrypted, with point-in-time recovery
    and deletion protection. It outlives the disposable CDC stack.
  - The Glue job `apply_cdc` has a least-privilege role: it reads `bronze/postgres_cdc/*`, writes
    Silver and `quarantine/silver_cdc/*`, uses the checkpoint table and three DMS describe calls,
    and is denied Bronze deletes.
  - Full-load completeness comes from DMS table statistics (`Table completed`) when a task ARN is
    passed.
- **Gold:** dbt staging filters `_is_deleted`, and Gold rows carry `source_sequence`, so a Gold
  metric traces to the exact DMS Parquet row. Evidence: `docs/evidence/sprint1-week5-cdc-silver-gold-live.md`.
- **Shared Glue resources** fixed while adding a second job: the account-wide Glue log groups
  moved from the job module to the root (with `moved` blocks), and each job's wheel has its own
  S3 key.
- **Remaining for C5:** a live session (CDC stack up, workload, `make cdc-apply`, `make dbt-build`)
  timed against the 15-minute gate.
