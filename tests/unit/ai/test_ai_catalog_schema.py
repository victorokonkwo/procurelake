"""The document catalog, the extraction schema and the model config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_doubles import evidence, good_extraction
from pydantic import ValidationError

from procurelake.ai.catalog import DocumentCatalog, load_catalog
from procurelake.ai.extract import load_model_config
from procurelake.ai.schema import ContractExtraction, json_schema

AI = Path(__file__).resolve().parents[3] / "configs" / "ai"


def test_catalog_loads_and_ids_are_unique():
    catalog = load_catalog(AI / "public_contract_documents.yaml")
    assert len(catalog.documents) == 12
    assert (
        catalog.get("nasa-arc-nna09dc26c-mod01").usaspending_award_id
        == "CONT_AWD_NNA09DC26C_8000_-NONE-_-NONE-"
    )
    with pytest.raises(KeyError):
        catalog.get("missing")


def test_catalog_rejects_duplicates_and_bad_ids():
    doc = {
        "document_id": "a-b",
        "url": "https://www.nasa.gov/x.pdf",
        "landing_page": None,
        "usaspending_award_id": "CONT_AWD_NNA09DC26C_8000_-NONE-_-NONE-",
        "form": "SF-30",
    }
    with pytest.raises(ValidationError, match="duplicate"):
        DocumentCatalog(catalog_version=1, license_basis="x", documents=[doc, doc])
    with pytest.raises(ValidationError, match="lower-case"):
        DocumentCatalog(catalog_version=1, license_basis="x", documents=[{**doc, "document_id": "A B"}])
    with pytest.raises(ValidationError, match="USAspending"):
        DocumentCatalog(
            catalog_version=1, license_basis="x", documents=[{**doc, "usaspending_award_id": "NNA09DC26C"}]
        )


def test_committed_json_schema_matches_code():
    assert json.loads((AI / "extraction_schema.json").read_text()) == json_schema()


def test_schema_is_strict_for_structured_outputs():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json_schema())


def test_good_extraction_validates():
    ContractExtraction.model_validate(good_extraction())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("effective_date", evidence("07/30/2009", 1, "x"), "date"),
        ("effective_date", evidence("2009-02-30", 1, "x"), "day is out of range"),
        ("supplier", evidence("X", None, None), "all present or all null"),
        ("supplier", evidence(None, 1, "quote"), "all present or all null"),
        ("supplier", evidence("X", 1, "  "), "blank"),
        (
            "contract_amount",
            evidence("$588.00", 1, "x", currency="USD", amount_type="other"),
            "plain decimal",
        ),
        ("contract_amount", evidence("588", 1, "x", currency="usd", amount_type="other"), "ISO 4217"),
        (
            "contract_amount",
            evidence("588", 1, "x", currency=None, amount_type=None),
            "currency and amount_type",
        ),
        ("contract_amount", evidence(None, None, None, currency="USD", amount_type=None), "must be null"),
        ("contract_amount", evidence("588", 1, "x", currency="USD", amount_type="spend"), "amount_type"),
    ],
)
def test_schema_rules(field, value, message):
    data = good_extraction()
    data[field] = value
    with pytest.raises(ValidationError, match=message):
        ContractExtraction.model_validate(data)


def test_extra_keys_rejected():
    data = good_extraction()
    data["confidence"] = 0.99
    with pytest.raises(ValidationError):
        ContractExtraction.model_validate(data)


def test_model_config_and_prompt_load():
    config, prompt = load_model_config(AI)
    assert config.model_id == "ca.anthropic.claude-haiku-5-5"
    assert config.region == "ca-central-1"
    assert config.auto_approve is False
    assert "Never guess" in prompt
    pricing = config.pricing_usd_per_million_tokens
    assert pricing.cost_usd(100_000, 1_000_000) == pricing.input / 10 + pricing.output
    # Over the threshold, the whole request is billed at the long-context rates.
    assert pricing.cost_usd(200_000, 0) == pricing.input_long_context / 5
