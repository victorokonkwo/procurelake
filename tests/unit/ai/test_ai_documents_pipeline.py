"""Landing documents in Bronze and running one extraction end to end (local stores, fakes)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ai_doubles import SF30_PAGE, FakeBedrock, FakeTextract, good_extraction, make_pdf

from procurelake.ai import review
from procurelake.ai.catalog import load_catalog
from procurelake.ai.documents import NotAPdfError, land_document, landed_documents
from procurelake.ai.extract import ExtractionError, load_model_config
from procurelake.ai.pipeline import run_extraction
from procurelake.ingestion.http import HttpResponse
from procurelake.ingestion.landing import LocalLandingStore

AI = Path(__file__).resolve().parents[3] / "configs" / "ai"
DOC_ID = "nasa-arc-nna09dc26c-mod01"
AWARD_ID = "CONT_AWD_NNA09DC26C_8000_-NONE-_-NONE-"
AWARD = {
    "generated_unique_award_id": AWARD_ID,
    "piid": "NNA09DC26C",
    "parent_award": None,
    "recipient": {"recipient_name": "SWINERTON BUILDERS"},
}


class FakeHttp:
    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(200, self.routes[url])


@pytest.fixture
def catalog():
    return load_catalog(AI / "public_contract_documents.yaml")


def _http(catalog, pdf: bytes, award: dict = AWARD) -> FakeHttp:
    doc = catalog.get(DOC_ID)
    return FakeHttp(
        {
            str(doc.url): pdf,
            f"https://api.usaspending.gov/api/v2/awards/{AWARD_ID}/": json.dumps(award).encode(),
        }
    )


def test_land_document_is_create_only_and_idempotent(tmp_path, catalog):
    store = LocalLandingStore(tmp_path / "raw")
    manifests = LocalLandingStore(tmp_path / "artifacts")
    pdf = make_pdf([SF30_PAGE])
    http = _http(catalog, pdf)
    landed = land_document(catalog.get(DOC_ID), catalog, store, manifests, http, "test")
    assert store.get(landed.pdf_key) == pdf
    assert json.loads(store.get(landed.award_key))["piid"] == "NNA09DC26C"
    request = landed.manifest.request
    assert request["license_basis"].startswith("US federal government work")
    assert request["usaspending_award_id"] == AWARD_ID

    again = land_document(catalog.get(DOC_ID), catalog, store, manifests, http, "test")
    assert again.sha256 == landed.sha256
    assert len([k for k in manifests.list_keys("pipeline_manifests/") if k.endswith("manifest.json")]) == 1
    assert store.list_keys("pipeline_manifests/") == []
    assert set(landed_documents(manifests)) == {DOC_ID}


def test_land_rejects_html_and_wrong_award(tmp_path, catalog):
    store = LocalLandingStore(tmp_path)
    with pytest.raises(NotAPdfError):
        land_document(catalog.get(DOC_ID), catalog, store, store, _http(catalog, b"<html>"), "test")
    wrong = {**AWARD, "generated_unique_award_id": "CONT_AWD_OTHER_8000_-NONE-_-NONE-"}
    with pytest.raises(ValueError, match="expected"):
        land_document(
            catalog.get(DOC_ID), catalog, store, store, _http(catalog, make_pdf([SF30_PAGE]), wrong), "test"
        )
    assert store.list_keys("") == []  # nothing landed on failure


def _landed(tmp_path, catalog, pages):
    raw = LocalLandingStore(tmp_path / "raw")
    manifests = LocalLandingStore(tmp_path / "manifests")
    landed = land_document(
        catalog.get(DOC_ID), catalog, raw, manifests, _http(catalog, make_pdf(pages)), "test"
    )
    return raw, landed


def test_run_extraction_queues_review_with_audit_trail(tmp_path, catalog, review_table):
    raw, landed = _landed(tmp_path, catalog, [SF30_PAGE, None])
    audit = LocalLandingStore(tmp_path / "artifacts")
    config, prompt = load_model_config(AI)
    textract = FakeTextract([["Page 2 of 2", "BLOCK 14. DESCRIPTION OF MODIFICATION", "no further changes"]])
    bedrock = FakeBedrock(good_extraction())

    run = run_extraction(landed, raw, audit, textract, bedrock, review_table, config, prompt)

    assert run.ocr_pages == [2]
    assert run.checks.all_passed
    sent = bedrock.requests[0]["messages"][0]["content"]
    assert "[EMAIL]" in sent and "[PHONE]" in sent and "jane.doe" not in sent
    keys = audit.list_keys("audit/ai_extraction/")
    names = {k.rsplit("/", 1)[-1] for k in keys}
    assert names == {"pages.json", "page-2.json", "request.json", "response.json"}

    item = review.get(review_table, DOC_ID, run.extraction_id)
    assert item.status == "pending_review"
    assert item.record["provenance"]["usaspending_award_id"] == AWARD_ID
    assert item.record["prompt_version"] == "contract_extraction_v1"
    assert item.checks["contract_amount"]["status"] == "passed"


def test_failed_extraction_writes_error_and_no_review_item(tmp_path, catalog, review_table):
    raw, landed = _landed(tmp_path, catalog, [SF30_PAGE])
    audit = LocalLandingStore(tmp_path / "artifacts")
    config, prompt = load_model_config(AI)
    with pytest.raises(ExtractionError):
        run_extraction(landed, raw, audit, FakeTextract([]), FakeBedrock("{}"), review_table, config, prompt)
    assert any(k.endswith("/error.json") for k in audit.list_keys("audit/"))
    assert review.list_items(review_table) == []


def test_tampered_bronze_object_is_refused(tmp_path, catalog, review_table):
    raw, landed = _landed(tmp_path, catalog, [SF30_PAGE])
    (tmp_path / "raw" / landed.pdf_key).write_bytes(make_pdf(["tampered"]))
    config, prompt = load_model_config(AI)
    with pytest.raises(RuntimeError, match="checksum"):
        run_extraction(
            landed, raw, LocalLandingStore(tmp_path / "a"), FakeTextract([]), FakeBedrock(good_extraction()),
            review_table, config, prompt,
        )  # fmt: skip
