"""Validation tests for the generator configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from procurelake.data_generator.config import GeneratorConfig

BASE_TENANT = {
    "tenant_id": "tenant_alder",
    "tenant_name": "Alder (synthetic)",
    "agency_names": ["A", "B"],
    "supplier_count": 5,
    "solicitation_count": 10,
    "currencies": {"CAD": 1.0},
    "near_duplicate_supplier_rate": 0.0,
    "open_market_po_ratio": 0.2,
}


def _config(**tenant_overrides):
    return {
        "as_of": "2026-10-31T23:59:59Z",
        "history_start": "2024-01-01T00:00:00Z",
        "tenants": [{**BASE_TENANT, **tenant_overrides}],
    }


def test_valid_config_parses():
    assert GeneratorConfig.model_validate(_config()).tenants[0].tenant_id == "tenant_alder"


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": "alder"},
        {"agency_names": ["A", "A"]},
        {"currencies": {"cad": 1.0}},
        {"currencies": {"CAD": 0}},
        {"supplier_count": 0},
        {"open_market_po_ratio": 1.0},
        {"unexpected_field": 1},
    ],
)
def test_invalid_tenant_settings_rejected(overrides):
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(_config(**overrides))


def test_naive_timestamps_rejected():
    raw = _config()
    raw["as_of"] = "2026-10-31T23:59:59"
    with pytest.raises(ValidationError):
        GeneratorConfig.model_validate(raw)


def test_duplicate_tenants_rejected():
    raw = _config()
    raw["tenants"].append(dict(BASE_TENANT, tenant_name="Other"))
    with pytest.raises(ValidationError, match="tenant_id values must be unique"):
        GeneratorConfig.model_validate(raw)


def test_history_window_must_be_long_enough():
    raw = _config()
    raw["history_start"] = "2026-10-01T00:00:00Z"
    with pytest.raises(ValidationError, match="at least 120 days"):
        GeneratorConfig.model_validate(raw)
