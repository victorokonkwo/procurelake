# ADR 0046: USAspending keyset paging after page 1

- **Status:** Accepted (2026-11-14). Run live against the API on 2026-11-14 (local store, see
  Consequences); not yet deployed to the task runner.
- **Date:** 2026-11-14
- **Implementation:** `src/procurelake/ingestion/connectors/usaspending.py` (`_land_pages`,
  `_next_cursor`, `PAGINATION`); the test fake `tests/unit/ingestion/fake_usaspending.py` and
  tests in `tests/unit/ingestion/test_usaspending.py`

## Context
ADR 0020 collected each USAspending day with page numbers, `limit` 100, until a short page. On
2026-11-14 the days over 50,000 records failed (ADR 0044): the API refused page 501 with HTTP
422, "Page #501 with limit 100 is over the maximum result limit 50000. Please provide the
'last_record_sort_value' and 'last_record_unique_id' to paginate sequentially." 2026-10-29 has
121,674 contracts and 2026-10-30 has 75,201.

The API's published contract (`api_contracts/contracts/v2/search/spending_by_award.md`) lists
both request fields and the same two fields in `page_metadata`. Its source
(`usaspending_api/search/v2/views/spending_by_award.py`, read 2026-11-14) shows how they work:
- Both values, or neither, must be sent. With both, the query is Elasticsearch `search_after`
  on the sort `[sort field, award_id]`, and `page` is ignored.
- In that mode the API fetches `limit + 1` records, so `hasNext` is exact.
- With page numbers, `hasNext` is computed from the hit total. ADR 0020 observed it turning
  false from page 100, consistent with a total that stops at 10,000.
- `page_metadata` carries the last returned record's cursor only when `hasNext` is true.

Each record carries both cursor values: `generated_internal_id` (the sort field here) and
`internal_id` (the award id). A live check on 2026-11-14 against 2026-10-29: page 1 reported
the cursor `CONT_AWD_12024B26M0846_12C2_12024B26T7815_12C2` / `361903792`, equal to its last
record, and a keyset request with it returned the same 100 records as page number 2.

## Decision
1. **Page 1 by number, every later page by keyset**, for every day, not only past 50,000
   records. One code path, an exact `hasNext`, and no deep-offset queries. Page 1 is unchanged.
2. **The cursor comes from the previous page's last record** (`generated_internal_id`,
   `internal_id`), not from `page_metadata`, because page-number mode omits it once `hasNext`
   turns false. When `page_metadata` does report a cursor, it must equal the last record's, or
   the batch fails.
3. **The cursor must move forward.** A page whose last sort value is not greater than the
   previous cursor fails the batch instead of looping. `generated_internal_id` is unique, so the
   order is strict.
4. **Paging ends at a short page, or at a keyset page with `hasNext` false** (exact there). On
   page 1 `hasNext` is still ignored.
5. **`max_pages` 1,000 to 5,000.** It guards against a runaway loop and is not a source limit:
   500,000 records, about 4 times the largest day seen, and about 1 h 45 min at the paced rate
   (ADR 0044).
6. **Nothing downstream changes.** Pages keep the same JSON shape, file names and manifest
   entries. `schema_version` is unchanged. The manifest's `request` gains `pagination`.
   Reconciliation is unchanged: count before and after, landed and unique ids.

## Alternatives considered
- **Keyset only past page 500.** Two code paths for the same day, and the page-number
  `hasNext` quirk stays in use.
- **Split a large day into smaller windows** (for example by award type). The time filter takes
  dates, not times, so a day cannot be split by time, and other splits need their own
  reconciliation.
- **Take the cursor from `page_metadata` only.** It is missing when page-number `hasNext` is
  false, and trusting it alone would hide a disagreement.

## Consequences
- **Live run, 2026-11-14** (`make ingest-usaspending-local DATE=2026-10-29`, from a
  workstation into `./local_lake`, batch `20261114T203326Z-8d867334`): `succeeded`, 1,217
  pages, 121,674 records landed, 121,674 unique, the count endpoint 121,674 before and after.
  20:33:26Z to 20:59:06Z, 25 min 40 s. Pages 501 to 1,217 are past the cap that failed before.
- **Dev Silver still lacks 2026-10-29 and 2026-10-30** until the task runner image is rebuilt
  and deployed; the next daily batch ingestion run then collects them (they have no succeeded
  batch).
- A large day takes long: about 1.25 s per page (ADR 0044 pacing), so 121,674 records are about
  1,220 requests, about 26 minutes. Neither the batch ingestion machine nor its task sets a timeout.
- The test fake now models both modes: the 50,000-record cap with HTTP 422, the capped total
  for page-number `hasNext`, exact keyset `hasNext`, and the cursor in `page_metadata` only
  while `hasNext` is true.
