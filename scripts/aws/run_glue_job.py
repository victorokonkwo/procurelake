"""Start a Glue job run, wait for it, and print the result as JSON (exit 1 unless SUCCEEDED).

Usage: uv run python scripts/aws/run_glue_job.py JOB_NAME [--arg=--batch-id=...] [--region R]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.glue_runs import start_and_wait


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_name")
    parser.add_argument("--arg", action="append", default=[], help="Job argument as --name=value")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args(argv)
    arguments = dict(a.split("=", 1) for a in args.arg)
    glue = build_session(region=args.region).client("glue")
    result = start_and_wait(glue, args.job_name, arguments, timeout_seconds=args.timeout_seconds)
    print(json.dumps({**asdict(result), "succeeded": result.succeeded}, indent=2))
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
