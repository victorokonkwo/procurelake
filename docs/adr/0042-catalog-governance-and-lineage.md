# ADR 0042: Catalog governance (owners, classifications in Glue) and source-to-Gold lineage

- **Status:** Accepted (deployed; `make catalog-check` 18/18 OK on dev 2026-11-13. A defect found
  live on 2026-11-14 is fixed in the maintenance job: see Consequences)
- **Date:** 2026-11-13
- **Implementation:**
  - `src/procurelake/governance/catalog.py`: Silver metadata from the source contracts, the
    version-checked Glue write, read-only drift checks for Silver and Gold
  - `src/procurelake/governance/lineage.py`: lineage from the dbt manifest plus the Silver specs
  - `src/procurelake/governance/cli.py`: `lineage` (offline) and `check` (read-only)
  - `src/procurelake/lakehouse/maintenance_job.py`: saves every maintained table's Glue
    parameters, restores the ones Athena's commits drop, then syncs Silver from the contracts
  - dbt: `+persist_docs` in `dbt/dbt_project.yml`; `meta.data_classification` on every Gold model
  - Make: `lineage`, `catalog-check`; CI runs `make lineage` after `dbt compile`
  - Tests: `tests/unit/governance/`, `tests/unit/dbt/test_gold_governance.py`

## Context
Plan work package E lists **lineage** ("dbt manifests, source-to-model documentation") and
**catalog governance** ("Glue metadata, classifications, owners"). ADR 0031 recorded both as not
built. Lineage was only the dbt manifest uploaded per run. Owners and classifications existed in
the source contracts and the dbt YAML, but nobody browsing Glue, Athena or Lake Formation
could see them.

## Decision
1. **Classification vocabulary.** Gold uses the same vocabulary as the source contracts
   (`synthetic`, `public`, `internal`, `confidential`), declared as `meta.data_classification`
   on every Gold model. A unit test makes it required. A second test forbids `public` on any
   model whose SQL has a `tenant_id`. Gold built from the synthetic tenants is `synthetic`.
   `dim_date` and `mart_ai_contract_intelligence` (public US federal documents) are `public`.
2. **Gold metadata comes from dbt.** `persist_docs` (relation and columns) makes dbt-athena write
   the model description as the Glue `Description` and `comment` parameter, the model `meta` as
   table parameters, and column descriptions as column comments (verified in the installed
   dbt-athena 1.11.1, `impl.persist_docs_to_glue`). It calls `UpdateTable` only when something
   differs, so a run with unchanged docs writes nothing.
3. **Silver metadata comes from the contracts.** Silver tables are created by Spark, not dbt.
   The daily maintenance job (ADR 0033) writes each contracted table's `owner`,
   `data_classification`, `grain`, contract name and version, source table and `tenant_scoped`
   as Glue parameters, plus the description. Two Glue fields are left alone:
   - the Glue `Owner` field;
   - column comments, because an Iceberg commit rewrites the columns from the Iceberg schema.

   `silver_contract_intelligence` has no source contract and is skipped. Its owner is in the
   dbt source YAML.
4. **The Silver write never rolls back an Iceberg commit.** Glue `UpdateTable` replaces the whole
   table, including `metadata_location`. Each write passes the `VersionId` it read. If a CDC or
   Spark commit lands in between, Glue raises `ConcurrentModificationException`. The table is
   then read again and the write retried, at most 5 times. After that it is reported as failed,
   and the job run fails.
5. **Read-only check.** `make catalog-check` compares Glue with the contracts (Silver) and with
   the parsed dbt manifest (Gold). It exits 1 on any drift or missing table, and needs only
   `glue:GetTable`, as the data-engineer role.
6. **Lineage.** `make lineage` joins the dbt manifest (`depends_on`) to the Silver specs. For
   each Gold model it reports:
   - its direct inputs, with ephemeral staging collapsed into the Silver tables it reads;
   - every upstream model;
   - each Silver table's owner, classification, operational source table and Bronze prefixes
     (`bronze/postgres/<table>/`, `bronze/postgres_cdc/load_id=*/<schema>/<table>/`).

   It writes a Mermaid graph to `build/lineage/lineage.md`. The output is generated, not
   committed. CI runs it so it stays buildable.

## Consequences
- **Not yet live.** The Silver catalog step ships in the `procurelake` wheel. It needs
  `make dev-data-platform-plan` and an owner-approved apply, then one maintenance run. The
  maintenance role already has `glue:GetTable`/`UpdateTable` on the Silver and Gold tables
  (ADR 0033), so no IAM change is needed. Gold metadata arrives with the next `dbt build`.
  `make catalog-check` should then report every table OK. Until that has been run, catalog
  governance is not claimed as working in dev.
- **dbt-athena's write is not version-checked.** `persist_docs_to_glue` calls `UpdateTable`
  without `VersionId`. If an Athena `OPTIMIZE`/`VACUUM` commit on the same mart landed between
  dbt's read and write, Glue would point back at the older metadata. If `VACUUM` had already
  removed that metadata's data files, the table would be broken until its pointer is restored
  from the Glue table versions. The window is small: dbt writes only when the docs change (first
  deploy, doc edits), and maintenance runs at 03:30. Deploy doc changes outside the maintenance
  window. Removing the risk would mean patching dbt-athena; not done.
- **Athena maintenance drops Gold metadata (found live 2026-11-14).**
  - What happened: `catalog-check` failed for the 4 Gold marts right after a maintenance run.
    Their Glue entries kept only `metadata_location`, `previous_metadata_location` and
    `table_type`; dbt's `owner`, `grain`, `freshness` and `data_classification` were gone, while
    the description survived. Athena's `OPTIMIZE`/`VACUUM`/`SET TBLPROPERTIES` commits write a
    Glue entry with Iceberg's parameters only.
  - Silver hid the problem, because the catalog step rewrites it from the contracts. Gold had
    nothing to rewrite it until the next `dbt build`. The 18/18 result of 2026-11-13 was taken
    with dbt run after maintenance.
  - Fix: before maintenance the job saves each table's non-Iceberg parameters and description;
    after it, it writes back only the ones now missing, version-checked like the Silver write. A
    value set meanwhile (a dbt run during maintenance) is kept, never rolled back.
- The Silver write adds no Glue table version (`SkipArchive=True`). It writes once per table on
  the first sync, and again only when a contract's metadata changes.
- Lineage covers the operational tables and the AI mart. USAspending is in Silver (ADR 0044) and
  is covered by `catalog-check`, but no Gold model reads it yet, so it is not in the lineage
  report. Canada contracts are not in Silver.
- Published dbt docs (`dbt docs generate`) are still not built: they need a warehouse catalog
  query, and the Glue catalog plus `make lineage` now cover their governance purpose.
