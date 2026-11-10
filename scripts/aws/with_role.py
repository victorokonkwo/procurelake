"""Run a command with temporary credentials for an IAM role.

Usage: uv run python scripts/aws/with_role.py ROLE_ARN -- COMMAND [ARGS...]

The base identity comes from the standard AWS chain. The command gets AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN for the assumed role (1 hour), so tools such as
dbt act as e.g. procurelake-dev-data-engineer rather than with the caller's own rights.
"""

from __future__ import annotations

import os
import sys

from procurelake.common.aws_clients import DEFAULT_REGION, build_session


def main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[1] != "--":
        raise SystemExit(__doc__.splitlines()[2])
    role_arn, command = argv[0], argv[2:]
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    creds = build_session(region=region, role_arn=role_arn, session_name="procurelake-cli").get_credentials()
    env = {
        k: v for k, v in os.environ.items() if k not in {"AWS_PROFILE", "AWS_ACCESS_KEY", "AWS_SECRET_KEY"}
    }
    env.update(
        AWS_ACCESS_KEY_ID=creds.access_key,
        AWS_SECRET_ACCESS_KEY=creds.secret_key,
        AWS_SESSION_TOKEN=creds.token,
        AWS_REGION=region,
        AWS_DEFAULT_REGION=region,
    )
    os.execvpe(command[0], command, env)  # noqa: S606 - runs the caller's own command


if __name__ == "__main__":
    main(sys.argv[1:])
