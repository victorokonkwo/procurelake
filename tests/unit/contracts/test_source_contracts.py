"""Unit tests for source contracts: every committed contract parses, and the validator's
rules reject inconsistent contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from procurelake.contracts.source import SourceContract, load_contracts

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "source"


def test_committed_contracts_parse():
    contracts = load_contracts(CONTRACTS_DIR)
    assert {c.name for c in contracts} == {
        "tenants",
        "agencies",
        "suppliers",
        "solicitations",
        "awards",
        "purchase_orders",
        "invoices",
        "contract_documents",
    }
    assert all(c.tenant_scoped and c.data_classification == "synthetic" for c in contracts)


def test_contract_file_name_matches_contract_name():
    for path in CONTRACTS_DIR.glob("*.yaml"):
        assert yaml.safe_load(path.read_text())["name"] == path.stem


def _base() -> dict:
    return yaml.safe_load((CONTRACTS_DIR / "awards.yaml").read_text())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda c: c["columns"].append(dict(c["columns"][0])), "duplicate column"),
        (lambda c: c.update(primary_key=["missing_col"]), "primary key"),
        (lambda c: c["columns"][0].update(nullable=True), "primary key"),
        (lambda c: c["columns"].__setitem__(1, {**c["columns"][1], "nullable": True}), "tenant_id"),
        (lambda c: c["columns"][4].update(type="money"), "unsupported type"),
        (lambda c: c["foreign_keys"][0].update(columns=["tenant_id", "nope"]), "not declared"),
        (lambda c: c["change_capture"].update(source_updated_at_column="awarded_atx"), "change_capture"),
        (lambda c: c.update(unexpected=True), "Extra inputs"),
    ],
)
def test_validator_rejects_inconsistent_contracts(mutate, message):
    raw = _base()
    mutate(raw)
    with pytest.raises(ValidationError, match=message):
        SourceContract.model_validate(raw)
