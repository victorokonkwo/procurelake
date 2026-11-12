"""Task runner container entrypoint: allow-listed tasks and validated arguments (no AWS)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[3] / "jobs" / "containers" / "task_runner" / "entrypoint.py"
spec = importlib.util.spec_from_file_location("task_runner_entrypoint", ENTRY)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

BASE = {
    "PROCURELAKE_RUN_ID": "exec-1",
    "PROCURELAKE_RAW_BUCKET": "raw",
    "PROCURELAKE_ARTIFACTS_BUCKET": "art",
}


def test_usaspending_fills_gaps_since_a_validated_date():
    env = BASE | {"PROCURELAKE_TASK": "ingest-usaspending", "PROCURELAKE_USASPENDING_SINCE": "2026-10-23"}
    module, argv = entry.task_argv(env)
    assert module == "procurelake.ingestion.cli"
    assert argv[:5] == ["usaspending", "--since", "2026-10-23", "--max-days", "3"]
    assert "--role-arn" not in argv  # rights come from the task role


def test_canada_contracts_skips_an_unchanged_file():
    _module, argv = entry.task_argv(BASE | {"PROCURELAKE_TASK": "ingest-canada-contracts"})
    assert argv[:2] == ["canada-contracts", "--skip-if-unchanged"]


def test_data_quality_arguments():
    env = BASE | {
        "PROCURELAKE_TASK": "data-quality",
        "PROCURELAKE_ATHENA_WORKGROUP": "wg",
        "PROCURELAKE_SILVER_DATABASE": "silver",
    }
    module, argv = entry.task_argv(env)
    assert module == "procurelake.quality.cli"
    assert argv[argv.index("--run-id") + 1] == "exec-1"


@pytest.mark.parametrize(
    "env",
    [
        BASE | {"PROCURELAKE_TASK": "rm -rf"},
        BASE | {"PROCURELAKE_TASK": "ingest-usaspending", "PROCURELAKE_USASPENDING_SINCE": "2026-10-23; ls"},
        BASE
        | {
            "PROCURELAKE_TASK": "ingest-usaspending",
            "PROCURELAKE_USASPENDING_SINCE": "2026-10-23",
            "PROCURELAKE_USASPENDING_MAX_DAYS": "400",
        },
        BASE | {"PROCURELAKE_TASK": "data-quality", "PROCURELAKE_RUN_ID": "bad id with spaces"},
        {"PROCURELAKE_TASK": "ingest-canada-contracts", "PROCURELAKE_RUN_ID": "x"},  # no buckets
    ],
)
def test_anything_else_is_refused(env):
    with pytest.raises(ValueError):
        entry.task_argv(env)


def test_main_returns_the_cli_exit_code(monkeypatch):
    monkeypatch.setattr(entry, "task_argv", lambda _env: ("json.tool", ["--help"]))
    assert entry.main() == 0
