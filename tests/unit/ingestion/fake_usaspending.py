"""A local fake of the two USAspending endpoints, for tests only.

Reproduces the live-API behaviour the connector depends on (verified 2026-11-10):
``limit`` > 100 is rejected with 422, and ``page_metadata.hasNext`` turns false from page 100
onward even when later pages still contain records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from werkzeug import Request, Response


@dataclass
class FakeUsaspending:
    record_count: int
    count_sequence: list[int] | None = None  # values returned by successive count calls
    duplicate_on_page: int | None = None  # repeat the previous page's last record here
    fail_first_n_page_requests: int = 0  # respond 503 to the first N page requests
    permanent_error_on_page: int | None = None  # respond 422 to this page
    page_requests: list[dict] = field(default_factory=list)
    count_requests: int = 0

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

        records = self._records()
        start = (page - 1) * limit
        results = records[start : start + limit]
        if self.duplicate_on_page == page and start > 0:
            results = [records[start - 1], *results[1:]]
        body = {
            "spending_level": "awards",
            "limit": limit,
            "results": results,
            # Live quirk: hasNext is false from page 100 regardless of remaining data.
            "page_metadata": {"page": page, "hasNext": page < 100 and start + limit < len(records)},
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
