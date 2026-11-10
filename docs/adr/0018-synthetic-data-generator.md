# ADR 0018: Deterministic synthetic data generator

- **Status:** Accepted
- **Date:** 2026-11-10
- **Implementation:** `src/procurelake/data_generator/`, `configs/tenants/synthetic_tenants.yaml`, `scripts/local/seed_db.py`

## Context
Sprint 1 Day 2 requires "a repeatable seeded dataset with valid relationships" for at least two synthetic tenants. Later work depends on this dataset: CDC tests, tenant-isolation tests, dbt reconciliation tests, and performance benchmarks. It has to be identical on every run, valid under every database constraint, and realistic enough to exercise the Silver and Gold logic.

## Decision
1. **Determinism.** Each tenant gets its own `random.Random` seeded with `procurelake:<seed>:<tenant_id>`. UUIDs come from that stream, not from `uuid4()`. All timestamps fall in a fixed window (`history_start` to `as_of`) from config, never the wall clock. A SHA-256 **fingerprint** of every row identifies a dataset (seed 42 at scale 1 is `350d064e…`).
2. **Statuses derived from a simulated timeline.** Each solicitation gets a timeline (created → published → closing → award). Its status comes from where `as_of` falls on that timeline, so statuses, timestamps, and amounts always agree, and nothing happens after `as_of`.
3. **Money invariants.** Purchase orders under an award total at most the award amount. Invoices total at most their purchase order. Payment status agrees with the paid amount. Currency flows from award to PO to invoice. Tenant `tenant_birch` mixes CAD and USD so the rule against cross-currency aggregation gets exercised.
4. **Deliberate data-quality material.** About 5% of suppliers are near-duplicate spellings of an existing supplier, for Silver deduplication and `dim_supplier` canonicalisation. Some POs have no award ("open-market"). Every allowed status value appears.
5. **Supplier popularity is skewed** (Zipf-like) so `mart_supplier_concentration` has a meaningful result.
6. **No `contract_documents` rows yet.** A document key must point to a real document. Rows are added in Week 9, when real public contract PDFs are ingested, rather than pointing at objects that don't exist.
7. **Loading.** COPY in a single transaction, with row counts verified afterwards. The loader refuses non-empty tables unless `--reset` is given. The CLI only loads into a local host unless `--allow-remote-host` is given.
8. **Scaling.** `--scale` multiplies supplier and solicitation counts, for the Week 10 benchmarks (100k–1M records).

## Consequences
- Tests can assert exact values, and the fingerprint detects any unintended change to the generator. **Any intentional change to the generator changes the fingerprint.** Note it in the commit.
- Amount distributions and lifecycle probabilities in `catalog.py` and `generate.py` are illustrative. They are not calibrated to real procurement data and must never be presented as statistics.
- The Week 5 CDC workload (inserts, updates, cancellations, deletes against a seeded database) is a separate component built on this dataset.
