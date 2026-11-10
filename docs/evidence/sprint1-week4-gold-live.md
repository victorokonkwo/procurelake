# Evidence: Week 4, first full Bronze-to-Gold run in dev

**Gate (implementation plan B3):**
- `dbt build` produces the expected Gold models.
- Uniqueness, not-null, relationship, financial reconciliation and business-rule tests pass.
- A Gold metric can be traced back to its originating source records.

**Result:** met, on 2026-11-10, in dev (`ca-central-1`). Incremental merge models are deferred to
Week 6 by the owner's decision (ADR 0026).

| | |
|---|---|
| Chain | local PostgreSQL → snapshot batch `20261110T093202Z-983c92e5` in Bronze (ADR 0023) → Silver Iceberg via Glue (ADR 0025) → Gold via dbt on Athena (ADR 0026) |
| dbt | dbt-core 1.12.6, dbt-athena 1.11.1, run as `procurelake-dev-data-engineer` in workgroup `procurelake-dev-engineering` |
| Gold database | `procurelake_dev_gold` (Iceberg tables under `s3://<lake>/gold/procurelake_dev_gold/<model>/<uuid>/`) |

## dbt build

| Invocation | Result | Elapsed |
|---|---|---|
| `f8712212-530d-4f27-86c1-3f5c0c1ab396` | PASS=59 (10 models, 49 tests), ERROR=0, WARN=0 | 34.5 s |
| `990209be-76fd-4968-8947-a94a80659ae2` (rebuild) | PASS=59, ERROR=0, WARN=0; identical row counts; previous table locations removed | 45.7 s |

Three earlier attempts failed and were fixed (ADR 0026):
1. A source schema was given as an unrendered `var`.
2. Athena views cannot hold `timestamp(6)`, so staging became ephemeral.
3. The lineage macro was applied twice, and a boolean accepted-values test was quoted.

None of these attempts left tables or orphaned files: every Gold table has exactly one S3
location, and it is the one in its Glue table.

| Model | Rows | | Model | Rows |
|---|---|---|---|---|
| dim_agency | 13 | | fct_invoice | 2,823 |
| dim_supplier | 105 | | mart_awarded_value | 596 |
| dim_date | 975 | | mart_supplier_concentration | 123 |
| fct_contract_award | 628 | | mart_procurement_cycle_time | 309 |
| fct_purchase_order | 1,481 | | mart_actual_spend | 1,733 |

The 49 tests include these singular tests:
- `assert_awarded_value_reconciles_to_silver`
- `assert_actual_spend_reconciles_to_silver`
- `assert_facts_keep_every_silver_row`
- `assert_fact_references_share_the_tenant`
- `assert_supplier_shares_sum_to_one`
- `assert_award_not_before_publication`
- `assert_paid_within_invoice`

## Live integration tests (`make test-aws`): 9 passed in 16.2 s

The 6 Bronze tests from Day 5 still pass. The 3 new Gold tests are in
`tests/integration/aws/test_gold_traceability.py`.

### Trace: Gold metric → source record

| Step | Value |
|---|---|
| Gold cell (largest in `mart_awarded_value`) | tenant_birch · agency `41960e30…` · supplier `451abf4c…` · 2024-05 · **CAD 9,288,650.19** · 1 award (query `635b1c65-3756-4ad7-895c-638279e24c99`) |
| `fct_contract_award` row | award `3dd42214-7526-4a2d-a3b7-78c09e3d0919`, 9,288,650.19 CAD, batch `20261110T093202Z-983c92e5` (query `16e2d420-7b4a-48a7-884c-cfde9eca871b`) |
| Bronze line | `bronze/postgres/awards/ingestion_date=2026-11-10/batch_id=20261110T093202Z-983c92e5/part-00001.jsonl`, line SHA-256 `904fbb1d…82ee6e`. The JSON values equal the Gold row |
| Manifest | That file is listed in the batch's succeeded manifest, and its whole-file SHA-256 matches |
| Source row | `procurement.awards` in local PostgreSQL: `3dd42214-… · 9288650.19 · CAD · active` |

### Reconciliation: Gold = Silver, per tenant and currency

Awarded value is not spend, and the two are reported separately. Currencies are never summed together.

| tenant | currency | awarded value (active awards) | actual spend (cash paid) |
|---|---|---|---|
| tenant_alder | CAD | 116,127,397.66 | 58,415,701.47 |
| tenant_birch | CAD | 80,841,232.30 | 38,930,626.53 |
| tenant_birch | USD | 13,444,097.89 | 7,990,520.58 |

Query IDs:

| Check | Gold query | Silver query |
|---|---|---|
| Awarded value | `6b7a8c47-9da4-4fc1-88b1-57f245e5141b` | `50d68ed1-e94a-4ca9-a05c-49d449a730af` |
| Actual spend | `ec094da5-a6f9-4a69-9c25-7bdfb1446af7` | `521f3673-7554-4700-af71-058c4bdfb7a4` |

The Week 3 Silver totals (`docs/evidence/sprint1-week3-silver-live.md`) include cancelled awards.
`mart_awarded_value` counts active awards only.
