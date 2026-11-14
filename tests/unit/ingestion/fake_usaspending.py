"""A local fake of the two USAspending endpoints, for tests only.

Reproduces the live-API behaviour the connector depends on (verified 2026-11-10 and
2026-11-14, and against ``search/v2/views/spending_by_award.py``): ``limit`` > 100 is rejected
with 422; with page numbers, ``hasNext`` is computed from a hit total capped at 10,000 (so it
turns false from page 100) and a page starting at record 50,000 or later is rejected with 422;
with ``last_record_sort_value`` + ``last_record_unique_id`` (keyset), the page holds the records
after that cursor and ``hasNext`` is exact. Both modes report the last record's cursor in
``page_metadata`` only when ``hasNext`` is true.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass, field
from functools import cached_property

from werkzeug import Request, Response


@dataclass
class FakeUsaspending:
    record_count: int
    count_sequence: list[int] | None = None  # values returned by successive count calls
    duplicate_on_page: int | None = None  # repeat the previous page's last record here
    fail_first_n_page_requests: int = 0  # respond 503 to the first N page requests
    permanent_error_on_page: int | None = None  # respond 422 to this page
    wrong_cursor_on_page: int | None = None  # report a page_metadata cursor that is not the last record
    stall_on_page: int | None = None  # serve this keyset page from the start again (cursor ignored)
    page_requests: list[dict] = field(default_factory=list)
    count_requests: int = 0

    @cached_property
    def _records(self) -> list[dict]:
        return [
            {
                "internal_id": i,
                "Award ID": f"AWD{i:07d}",
                "generated_internal_id": f"CONT_AWD_{i:07d}",
                "Award Amount": 1000.0 + i,
                "Last Modified Date": "2026-11-08 12:00:00",
            }
            for i in range(self.record_count)
        ]

    @cached_property
    def _keys(self) -> list[tuple[str, int]]:
        return [(r["generated_internal_id"], r["internal_id"]) for r in self._records]

    def handle_count(self, request: Request) -> Response:
        self.count_requests += 1
        if self.count_sequence:
            value = self.count_sequence[min(self.count_requests - 1, len(self.count_sequence) - 1)]
        else:
            value = self.record_count
        body = {"results": {"contracts": value, "idvs": 0, "grants": 0}, "spending_level": "awards"}
        return Response(json.dumps(body), content_type="application/json")

    def handle_page(self, request: Request) -> Response:
        payload = json.loads(request.data)
        self.page_requests.append(payload)
        if len(self.page_requests) <= self.fail_first_n_page_requests:
            return Response("service unavailable", status=503)
        limit, page = payload["limit"], payload["page"]
        if limit > 100:
            return Response(
                json.dumps({"detail": f"Field 'limit' value '{limit}' is above max '100'"}), status=422
            )
        if page == self.permanent_error_on_page:
            return Response(json.dumps({"detail": "bad request"}), status=422)

        records = self._records
        sort_value = payload.get("last_record_sort_value")
        unique_id = payload.get("last_record_unique_id")
        if (sort_value is None) != (unique_id is None):
            return Response("search_after requires both values", status=500)
        if sort_value is None:
            start = (page - 1) * limit
            if start >= 50_000:
                detail = (
                    f"Page #{page} with limit {limit} is over the maximum result limit 50000. Please "
                    "provide the 'last_record_sort_value' and 'last_record_unique_id' to paginate"
                    " sequentially."
                )
                return Response(json.dumps({"detail": detail}), status=422)
            has_next = min(len(records), 10_000) - start > limit  # capped hit total
        else:
            key = (sort_value, unique_id)
            start = bisect.bisect_right(self._keys, key)
            if self.stall_on_page == page:
                start = 0
            has_next = len(records) - start > limit  # limit + 1 fetched: exact
        results = records[start : start + limit]
        if self.duplicate_on_page == page and start > 0:
            results = [records[start - 1], *results[1:]]
        metadata = {
            "page": page,
            "hasNext": has_next,
            "last_record_unique_id": None,
            "last_record_sort_value": "None",  # the API sends str(None)
        }
        if has_next and results:
            last = results[-1]
            if self.wrong_cursor_on_page == page:
                last = records[start]
            metadata["last_record_unique_id"] = last["internal_id"]
            metadata["last_record_sort_value"] = last["generated_internal_id"]
        body = {
            "spending_level": "awards",
            "limit": limit,
            "results": results,
            "page_metadata": metadata,
            "messages": [],
        }
        return Response(json.dumps(body), content_type="application/json")

    def install(self, httpserver) -> None:
        httpserver.expect_request(
            "/api/v2/search/spending_by_award_count/", method="POST"
        ).respond_with_handler(self.handle_count)
        httpserver.expect_request("/api/v2/search/spending_by_award/", method="POST").respond_with_handler(
            self.handle_page
        )
