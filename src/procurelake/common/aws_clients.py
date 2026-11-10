"""AWS session construction.

Credentials come from the standard boto3 chain (environment variables, SSO/profile, instance
role). When ``role_arn`` is given, the base identity assumes that role, which is how the
ingestion job acts as ``procurelake-<env>-ingestion`` rather than with the caller's own rights.
"""

from __future__ import annotations

import boto3

DEFAULT_REGION = "ca-central-1"


def build_session(
    region: str = DEFAULT_REGION,
    role_arn: str | None = None,
    session_name: str = "procurelake",
    profile: str | None = None,
) -> boto3.Session:
    base = boto3.Session(profile_name=profile, region_name=region)
    if role_arn is None:
        return base

    credentials = base.client("sts").assume_role(RoleArn=role_arn, RoleSessionName=session_name)[
        "Credentials"
    ]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )
