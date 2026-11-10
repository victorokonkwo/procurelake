"""Silver spec loading (no Spark needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from procurelake.contracts.source import load_contracts
from procurelake.lakehouse.silver.spec import load_spec, load_specs, spark_type

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = load_contracts(REPO / "contracts" / "source")
CONFIG_DIR = REPO / "configs" / "pipelines" / "silver"


@pytest.mark.parametrize(
    ("contract_type", "expected"),
    [
        ("uuid", "string"),
        ("text", "string"),
        ("char(3)", "string"),
        ("numeric(18,2)", "decimal(18,2)"),
        ("timestamptz", "timestamp"),
        ("date", "date"),
        ("boolean", "boolean"),
    ],
)
def test_spark_type_mapping(contract_type, expected):
    assert spark_type(contract_type) == expected


def test_every_pipeline_config_loads_against_its_contract():
    specs = load_specs(CONTRACTS, CONFIG_DIR)
    assert set(specs) == {
        "tenants",
        "agencies",
        "suppliers",
        "solicitations",
        "awards",
        "purchase_orders",
        "invoices",
    }
    awards = specs["awards"]
    assert awards.silver_table == "silver_awards"
    assert awards.primary_key == ["award_id"]
    assert awards.allowed_values == {"status": ["active", "cancelled"]}
    assert awards.char_lengths == {"currency_code": 3}
    assert "award_amount" in awards.not_null
    assert awards.change_column == "updated_at"


def test_partition_column_must_exist(tmp_path):
    bad = tmp_path / "awards.yaml"
    bad.write_text(
        "source_table: awards\nsilver_table: silver_awards\npartition_by: [nope]\nmax_reject_ratio: 0\n"
    )
    contract = next(c for c in CONTRACTS if c.name == "awards")
    with pytest.raises(ValueError, match="partition columns not in contract"):
        load_spec(contract, bad)


def test_config_must_match_contract_name(tmp_path):
    bad = tmp_path / "awards.yaml"
    bad.write_text("source_table: invoices\nsilver_table: silver_awards\nmax_reject_ratio: 0\n")
    contract = next(c for c in CONTRACTS if c.name == "awards")
    with pytest.raises(ValueError, match="source_table"):
        load_spec(contract, bad)
