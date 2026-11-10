"""Start an AWS Glue job run and wait for its final state."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "STOPPED", "TIMEOUT", "ERROR", "EXPIRED"}


@dataclass(frozen=True)
class GlueRunResult:
    job_name: str
    run_id: str
    state: str
    execution_time_seconds: int | None
    dpu_seconds: float | None
    error_message: str | None
    completed_on: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.state == "SUCCEEDED"


def start_and_wait(
    glue,
    job_name: str,
    arguments: dict[str, str] | None = None,
    poll_seconds: float = 15.0,
    timeout_seconds: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> GlueRunResult:
    """Start ``job_name`` and poll ``GetJobRun`` until the run reaches a terminal state."""
    kwargs = {"JobName": job_name}
    if arguments:
        kwargs["Arguments"] = arguments
    run_id = glue.start_job_run(**kwargs)["JobRunId"]
    deadline = clock() + timeout_seconds
    while True:
        run = glue.get_job_run(JobName=job_name, RunId=run_id, PredecessorsIncluded=False)["JobRun"]
        state = run["JobRunState"]
        if state in TERMINAL_STATES:
            return GlueRunResult(
                job_name=job_name,
                run_id=run_id,
                state=state,
                execution_time_seconds=run.get("ExecutionTime"),
                dpu_seconds=run.get("DPUSeconds"),
                error_message=run.get("ErrorMessage"),
                completed_on=run.get("CompletedOn"),
            )
        if clock() >= deadline:
            raise TimeoutError(f"{job_name} run {run_id} still {state} after {timeout_seconds}s")
        sleep(poll_seconds)
