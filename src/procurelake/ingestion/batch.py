"""Batch identity helpers shared by every connector."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version


def new_batch_id(now: datetime) -> str:
    """Sortable, unique batch ID: UTC start time plus 8 random hex characters."""
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def code_version() -> str:
    """Package version, plus the git SHA when PROCURELAKE_GIT_SHA is set (CI and make targets set it)."""
    try:
        package_version = version("procurelake")
    except PackageNotFoundError:
        package_version = "unknown"
    git_sha = os.environ.get("PROCURELAKE_GIT_SHA", "")
    return f"{package_version}+{git_sha}" if git_sha else package_version
