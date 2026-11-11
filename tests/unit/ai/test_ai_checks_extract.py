"""Deterministic evidence checks and the Bedrock call wrapper (fake client)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from ai_doubles import SF30_PAGE, FakeBedrock, evidence, good_extraction

from procurelake.ai.checks import amounts_in, check_extraction, dates_in, value_in_quote
from procurelake.ai.extract import ExtractionError, extract, load_model_config
from procurelake.ai.schema import ContractExtraction
from procurelake.ai.text import PageText

AI = Path(__file__).resolve().parents[3] / "configs" / "ai"
PAGES = [PageText(1, SF30_PAGE, "text_layer", 40)]
AWARD = {
    "piid": "NNA09DC26C",
    "parent_award": None,
    "recipient": {"recipient_name": "SWINERTON BUILDERS"},
}


def test_dates_in_reads_contract_form_formats():
    text = "07/30/2009, 7/1/15, 2014-12-22, 20141222, Sep 18, 2009, 18 SEP 2009, 01-OCT-19, 13/45/2020"
    assert dates_in(text) == {
        date(2009, 7, 30), date(2015, 7, 1), date(2014, 12, 22), date(2009, 9, 18), date(2019, 10, 1),
    }  # fmt: skip


def test_amounts_in():
    assert amounts_in("$356,422,167.89 and 588.00 and 35,000,000") == {
        Decimal("356422167.89"), Decimal("588.00"), Decimal("35000000"),
    }  # fmt: skip


@pytest.mark.parametrize(
    ("field", "value", "quote", "ok"),
    [
        ("contract_id", "DE-SC0012704", "CONTRACT NO. DESC0012704", True),
        ("contract_id", "NNA09DC26C", "NNAO9DC26C", False),
        ("effective_date", "2009-07-30", "EFFECTIVE DATE 07/30/2009", True),
        ("effective_date", "2009-07-31", "EFFECTIVE DATE 07/30/2009", False),
        ("contract_amount", "588.00", "increased by $588.00", True),
        ("contract_amount", "588", "increased by $588.00", True),
        ("contract_amount", "5880", "increased by $588.00", False),
        ("supplier", "Swinerton Builders, Inc.", "CONTRACTOR SWINERTON BUILDERS INC", True),
        ("supplier", "Swinerton Builders", "CONTRACTOR Turner Construction", False),
    ],
)
def test_value_in_quote(field, value, quote, ok):
    assert value_in_quote(field, value, quote) is ok


def test_check_extraction_passes_traceable_fields():
    report = check_extraction(ContractExtraction.model_validate(good_extraction()), PAGES, AWARD)
    assert report.all_passed
    assert report.fields["performance_start_date"].status == "missing"
    assert report.fields["supplier"].notes == ()  # same words as the USAspending recipient


def test_check_extraction_failures():
    data = good_extraction()
    data["supplier"] = evidence("Turner Construction", 1, "CONTRACTOR Turner Construction")
    data["effective_date"] = evidence("2009-07-30", 2, "3. EFFECTIVE DATE 07/30/2009")
    data["contract_id"] = evidence("NNA09DC99C", 1, "ORDER NO. NNA09DC99C")
    report = check_extraction(ContractExtraction.model_validate(data), PAGES, AWARD)
    assert report.fields["supplier"].reasons == ("quote_not_on_page",)
    assert report.fields["supplier"].notes == ("supplier_differs_from_usaspending:SWINERTON BUILDERS",)
    assert report.fields["effective_date"].reasons == ("page_out_of_range",)
    assert report.fields["contract_id"].reasons == ("quote_not_on_page", "contract_id_not_in_usaspending")
    assert set(report.failed) == {"supplier", "effective_date", "contract_id"}


def test_parent_piid_is_accepted():
    award = {**AWARD, "piid": "70SBUR19F00000565", "parent_award": {"piid": "GS00Q17GWD2110"}}
    data = good_extraction()
    data["contract_id"] = evidence("GS00Q17GWD2110", 1, "x GS00Q17GWD2110")
    page = [PageText(1, SF30_PAGE + "\nx GS00Q17GWD2110", "text_layer", 40)]
    report = check_extraction(ContractExtraction.model_validate(data), page, award)
    assert report.fields["contract_id"].status == "passed"


def test_extract_sends_schema_and_validates_reply():
    config, prompt = load_model_config(AI)
    bedrock = FakeBedrock(good_extraction(), usage=(10_000, 500))
    result = extract(bedrock, config, prompt, "doc-1", PAGES)
    request = bedrock.requests[0]
    assert request["model"] == "ca.anthropic.claude-haiku-5-5"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert '<page number="1">' in request["messages"][0]["content"]
    assert result.extraction.contract_id.value == "NNA09DC26C"
    assert result.cost_usd == Decimal("0.0011") + Decimal("0.000275")


@pytest.mark.parametrize(
    ("reply", "stop_reason", "message"),
    [
        (good_extraction(), "refusal", "declined"),
        (good_extraction(), "max_tokens", "cut off"),
        ("not json", "end_turn", "schema validation"),
        ({**good_extraction(), "confidence": 0.9}, "end_turn", "schema validation"),
    ],
)
def test_extract_failures_raise(reply, stop_reason, message):
    config, prompt = load_model_config(AI)
    with pytest.raises(ExtractionError, match=message):
        extract(FakeBedrock(reply, stop_reason=stop_reason), config, prompt, "doc-1", PAGES)


def test_extract_refuses_to_truncate():
    config, prompt = load_model_config(AI)
    pages = [PageText(i, "x", "text_layer", 1) for i in range(1, config.max_pages + 2)]
    with pytest.raises(ExtractionError, match="not truncating"):
        extract(FakeBedrock(good_extraction()), config, prompt, "doc-1", pages)
