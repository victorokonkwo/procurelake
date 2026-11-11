"""Run an Athena query to completion and collect its results and statistics."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class AthenaQueryError(RuntimeError):
    def __init__(self, message: str, query_execution_id: str | None = None):
        super().__init__(message)
        self.query_execution_id = query_execution_id


@dataclass(frozen=True)
class QueryResult:
    query_execution_id: str
    columns: list[str]
    rows: list[tuple[str | None, ...]]
    data_scanned_bytes: int
    engine_execution_ms: int
    output_location: str
    # Athena's full ``Statistics`` block (planning/queue/total times, result reuse) for benchmarks.
    statistics: dict = field(default_factory=dict)


def run_query(
    athena,
    sql: str,
    workgroup: str,
    database: str,
    timeout_seconds: float = 300.0,
    poll_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> QueryResult:
    """Start ``sql`` in ``workgroup``, wait for a terminal state, and return every result row.

    Raises ``AthenaQueryError`` if the query fails, is cancelled, or exceeds ``timeout_seconds``
    (the query is then stopped).
    """
    query_id = athena.start_query_execution(
        QueryString=sql,
        WorkGroup=workgroup,
        QueryExecutionContext={"Database": database},
    )["QueryExecutionId"]

    deadline = time.monotonic() + timeout_seconds
    while True:
        execution = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in TERMINAL_STATES:
            break
        if time.monotonic() > deadline:
            athena.stop_query_execution(QueryExecutionId=query_id)
            raise AthenaQueryError(f"query exceeded {timeout_seconds}s and was stopped", query_id)
        sleep(poll_seconds)

    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "no reason given")
        raise AthenaQueryError(f"query {state}: {reason}", query_id)

    columns: list[str] = []
    rows: list[tuple[str | None, ...]] = []
    paginator = athena.get_paginator("get_query_results")
    for page_index, page in enumerate(paginator.paginate(QueryExecutionId=query_id)):
        if page_index == 0:
            columns = [c["Name"] for c in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
        page_rows = page["ResultSet"]["Rows"]
        if page_index == 0 and page_rows:
            page_rows = page_rows[1:]  # first row of a SELECT result is the header
        rows.extend(tuple(cell.get("VarCharValue") for cell in r["Data"]) for r in page_rows)

    stats = execution.get("Statistics", {})
    return QueryResult(
        query_execution_id=query_id,
        columns=columns,
        rows=rows,
        data_scanned_bytes=int(stats.get("DataScannedInBytes", 0)),
        engine_execution_ms=int(stats.get("EngineExecutionTimeInMillis", 0)),
        output_location=execution["ResultConfiguration"]["OutputLocation"],
        statistics=dict(stats),
    )
