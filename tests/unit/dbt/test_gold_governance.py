"""Gold governance rules (AGENTS.md section 2.4), checked on the dbt project files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
import yaml

from procurelake.contracts.source import SourceContract

DBT = Path(__file__).resolve().parents[3] / "dbt"
GOLD_DIRS = [DBT / "models" / "core", DBT / "models" / "marts"]
CLASSIFICATIONS = set(get_args(SourceContract.model_fields["data_classification"].annotation))


def _gold_sql() -> dict[str, Path]:
    return {p.stem: p for d in GOLD_DIRS for p in d.rglob("*.sql")}


def _documented() -> dict[str, dict]:
    docs = {}
    for d in GOLD_DIRS:
        for path in d.rglob("*.yml"):
            for model in yaml.safe_load(path.read_text()).get("models", []):
                docs[model["name"]] = model
    return docs


def _has_tests(model: dict) -> bool:
    if model.get("data_tests"):
        return True
    return any(c.get("data_tests") for c in model.get("columns", []))


def test_every_gold_model_is_documented():
    assert set(_gold_sql()) == set(_documented())


@pytest.mark.parametrize("name", sorted(_gold_sql()))
def test_gold_model_declares_grain_owner_freshness_and_tests(name):
    model = _documented()[name]
    assert model.get("description"), name
    meta = model.get("meta", {})
    for key in ("grain", "owner", "freshness"):
        assert meta.get(key), f"{name}: meta.{key} missing"
    # The vocabulary of the source contracts (procurelake.contracts.source.SourceContract).
    assert meta.get("data_classification") in CLASSIFICATIONS, f"{name}: meta.data_classification"
    assert _has_tests(model), f"{name}: no tests"


@pytest.mark.parametrize("name", sorted(n for n in _gold_sql() if n.startswith("mart_")))
def test_monetary_marts_group_by_currency(name):
    # Never aggregate across currencies (AGENTS.md 2.4): a mart that sums money groups by currency.
    sql = _gold_sql()[name].read_text().lower()
    if re.search(r"sum\((award_amount|paid_amount|awarded_value|actual_spend|supplier_awarded_value)", sql):
        assert "currency_code" in sql and re.search(r"(group by|partition by)[^;]*currency_code", sql), name


def test_award_value_is_never_called_spend():
    for name, path in _gold_sql().items():
        sql = path.read_text().lower()
        if "award_amount" in sql:
            assert not re.search(r"\bas\s+\w*spend\w*", sql), f"{name} labels award totals as spend"


def test_gold_docs_are_persisted_to_the_glue_catalog():
    # ADR 0042: descriptions, meta and column descriptions go to Glue on every build.
    config = yaml.safe_load((DBT / "dbt_project.yml").read_text())["models"]["procurelake"]
    assert config["+persist_docs"] == {"relation": True, "columns": True}


@pytest.mark.parametrize("name", sorted(_gold_sql()))
def test_tenant_data_is_never_classified_public(name):
    # Tenant-scoped Gold is built from the synthetic tenants; only reference data is public.
    sql = _gold_sql()[name].read_text()
    if "tenant_id" in sql:
        assert _documented()[name]["meta"]["data_classification"] != "public", name
