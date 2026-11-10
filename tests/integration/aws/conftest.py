"""Fixtures for integration tests against the deployed dev environment.

These tests run real AWS calls (Athena queries scan a few MB; cost is negligible). They are
opt-in: set PROCURELAKE_AWS_TESTS=1 and the PROCURELAKE_DEV_* variables. `make test-aws` does
both from Terraform outputs. Each role-specific client is created by assuming that role.

Evidence (query IDs, bytes scanned, counts) is written to build/evidence/aws_integration.json.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from procurelake.common.aws_clients import build_session

REQUIRED = [
    "PROCURELAKE_DEV_RAW_BUCKET",
    "PROCURELAKE_DEV_ARTIFACTS_BUCKET",
    "PROCURELAKE_DEV_DATA_KMS_KEY_ARN",
    "PROCURELAKE_DEV_INGESTION_ROLE_ARN",
    "PROCURELAKE_DEV_DATA_ENGINEER_ROLE_ARN",
    "PROCURELAKE_DEV_ATHENA_WORKGROUP",
    "PROCURELAKE_DEV_BRONZE_DATABASE",
    "PROCURELAKE_DEV_SILVER_DATABASE",
    "PROCURELAKE_DEV_GOLD_DATABASE",
]
EVIDENCE_PATH = Path(__file__).resolve().parents[3] / "build" / "evidence" / "aws_integration.json"


def pytest_collection_modifyitems(config, items):
    if os.environ.get("PROCURELAKE_AWS_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="AWS integration tests are opt-in: run `make test-aws`")
    for item in items:
        if "aws" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def dev_env() -> dict[str, str]:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        pytest.fail(f"missing environment variables: {', '.join(missing)} (use `make test-aws`)")
    return {k: os.environ[k] for k in REQUIRED}


@pytest.fixture(scope="session")
def engineer_session(dev_env):
    return build_session(
        role_arn=dev_env["PROCURELAKE_DEV_DATA_ENGINEER_ROLE_ARN"], session_name="it-engineer"
    )


@pytest.fixture(scope="session")
def ingestion_session(dev_env):
    return build_session(role_arn=dev_env["PROCURELAKE_DEV_INGESTION_ROLE_ARN"], session_name="it-ingestion")


@pytest.fixture(scope="session")
def evidence():
    record: dict = {"run_started_at": datetime.now(UTC).isoformat(timespec="seconds"), "checks": []}
    yield record
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(record, indent=2, default=str) + "\n")
