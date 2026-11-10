"""Gold governance rules (AGENTS.md section 2.4), checked on the dbt project files."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

DBT = Path(__file__).resolve().parents[3] / "dbt"
GOLD_DIRS = [DBT / "models" / "core", DBT / "models" / "marts"]


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
