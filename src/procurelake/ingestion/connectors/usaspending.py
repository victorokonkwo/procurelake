"""USAspending.gov award search connector (public US federal awards).

Endpoint contracts (fedspendingtransparency/usaspending-api, ``api_contracts``):
  POST /api/v2/search/spending_by_award/        paged results (``limit`` <= 100, ``page``, or
                                                ``last_record_sort_value`` + ``last_record_unique_id``)
  POST /api/v2/search/spending_by_award_count/  counts per award category for the same filters

API behaviour verified against the live API on 2026-11-10 that shapes this design:
* ``limit`` above 100 returns HTTP 422.
* With ``page`` numbers, ``page_metadata.hasNext`` is ``false`` from page 100 onward even when
  more pages hold data (a one-day window with 11,429 contracts returned full pages through page
  114): the API computes it from Elasticsearch's hit total (``construct_es_response`` in
  ``search/v2/views/spending_by_award.py``), which stops at 10,000 here.
* Page numbers stop at 50,000 records: page 501 with limit 100 returns HTTP 422 ("over the
  maximum result limit 50000. Please provide the 'last_record_sort_value' and
  'last_record_unique_id' to paginate sequentially"; 2026-10-29 had 121,674 contracts).
* Keyset paging (Elasticsearch ``search_after``): the request carries the last record's sort
  value and unique id, and the API sorts on ``[sort field, award_id]``, so the order is total and
  pages don't overlap. In this mode the API fetches ``limit + 1`` records, so ``hasNext`` is
  exact. Each record carries both values: ``generated_internal_id`` (the sort value here) and
  ``internal_id`` (the award id). Verified live on 2026-11-14: keyset page 2 returned the same
  100 records as page number 2.
* So page 1 is requested by number and every later page by keyset, with the cursor taken from
  the previous page's last record (and checked against ``page_metadata`` when it gives one).
  Paging ends at a short page or when a keyset page reports ``hasNext`` false.
* Recent days are incomplete: on 2026-11-10 the per-day contract counts were 9,550 (11-04),
  136 (11-05), 103 (11-06), then 0 for 11-07 to 11-09, versus thousands for older days. A day is
  therefore only collected once it is ``settle_days`` old (default 10). Batches forced earlier
  with ``allow_unsettled`` are recorded as ``window_settled=False`` and do not complete a window.
* The API blocks a client that sends too many requests (observed 2026-11-13/14). After about
  250 requests at 2 to 4 per second, from Fargate and from a workstation alike, it closes every
  connection without a response, for about 5 minutes; then it serves again. No rate-limit
  header is sent. So requests are paced (``REQUEST_INTERVAL_SECONDS``, at most 240 per 5
  minutes) and a refused request waits long enough to outlast a block (``RETRY_POLICY``).

Each batch covers one complete UTC day of ``last_modified_date``. It is reconciled against the
count endpoint (queried before and after paging) and against the number of unique
``generated_internal_id`` values. Any disagreement marks the batch ``failed``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from procurelake.common.retry import RetryPolicy
from procurelake.ingestion.batch import code_version, new_batch_id
from procurelake.ingestion.http import HttpClient, HttpError
from procurelake.ingestion.landing import LandingStore, ObjectAlreadyExistsError
from procurelake.ingestion.manifest import (
    CollectionWindow,
    IngestionManifest,
    LandedFile,
    Reconciliation,
    load_manifests,
    succeeded_windows,
    write_manifest,
)

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "usaspending"
DATASET = "spending_by_award"
# Bump when FIELDS or filters change shape: Silver parses by schema_version.
SCHEMA_VERSION = "usaspending.spending_by_award.contracts.v1"
MAX_PAGE_SIZE = 100
EARLIEST_SEARCH_DATE = date(2007, 10, 1)  # per the API's own response message

CONTRACT_AWARD_TYPES = ("A", "B", "C", "D")
FIELDS = (
    "Award ID",
    "generated_internal_id",
    "Recipient Name",
    "Recipient UEI",
    "Award Amount",
    "Total Outlays",
    "Awarding Agency",
    "Awarding Agency Code",
    "Awarding Sub Agency",
    "Awarding Sub Agency Code",
    "Funding Agency",
    "Funding Sub Agency",
    "Contract Award Type",
    "Start Date",
    "End Date",
    "Base Obligation Date",
    "Last Modified Date",
    "NAICS",
    "PSC",
    "Description",
    "Place of Performance State Code",
    "Place of Performance Country Code",
)


# Pacing and retries for the public API (see the module docstring): one request per 1.25 s is
# 240 per 5 minutes, under the ~250 at which the API blocked us; retries wait 1 to 5 minutes
# (8 attempts: at least 7 minutes of waiting in all), longer than the ~5 minute block.
REQUEST_INTERVAL_SECONDS = 1.25
RETRY_POLICY = RetryPolicy(
    max_attempts=8, base_delay_seconds=60.0, max_delay_seconds=300.0, min_delay_seconds=60.0
)


def usaspending_http_client() -> HttpClient:
    """The HTTP client the connector uses against the live API."""
    return HttpClient(retry_policy=RETRY_POLICY, min_interval_seconds=REQUEST_INTERVAL_SECONDS)


@dataclass
class UsaspendingConfig:
    base_url: str = "https://api.usaspending.gov"
    award_type_codes: tuple[str, ...] = CONTRACT_AWARD_TYPES
    fields: tuple[str, ...] = FIELDS
    page_size: int = MAX_PAGE_SIZE
    # A guard against a runaway loop, not a source limit: 5,000 pages of 100 is 500,000 records,
    # about 4 times the largest day seen (121,674), and about 1 h 45 min at the paced rate.
    max_pages: int = 5_000
    # Days a window must age before its data is treated as complete (see module docstring).
    settle_days: int = 10

    def __post_init__(self) -> None:
        if self.settle_days < 0:
            raise ValueError("settle_days must be >= 0")
        if not 1 <= self.page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if "generated_internal_id" not in self.fields:
            raise ValueError("fields must include generated_internal_id (used for dedup and ordering)")


@dataclass
class _BatchState:
    files: list[LandedFile] = field(default_factory=list)
    landed_records: int = 0
    ids: set[str] = field(default_factory=set)


class UsaspendingConnector:
    def __init__(
        self,
        http: HttpClient,
        raw_store: LandingStore,
        manifest_store: LandingStore,
        config: UsaspendingConfig | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.http = http
        self.raw_store = raw_store
        self.manifest_store = manifest_store
        self.config = config or UsaspendingConfig()
        self.clock = clock

    # ------------------------------------------------------------------ request building
    def _filters(self, day: date) -> dict:
        return {
            "award_type_codes": list(self.config.award_type_codes),
            "time_period": [
                {
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                    "date_type": "last_modified_date",
                }
            ],
        }

    def _page_payload(self, day: date, page: int, cursor: tuple[str, int] | None = None) -> dict:
        payload = {
            "filters": self._filters(day),
            "fields": list(self.config.fields),
            "limit": self.config.page_size,
            "page": page,
            "sort": "generated_internal_id",
            "order": "asc",
            "subawards": False,
        }
        if cursor is not None:
            # Keyset paging: the API ignores ``page`` when both values are given.
            payload["last_record_sort_value"], payload["last_record_unique_id"] = cursor
        return payload

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    def expected_count(self, day: date) -> int:
        response = self.http.post_json(
            self._url("/api/v2/search/spending_by_award_count/"), {"filters": self._filters(day)}
        )
        results = response.json().get("results")
        if not isinstance(results, dict):
            raise HttpError("count endpoint returned no 'results' object")
        return sum(int(v) for v in results.values())

    # ------------------------------------------------------------------ ingestion
    def is_settled(self, day: date, as_of: date) -> bool:
        return (as_of - day).days >= self.config.settle_days

    def ingest_day(
        self, day: date, allow_unsettled: bool = False, purpose: str = "daily"
    ) -> IngestionManifest:
        """Collect one day. ``purpose`` (``daily`` or ``resweep``) is recorded in the manifest request."""
        started = self.clock().replace(microsecond=0)
        if day >= started.date():
            raise ValueError(f"{day} is not a complete UTC day yet; ingest only past days")
        settled = self.is_settled(day, started.date())
        if not settled and not allow_unsettled:
            raise ValueError(
                f"{day} is within the {self.config.settle_days}-day settling period; source data for it "
                "is still arriving. Use allow_unsettled to collect it anyway (recorded as unsettled)."
            )
        if day < EARLIEST_SEARCH_DATE:
            raise ValueError(f"USAspending search does not support dates before {EARLIEST_SEARCH_DATE}")

        batch_id = new_batch_id(started)
        prefix = (
            f"bronze/{SOURCE_SYSTEM}/{DATASET}/"
            f"ingestion_date={started.date().isoformat()}/batch_id={batch_id}"
        )
        state = _BatchState()
        expected_before = expected_after = None
        error: str | None = None
        detail = ""

        try:
            expected_before = self.expected_count(day)
            self._land_pages(day, prefix, state)
            expected_after = self.expected_count(day)
        except (HttpError, ObjectAlreadyExistsError, ValueError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"

        passed = error is None and expected_before == expected_after == state.landed_records == len(state.ids)
        if error:
            detail = "run aborted before reconciliation completed"
        elif expected_before != expected_after:
            detail = "source count changed during collection; window is not stable"
        elif state.landed_records != len(state.ids):
            detail = "duplicate records across pages"
        elif state.landed_records != expected_before:
            detail = "landed record count does not match the source count endpoint"
        else:
            detail = "landed records match source count before and after collection; all ids unique"

        manifest = IngestionManifest(
            source_system=SOURCE_SYSTEM,
            dataset=DATASET,
            schema_version=SCHEMA_VERSION,
            batch_id=batch_id,
            status="succeeded" if passed else "failed",
            ingestion_started_at=started,
            ingestion_completed_at=self.clock().replace(microsecond=0),
            window=CollectionWindow(date_type="last_modified_date", start_date=day, end_date=day),
            window_settled=settled,
            request={
                "purpose": purpose,
                "endpoint": "/api/v2/search/spending_by_award/",
                "count_endpoint": "/api/v2/search/spending_by_award_count/",
                "base_url": self.config.base_url,
                "filters": self._filters(day),
                "fields": list(self.config.fields),
                "limit": self.config.page_size,
                "sort": "generated_internal_id",
                "order": "asc",
                "pagination": PAGINATION,
            },
            files=state.files,
            reconciliation=Reconciliation(
                expected_count_before=expected_before,
                expected_count_after=expected_after,
                landed_record_count=state.landed_records,
                unique_record_count=len(state.ids),
                passed=passed,
                detail=detail,
            ),
            code_version=code_version(),
            error=error,
        )
        key = write_manifest(self.manifest_store, manifest)
        logger.info("batch %s %s: %s (%s)", batch_id, manifest.status, key, detail)
        return manifest

    def _land_pages(self, day: date, prefix: str, state: _BatchState) -> None:
        url = self._url("/api/v2/search/spending_by_award/")
        cursor: tuple[str, int] | None = None
        for page in range(1, self.config.max_pages + 1):
            response = self.http.post_json(url, self._page_payload(day, page, cursor))
            body = response.json()
            results = body.get("results") if isinstance(body, dict) else None
            if not isinstance(results, list):
                raise ValueError(f"page {page}: response has no 'results' list")

            stored = self.raw_store.put_new(
                f"{prefix}/page-{page:05d}.json", response.body, "application/json"
            )
            state.files.append(
                LandedFile(
                    key=stored.key,
                    uri=stored.uri,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256_hex,
                    record_count=len(results),
                    page=page,
                )
            )
            state.landed_records += len(results)
            for record in results:
                internal_id = record.get("generated_internal_id")
                if not internal_id:
                    raise ValueError(f"page {page}: record without generated_internal_id")
                state.ids.add(internal_id)

            # A short page ends paging. hasNext is trusted only on keyset pages, where it is exact
            # (see module docstring); on page 1 it is ignored.
            if len(results) < self.config.page_size:
                return
            metadata = body.get("page_metadata") if isinstance(body.get("page_metadata"), dict) else {}
            if cursor is not None and metadata.get("hasNext") is False:
                return
            cursor = _next_cursor(page, results, metadata, cursor)
        raise ValueError(f"exceeded max_pages={self.config.max_pages} without reaching the last page")


# Recorded in each manifest's request: how pages after the first were requested.
PAGINATION = "page 1 by number; later pages by keyset (last_record_sort_value, last_record_unique_id)"


def _next_cursor(
    page: int, results: list[dict], metadata: dict, previous: tuple[str, int] | None
) -> tuple[str, int]:
    """The keyset cursor after a full page: its last record's sort value and award id.

    Refuses a cursor that the API's own ``page_metadata`` contradicts, or that does not move
    forward, so a misbehaving source fails the batch instead of looping or skipping records.
    """
    last = results[-1]
    sort_value, unique_id = last.get("generated_internal_id"), last.get("internal_id")
    if not isinstance(unique_id, int) or isinstance(unique_id, bool):
        raise ValueError(f"page {page}: last record has no integer internal_id for keyset paging")
    reported = (metadata.get("last_record_sort_value"), metadata.get("last_record_unique_id"))
    if metadata.get("hasNext") is True and reported[1] is not None and reported != (sort_value, unique_id):
        raise ValueError(
            f"page {page}: page_metadata cursor {reported} differs from the last record "
            f"{(sort_value, unique_id)}"
        )
    if previous is not None and sort_value <= previous[0]:
        raise ValueError(f"page {page}: keyset cursor did not advance past {previous[0]!r}")
    return sort_value, unique_id


def pending_days(
    manifests: Iterable[IngestionManifest], since: date, through: date, max_days: int | None = None
) -> list[date]:
    """Days in [since, through] without a succeeded batch, oldest first (gap-filling watermark)."""
    if since > through:
        return []
    done = succeeded_windows(list(manifests))
    days = [since + timedelta(days=i) for i in range((through - since).days + 1)]
    missing = [d for d in days if d not in done]
    return missing[:max_days] if max_days is not None else missing


def resweep_days(as_of: date, settle_days: int, days: int) -> list[date]:
    """The ``days`` most recent settled days, oldest first (open question 30, ADR 0044).

    A re-sweep collects them again even though they already have a succeeded batch: records
    added to a day after it was collected, or changed without a new modification date, appear
    only this way. Silver's MERGE absorbs repeats, and reports what each re-sweep found.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    last = as_of - timedelta(days=settle_days)
    return [last - timedelta(days=i) for i in range(days - 1, -1, -1)]


def plan_incremental(
    manifest_store: LandingStore, since: date, through: date, max_days: int | None = None
) -> list[date]:
    return pending_days(load_manifests(manifest_store, SOURCE_SYSTEM, DATASET), since, through, max_days)
