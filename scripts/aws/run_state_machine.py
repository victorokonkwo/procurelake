"""Start a Step Functions execution, wait, print the result (exit 1 unless SUCCEEDED).

Usage: uv run python scripts/aws/run_state_machine.py STATE_MACHINE_ARN --input '{"k": "v"}' [--name N]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from procurelake.common.aws_clients import DEFAULT_REGION, build_session
from procurelake.common.sfn_runs import start_and_wait


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("state_machine_arn")
    parser.add_argument("--input", default="{}")
    parser.add_argument("--name", default=None)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)
    sfn = build_session(region=args.region).client("stepfunctions")
    result = start_and_wait(sfn, args.state_machine_arn, json.loads(args.input), name=args.name)
    print(json.dumps({**asdict(result), "succeeded": result.succeeded}, indent=2, default=str))
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
