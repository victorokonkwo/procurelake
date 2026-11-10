"""Start a Step Functions execution and wait for its final status."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}


@dataclass(frozen=True)
class ExecutionResult:
    execution_arn: str
    status: str
    started: datetime
    stopped: datetime | None
    output: dict | None
    error: str | None
    cause: str | None

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"


def start_and_wait(
    sfn,
    state_machine_arn: str,
    execution_input: dict,
    name: str | None = None,
    poll_seconds: float = 10.0,
    timeout_seconds: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ExecutionResult:
    kwargs = {"stateMachineArn": state_machine_arn, "input": json.dumps(execution_input)}
    if name:
        kwargs["name"] = name
    arn = sfn.start_execution(**kwargs)["executionArn"]
    deadline = clock() + timeout_seconds
    while True:
        d = sfn.describe_execution(executionArn=arn)
        if d["status"] in TERMINAL:
            return ExecutionResult(
                arn,
                d["status"],
                d["startDate"],
                d.get("stopDate"),
                json.loads(d["output"]) if d.get("output") else None,
                d.get("error"),
                d.get("cause"),
            )
        if clock() >= deadline:
            raise TimeoutError(f"{arn} still {d['status']} after {timeout_seconds}s")
        sleep(poll_seconds)
