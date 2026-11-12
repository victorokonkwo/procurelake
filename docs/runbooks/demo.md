# Runbook: end-to-end demo (5–10 minutes)

The recording script from the implementation plan, section 4. One `tenant_alder` award is
followed from PostgreSQL to a tenant's Gold mart, then checked from the other tenant's side.

Everything shown is synthetic or public data. `tenant_alder` and `tenant_birch` are fictional,
not SOVRA customers. Award amounts are **awarded value, not spend**.

## Before recording (about 15 minutes, billed)
1. Bring the CDC stack up, with an approved apply for each phase (ADR 0027):
   - `make cdc-plan`, then apply;
   - `make cdc-source-setup`;
   - `make cdc-plan PHASE=2`, then apply.
2. Wait for the DMS task to be `running` with the full load complete, about 3 minutes.
3. Check that `make demo STEP=health` shows no alarm in `ALARM`. The DMS latency alarm can fire
   once at stack creation (open question 39b).
4. Open the dashboard link that `STEP=health` prints, in a browser tab.
5. Credentials: `.env` holds `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. The Makefile loads
   it, and `AWS_REGION` defaults to `ca-central-1` in the scripts.

## The script

| # | Say | Run | What the audience sees |
|---|---|---|---|
| 1 | "A procurement award in the operational PostgreSQL database." | `make demo STEP=show` | One active `tenant_alder` award: amount, currency, agency, supplier. It is remembered in `build/demo/award.json` |
| 2 | "Change the awarded amount. DMS captures it from the WAL." | `make demo STEP=update AMOUNT=<new amount>` | The old and new amount, and the commit time |
| 3 | "The change lands in immutable Bronze as a DMS Parquet file." | `make demo STEP=bronze` (repeat after about a minute if no file yet) | The new CDC files under `bronze/postgres_cdc/load_id=…/procurement/awards/`, and this award's `Op=U` event with its commit timestamp |
| 4 | "The incremental Glue job merges it into Silver Iceberg." | Wait for the next scheduled `cdc_processing` run, then `make demo STEP=silver` | The Silver row with the new amount, `_cdc_op=U`, the source commit time and the Bronze file it came from, plus the Glue run that applied it |
| 5 | "dbt rebuilds the Gold KPI." | `make demo STEP=gold` once that execution's dbt step finishes | `fct_contract_award` with the new amount, and the award's `mart_awarded_value` group, whose total includes it |
| 6 | "The other tenant cannot see it." | `make demo STEP=tenants` | As `tenant_alder`: the group's rows. As `tenant_birch` (own role and workgroup, Lake Formation row filter): **no rows** |
| 7 | "Pipeline health and quality tests." | `make demo STEP=health`, then the dashboard tab | Alarm states, the latest `cdc_processing` and dbt executions, and the dashboard. For the tests, `make dbt-build` prints PASS/WARN/ERROR |
| 8 | "AI-derived contract attributes, approved by a human." | `make ai-review STATUS=approved`, then `make ai-review DOC=… EXTRACTION=…` | **Blocked:** the Bedrock quota (Week 9, ADR 0032). Until extraction has run live, show the review queue design and the evidence checks in ADR 0032. Do not show invented output |

**Steps 4 and 5, and the clock:** the schedule runs every 5 minutes. One run takes about
7.5 minutes (apply about 2.5, dbt about 5), so an overlapping tick is skipped. Measured on
2026-11-12, a change reached Gold 12 minutes after commit (p95, 599 changes). For a shorter
recording:
- cut between steps 3 and 4;
- or, when no `cdc_processing` execution is running, start the steps by hand with
  `make cdc-apply` then `make dbt-build`. A manual start while a scheduled run is active is
  refused (`ConcurrentRunsExceededException`). That refusal is the single-writer guard working.

## Rehearsal, 2026-11-12 (live dev, steps 1–7)

| Step | Observed |
|---|---|
| Setup | The benchmark workloads had cancelled every award, and the mart counts only active awards. One synthetic Alder award (`0134c8fa…`) was set back to `active` by hand for the rehearsal. A fresh stack after `make cdc-source-setup` starts with active awards, so a recording does not need this |
| 1. show | Award `0134c8fa…`, `tenant_alder`, 257,035.98 CAD, active |
| 2. update | 257,035.98 → 312,500.00 CAD, committed 16:29:33 UTC |
| 3. bronze | `awards/20261112-162955372.parquet`, written 16:29:56, with this award's two `Op=U` events: the reactivation and the new amount |
| 4. silver | 312,500.00 CAD, `_cdc_op=U`, source commit 16:29:33.79, the Bronze file above, batch `20261112T163002Z-fb7bf5e5`. Applied by the scheduled run that started 16:28:41 and finished 16:30:57 |
| 5. gold | `fct_contract_award` 312,500.00 CAD active. `mart_awarded_value` group: 1 award, 312,500.00 CAD. Visible at 16:36, when the run's dbt step finished: about 6.5 min after the commit |
| 6. tenants | `tenant_alder`: the group's row. `tenant_birch`: **no rows** for the same query |
| 7. health | All 9 alarms `OK`; latest `cdc_processing` and `dbt_transformation` SUCCEEDED; dashboard link printed |
| 8. Bedrock | Not rehearsed (quota) |

## After recording
- `make cdc-down` (asks for confirmation). **Do not leave RDS and DMS running**: about
  $0.075/hour.
- The demo's update stays in Bronze and Silver as history. It is synthetic data.
