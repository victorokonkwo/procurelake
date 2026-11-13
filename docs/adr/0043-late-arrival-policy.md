# ADR 0043: Late-arrival policy: ordering resolved in Silver, Gold recomputed in full (no lookback)

- **Status:** Accepted (owner decision 2026-11-13; resolves open question 8)
- **Date:** 2026-11-13
- **Implementation:** none new. This records the behaviour already built and tested:
  - Silver ordering: ADR 0028, `src/procurelake/cdc/`
  - Gold materialization: ADR 0026 (core rebuilt as tables), ADR 0030 (marts recomputed in full,
    then merged, with `delete_vanished_keys`)
  - Tests: `tests/e2e/test_late_arrivals.py` (acceptance test 5), the CDC apply tests for
    out-of-order and replayed files (ADR 0028)

## Context
AGENTS.md §2.5 and plan item B2 require incremental dbt lookback windows to be backed by a
documented late-arrival policy, with explicit delete handling. Open question 8 asked for that
policy: the lookback window for incremental models, and how late or out-of-order CDC events are
resolved. ADR 0026 deferred it to Week 6, when CDC arrived.

Week 6 was built without a lookback window, so the question became: is a lookback needed at all?

- **Out-of-order events are already resolved in Silver.** Every CDC event gets a sortable
  `_source_sequence`: the commit timestamp in microseconds, then full load before CDC, the file
  name, and the row position (ADR 0028). An event changes a row only if its sequence is greater
  than the row's stored sequence. A late file, a replayed file, or a delete arriving after a
  newer insert therefore cannot regress a row.
- **Gold never reads a window.** The core dims and facts are rebuilt in full each run (ADR 0026).
  The marts are recomputed in full and then merged (ADR 0030), so the table and its Lake
  Formation filters survive, and keys absent from the new result are deleted. No model filters
  its input on `_source_commit_ts` or `source_updated_at`.

## Decision
1. **Ordering is resolved in Silver, by source sequence (ADR 0028).** Lateness is measured in the
   source's commit order, not arrival time. A late event is applied if, and only if, it is the
   newest state for its key.
2. **Gold has no lookback window.** Every Gold model reads the whole current Silver state on every
   run. A late event reaches Gold on the first dbt run after it reaches Silver, however late it
   is. No event can fall outside a window, because there is none.
3. **Deletes are explicit at both layers.** In Silver, a delete is a tombstone
   (`_is_deleted = true`, ADR 0028). In Gold, staging filters tombstones out. Rebuilt tables lose
   the row, and the marts delete vanished keys (`delete_vanished_keys`, ADR 0030).
4. **Selective builds keep this policy.** Open question 40's fix builds only the models
   downstream of Silver tables that changed. A model it skips has unchanged inputs, so skipping
   it gives the same result as recomputing it.
5. **When to revisit.** Switch to truly incremental Gold models, with a lookback window on
   `_source_commit_ts` and this ADR superseded, only when a full recompute no longer fits the CDC
   schedule. Measured so far:
   - `dbt build` of the Gold project (models and tests) took 38.9 s at about 100,000 source
     rows and 41.5 s at about 1,000,000 (bench namespace, `docs/benchmarks/2026-11-11-bench-100k-1m.md`);
   - the dbt step of a CDC run takes about 5 minutes end to end, Fargate start-up and tests
     included (`docs/benchmarks/2026-11-12-replay-concurrency-cdc.md`).

   So the recompute is not yet what limits freshness. The window would then be set from measured
   lateness:
   - the gap between `_dms_commit_ts` and the Bronze file's arrival;
   - plus the longest pipeline outage the recovery runbooks allow.
6. **Public reference data** (USAspending) has its own lateness: records added to a day after it
   is collected. That is handled by the dedup and the re-sweep decided for open questions 30 and
   34, not by this policy.

## Consequences
- Late and out-of-order CDC events need no tuning parameter, and none can be silently dropped by a
  too-short window.
- The cost is a full Gold recompute per run, which grows with total data, not with change volume.
  That is the trigger in decision 5.
- Acceptance test 5 already covers decision 1; nothing new to test.
