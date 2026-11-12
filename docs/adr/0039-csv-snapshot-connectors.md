# ADR 0039: CSV snapshot connectors (generic, and Government of Canada contracts)

- **Status:** Accepted (landed in dev Bronze 2026-11-12)
- **Date:** 2026-11-12
- **Implementation:**
  - `src/procurelake/ingestion/connectors/csv_snapshot.py`: the generic connector
  - `src/procurelake/ingestion/connectors/canada_contracts.py`: the Government of Canada source
  - `src/procurelake/ingestion/landing.py`: `put_new_file`, create-only multipart landing
  - `src/procurelake/ingestion/http.py`: `download_to`, a streaming download with retries
  - CLI `canada-contracts` and `csv`; `make ingest-canada-contracts[-local]`,
    `make ingest-csv-local`
  - Tests: `tests/unit/ingestion/test_csv_snapshot.py`
  - IAM: the ingestion role may abort its own multipart uploads under `bronze/`
- **Builds on:** ADR 0002 (immutable Bronze) and ADR 0020 (ingestion framework). Closes ADR
  0020's open item, "the Canada Open Data CSV connector (pinned snapshot ingestion)".

## Context
Work package A2 lists three connectors: USAspending (done, ADR 0020), the Government of Canada
procurement CSV, and a generic CSV connector. It also asks that "public-file ingestion can be
reproduced from a pinned download snapshot".

The Canadian source is the open-data dataset "Proactive Publication - Contracts"
(`d8f85d91-7dec-4fd1-8055-483b77225d8b`), resource "Contracts over $10,000". The catalog,
checked 2026-11-12, gives:
- one CSV, replaced in place, frequency `P3M`;
- licence `ca-ogl-lgo` (Open Government Licence - Canada);
- a published schema at `open.canada.ca/data/recombinant-published-schema/contracts.json`.

**Measured on the file downloaded 2026-11-12:**

| Property | Value |
|---|---|
| Size | 655,507,792 bytes, equal to `Content-Length` |
| `Last-Modified` / `ETag` | Thu, 12 Nov 2026 06:56:19 GMT / `"0x8DF408A0E37BB97"` |
| SHA-256 | `bb0fbab2978dbd63df582a7bdc357d88d3dd7c83680183a52889843267f18375` |
| Encoding | UTF-8 with a byte-order mark; quoted fields may contain newlines |
| Columns | 43: the published schema's 41 fields plus `owner_org`, `owner_org_title` |
| Records | 1,336,184, none with the wrong field count; longest field 3,028 characters |
| `reference_number` distinct | 529,862: **not unique**, departments reuse numbers |
| `(owner_org, reference_number)` distinct | 1,336,184: **unique on every record**, none empty |
| `(owner_org, procurement_id)` distinct | 1,105,759: not unique |

## Decision
1. **A snapshot is one batch:** the whole published file, landed **byte for byte**:
   - Bronze: `bronze/<source>/<dataset>/ingestion_date=<UTC>/batch_id=<id>/<file>`;
   - manifest: `pipeline_manifests/<source>/<dataset>/window_start=snapshot/batch_id=<id>/`.
   Nothing is parsed into a new shape in Bronze.
2. **Reconciliation, without a source count endpoint.** A batch succeeds only if all of these
   hold:
   - the downloaded size equals `Content-Length`;
   - the header equals the source contract, so an added, removed or reordered column fails
     instead of drifting silently;
   - every record has the header's field count;
   - when the source has a key, no key is empty and every key is unique. For Canada the key is
     `(owner_org, reference_number)`, as measured above.

   The manifest records the record count, the unique key count and the first malformed record
   numbers. A failed batch keeps its raw file in Bronze (ADR 0020).
3. **Pinning.** Every manifest records:
   - the published URL, the final URL after redirects, the `ETag`, `Last-Modified`,
     `Content-Length` and the SHA-256;
   - the licence and the provenance page.

   `--expect-sha256` fails a batch unless the file is that exact snapshot. `--path` lands an
   already-downloaded copy (for example the Bronze file of an earlier batch) and still records
   the published URL. Together they reproduce a run from a known download.
4. **Large files never sit in memory:**
   - the download streams to a temporary file;
   - files over 64 MiB land as an S3 multipart upload: 64 MiB parts, each sent with its SHA-256
     for S3 to validate, then `CompleteMultipartUpload` with `IfNoneMatch="*"`. AWS documents
     that conditional writes apply to that call, so the object stays create-only;
   - a multipart object's SHA-256 is composite (a checksum of the part checksums; AWS
     documents full-object multipart checksums only for CRC types), so the whole-file SHA-256
     is computed locally and recorded;
   - a failed upload is aborted. The ingestion role gets `s3:AbortMultipartUpload` on
     `bronze/*`, which cannot remove a completed object, and the bucket lifecycle already
     aborts incomplete uploads after 7 days.
5. **Amounts stay text and stay CAD.** The publication defines its amounts in Canadian
   dollars. Bronze keeps them as published, and no conversion happens. Silver for this source is
   not built (see Consequences).
6. **Public reference data stays separate** (AGENTS.md §2.3), under its own `source_system`
   (`canada_open_data`). It is never joined to the synthetic tenants.

## Consequences
- **Local end-to-end run on the real file** (`--path`, `--expect-sha256`, local store),
  2026-11-12: `succeeded`, 1,336,184 records, 1,336,184 unique keys, pin matched, 13 s.
- **Landed in dev Bronze 2026-11-12** by the batch ingestion machine (ADR 0040), as batch
  `20261112T201949Z-d3199586`: 1,336,184 records, keys unique, the same SHA-256 as the local
  run. A second run skipped on the unchanged ETag and size.
- **Not yet in the catalog or Silver.** A Bronze Glue table, and a Silver model with its grain
  and amendment handling, are follow-ups. They are the same question as USAspending (open
  questions 30 and 34).
- **The file is replaced in place**, so two snapshots overlap almost entirely. Each is kept
  whole in Bronze (immutability), at about 655 MB per quarterly snapshot.

## Alternatives considered
- **The CKAN DataStore API, paged JSON, instead of the CSV:** rejected. It is not what the
  publication pins or describes, and a paged API over a file replaced in place gives no
  consistent snapshot.
- **Split the CSV into many Bronze objects:** rejected. Bronze must hold what was published,
  byte for byte. A split could be derived later from the Bronze copy.
- **`reference_number` as the key:** rejected after measuring it. It is not unique.
