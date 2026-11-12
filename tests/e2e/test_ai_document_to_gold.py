"""Acceptance test 11: AI evaluation. "Process a document with missing fields → unsupported
values are not fabricated."

Local tier, with test doubles for Bedrock and Textract (the live run is blocked on the Bedrock
quota; ADR 0032). A public-contract-shaped PDF that does **not** state a contract amount or an
effective date goes through the real pipeline: landing in Bronze, text extraction, redaction,
the model call, schema validation, evidence checks, the human review queue, a reviewer's
approval, and the Silver rows that Gold reads.

The model double behaves badly on purpose: it returns an amount with a quote that is not on the
page (a fabrication), and correctly leaves the effective date empty. The checks must fail the
fabricated field; approval must demand an explicit decision on it (the reviewer rejects it); and neither
field may reach the rows Gold admits (``stg_ai__approved_fields``: approved, accepted or
corrected, final value present).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from procurelake.ai import review
from procurelake.ai.catalog import load_catalog
from procurelake.ai.documents import land_document
from procurelake.ai.extract import load_model_config
from procurelake.ai.pipeline import run_extraction
from procurelake.ai.silver import rows_for
from procurelake.ingestion.http import HttpResponse
from procurelake.ingestion.landing import LocalLandingStore

REPO = Path(__file__).resolve().parents[2]
AI = REPO / "configs" / "ai"
_spec = importlib.util.spec_from_file_location(
    "ai_doubles_e2e", REPO / "tests" / "unit" / "ai" / "ai_doubles.py"
)
doubles = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(doubles)

DOC_ID = "nasa-arc-nna09dc26c-mod01"
AWARD_ID = "CONT_AWD_NNA09DC26C_8000_-NONE-_-NONE-"
AWARD = {"generated_unique_award_id": AWARD_ID, "piid": "NNA09DC26C", "parent_award": None,
         "recipient": {"recipient_name": "SWINERTON BUILDERS"}}  # fmt: skip
# The modification page without its effective date and without any amount.
PAGE_WITHOUT_AMOUNT_OR_DATE = "\n".join(
    line for line in doubles.SF30_PAGE.split("\n") if "EFFECTIVE DATE" not in line and "$" not in line
)
STAGING = REPO / "dbt" / "models" / "staging" / "contract_intelligence" / "stg_ai__approved_fields.sql"


class FakeHttp:
    """Serves the catalog's document URL and its USAspending award (test double)."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes

    def get(self, url: str) -> HttpResponse:
        return HttpResponse(200, self.routes[url])


@pytest.fixture
def review_table():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="ca-central-1")
        client.create_table(
            TableName=doubles.REVIEW_TABLE,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="ca-central-1").Table(doubles.REVIEW_TABLE)


def test_missing_fields_are_never_fabricated_through_to_gold(tmp_path, review_table):
    catalog = load_catalog(AI / "public_contract_documents.yaml")
    doc = catalog.get(DOC_ID)
    raw, manifests, audit = (LocalLandingStore(tmp_path / n) for n in ("raw", "manifests", "artifacts"))
    http = FakeHttp({
        str(doc.url): doubles.make_pdf([PAGE_WITHOUT_AMOUNT_OR_DATE]),
        f"https://api.usaspending.gov/api/v2/awards/{AWARD_ID}/": json.dumps(AWARD).encode(),
    })  # fmt: skip
    landed = land_document(doc, catalog, raw, manifests, http, "e2e")

    reply = doubles.good_extraction()
    reply["effective_date"] = doubles.evidence()  # correctly empty: the page does not state it
    # A fabrication: an amount, with a quote that appears nowhere in the document.
    reply["contract_amount"] = doubles.evidence(
        "588.00", 1, "increased by $588.00", currency="USD", amount_type="modification_change"
    )
    config, prompt = load_model_config(AI)
    run = run_extraction(landed, raw, audit, doubles.FakeTextract([]), doubles.FakeBedrock(reply),
                         review_table, config, prompt)  # fmt: skip

    assert not run.checks.all_passed
    item = review.get(review_table, DOC_ID, run.extraction_id)
    assert item.status == "pending_review"  # uncertain results go to a human
    assert item.checks["contract_amount"]["status"] == "failed"
    assert item.checks["effective_date"]["status"] == "missing"

    reviewer_arn = "arn:aws:sts::123456789012:assumed-role/r/e2e"
    # A failed field cannot slip through "accept everything that passed": it needs a decision.
    with pytest.raises(review.ReviewError, match="contract_amount"):
        review.decide(review_table, item, "E2E Reviewer", reviewer_arn, "approved", accept_passed=True)
    decided = review.decide(
        review_table, item, "E2E Reviewer", reviewer_arn, "approved",
        decisions={"contract_amount": review.FieldDecision(action="reject")}, accept_passed=True,
    )  # fmt: skip
    rows = {r["field_name"]: r for r in rows_for(decided)}

    # What the Gold staging model admits: approved, accepted or corrected, with a final value.
    staging_sql = STAGING.read_text()
    assert "review_status = 'approved'" in staging_sql
    assert (
        "review_action in ('accept', 'correct')" in staging_sql and "final_value is not null" in staging_sql
    )
    admitted = {
        name for name, r in rows.items()
        if r["review_status"] == "approved" and r["review_action"] in ("accept", "correct")
        and r["final_value"] is not None
    }  # fmt: skip

    for unsupported in (
        "contract_amount",
        "effective_date",
        "performance_start_date",
        "performance_end_date",
    ):
        assert rows[unsupported]["final_value"] is None, unsupported
        assert unsupported not in admitted, unsupported
    assert rows["contract_amount"]["extracted_value"] == "588.00"  # kept for audit, never published
    assert rows["contract_amount"]["currency"] is None
    # Every value that does reach Gold is quoted from the document's own text.
    for name in admitted:
        assert rows[name]["final_quote"] in PAGE_WITHOUT_AMOUNT_OR_DATE, name
    assert admitted == {"contract_id", "supplier", "issuing_agency", "contract_category"}
