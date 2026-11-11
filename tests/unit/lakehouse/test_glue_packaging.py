"""The Silver Glue job's packaging: bundled rules, pinned modules, and runtime imports."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from procurelake.lakehouse.silver.job import load_silver_specs, parse_args, resource_root

REPO = Path(__file__).resolve().parents[3]
MODULES_FILE = REPO / "jobs" / "glue" / "silver_python_modules.txt"


def _pins() -> dict[str, str]:
    pins = {}
    for line in MODULES_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.split("==")
            pins[name] = version
    return pins


def _locked() -> dict[str, dict]:
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    return {p["name"]: p for p in lock["package"]}


def test_pins_match_uv_lock():
    locked = _locked()
    assert {name: locked[name]["version"] for name in _pins()} == _pins()


def test_pins_cover_the_dependency_closure_of_pydantic_and_pyyaml():
    locked = _locked()
    closure, todo = set(), ["pydantic", "pyyaml"]
    while todo:
        name = todo.pop()
        if name not in closure:
            closure.add(name)
            todo.extend(d["name"] for d in locked[name].get("dependencies", []))
    assert closure == set(_pins())


def test_job_imports_without_psycopg_or_requests():
    # The Glue job installs the wheel with --no-deps: it must not need the ingestion-only deps.
    code = (
        "import sys; sys.modules['psycopg'] = None; sys.modules['requests'] = None\n"
        "import procurelake.lakehouse.silver.job as j; j.load_silver_specs()"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=REPO)  # noqa: S603 - fixed code string


def test_specs_load_from_the_repository_in_a_checkout():
    assert resource_root() == REPO
    specs = load_silver_specs()
    assert set(specs) == {
        "tenants",
        "agencies",
        "suppliers",
        "solicitations",
        "awards",
        "purchase_orders",
        "invoices",
    }


def test_wheel_bundles_every_contract_and_silver_config(tmp_path):
    uv = shutil.which("uv")
    assert uv, "uv must be on PATH to build the wheel"
    subprocess.run(  # noqa: S603 - fixed arguments
        [uv, "build", "--wheel", "--out-dir", str(tmp_path)], check=True, cwd=REPO, capture_output=True
    )
    [wheel] = tmp_path.glob("procurelake-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    for sub in ("contracts/source", "configs/pipelines/silver"):
        for path in (REPO / sub).glob("*.yaml"):
            assert f"procurelake/_bundled/{sub}/{path.name}" in names


def test_parse_args_ignores_glue_arguments():
    args = parse_args(
        [
            "--JOB_ID",
            "j_1",
            "--JOB_RUN_ID",
            "jr_abc",
            "--JOB_NAME",
            "procurelake-dev-bronze-to-silver",
            "--artifacts-bucket",
            "art",
            "--silver-database",
            "procurelake_dev_silver",
            "--silver-location",
            "s3://lake/silver",
            "--quarantine-root",
            "s3://raw/quarantine",
        ]
    )
    assert (args.job_run_id, args.catalog, args.batch_id) == ("jr_abc", "glue_catalog", "")
    assert args.source_system == "postgres"  # the dev job; only the bench job passes postgres_bench


def test_pyproject_force_includes_bundled_dirs():
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    include = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert include == {
        "contracts/source": "procurelake/_bundled/contracts/source",
        "configs/pipelines/silver": "procurelake/_bundled/configs/pipelines/silver",
    }


@pytest.mark.parametrize("bad", ["glue-catalog", "x; DROP"])
def test_run_batch_rejects_bad_names(bad):
    from procurelake.lakehouse.silver.job import run_batch

    with pytest.raises(ValueError):
        run_batch(None, {}, None, catalog=bad, database="db", quarantine_root="q")
    with pytest.raises(ValueError):
        run_batch(None, {}, None, catalog="c", database="db", quarantine_root="q", source_system=bad)


@pytest.mark.parametrize("entrypoint", sorted(p.name for p in (REPO / "jobs" / "glue").glob("*.py")))
def test_glue_entrypoint_never_calls_sys_exit(entrypoint):
    # Glue reports any SystemExit, even SystemExit(0), as a FAILED run (first dev run, 2026-11-10).
    tree = ast.parse((REPO / "jobs" / "glue" / entrypoint).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "exit"]
    assert calls == []
