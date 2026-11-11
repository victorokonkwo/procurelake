"""Review decisions (moto DynamoDB), Silver SQL generation, and evaluation scoring."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from ai_doubles import evidence, good_extraction

from procurelake.ai import review
from procurelake.ai.evaluate import DocumentLabels, GroundTruth, LabelField, evaluate, outcome
from procurelake.ai.review import FieldDecision, ReviewError
from procurelake.ai.schema import ALL_FIELDS
from procurelake.ai.silver import COLUMNS, create_table_sql, literal, merge_sql, rows_for

ARN = "arn:aws:sts::123456789012:assumed-role/procurelake-dev-ai-extraction/reviewer"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _checks(failed=()):
    out = {}
    for name in ALL_FIELDS:
        missing = good_extraction()[name]["value"] is None
        status = "missing" if missing else ("failed" if name in failed else "passed")
        out[name] = {
            "status": status,
            "reasons": ["quote_not_on_page"] if name in failed else [],
            "notes": [],
        }
    return out


def _record(document_id="doc-1", extraction_id="20261111T110000Z-abcd1234", failed=()):
    return {
        "pk": document_id,
        "sk": extraction_id,
        "extracted_at": "2026-11-11T11:00:00+00:00",
        "model_id": "ca.anthropic.claude-haiku-5-5",
        "response_model": "claude-haiku-5-5",
        "request_id": "req_1",
        "prompt_version": "contract_extraction_v1",
        "schema_version": "contract_extraction.v1",
        "input_tokens": 1000,
        "output_tokens": 200,
        "latency_ms": 2500,
        "cost_usd_estimate": "0.00022",
        "audit_prefix": "audit/ai_extraction/document_id=doc-1/extraction_id=x",
        "extraction": good_extraction(),
        "checks": _checks(failed),
        "provenance": {
            "source_url": "https://www.nasa.gov/x.pdf",
            "landing_page": None,
            "license_basis": "US federal government work",
            "retrieved_at": "2026-11-11T10:00:00+00:00",
            "pdf_sha256": "ab" * 32,
            "pdf_key": "documents/public_contracts/document_id=doc-1/sha256=ab/source.pdf",
            "award_key": "documents/.../usaspending_award.json",
            "usaspending_award_id": "CONT_AWD_NNA09DC26C_8000_-NONE-_-NONE-",
            "ocr_pages": [2],
            "masked": {"emails": 1, "phones": 1},
        },
    }


def test_enqueue_is_create_only(review_table):
    review.enqueue(review_table, _record())
    with pytest.raises(review_table.meta.client.exceptions.ConditionalCheckFailedException):
        review.enqueue(review_table, _record())
    assert [i.status for i in review.list_items(review_table, "pending_review")] == ["pending_review"]


def test_approve_with_accept_passed_and_a_correction(review_table):
    review.enqueue(review_table, _record(failed=("supplier",)))
    item = review.get(review_table, "doc-1", "20261111T110000Z-abcd1234")
    with pytest.raises(ReviewError, match=r"supplier .* needs a decision"):
        review.decide(review_table, item, "Victor", ARN, "approved", accept_passed=True, now=lambda: NOW)
    with pytest.raises(ReviewError, match="cannot be accepted"):
        review.decide(
            review_table, item, "Victor", ARN, "approved",
            {"supplier": FieldDecision(action="accept")}, accept_passed=True, now=lambda: NOW,
        )  # fmt: skip
    decided = review.decide(
        review_table, item, "Victor", ARN, "approved",
        {"supplier": FieldDecision(action="correct", value="Swinerton Builders", page=1)},
        accept_passed=True, now=lambda: NOW,
    )  # fmt: skip
    assert decided.status == "approved"
    assert decided.record["reviewer_arn"] == ARN
    assert decided.record["decisions"]["supplier"]["action"] == "correct"
    assert decided.record["decisions"]["contract_id"] == {"action": "accept"}
    assert "performance_start_date" not in decided.record["decisions"]  # left empty by the model
    with pytest.raises(ReviewError, match="already approved"):
        review.decide(review_table, decided, "Victor", ARN, "rejected", now=lambda: NOW)


def test_decisions_are_validated(review_table):
    review.enqueue(review_table, _record())
    item = review.get(review_table, "doc-1", "20261111T110000Z-abcd1234")
    bad_date = {"effective_date": FieldDecision(action="correct", value="07/30/2009", page=1)}
    with pytest.raises(ReviewError, match="effective_date is invalid"):
        review.decide(review_table, item, "V", ARN, "approved", bad_date, accept_passed=True)
    with pytest.raises(ReviewError, match="reviewer name"):
        review.decide(review_table, item, " ", ARN, "approved", accept_passed=True)
    with pytest.raises(ValueError, match="needs a value"):
        FieldDecision(action="correct", value="x")
    with pytest.raises(ReviewError, match="unknown fields"):
        review.decide(review_table, item, "V", ARN, "approved", {"price": FieldDecision(action="reject")})


def test_rows_for_approved_and_rejected(review_table):
    review.enqueue(review_table, _record())
    item = review.get(review_table, "doc-1", "20261111T110000Z-abcd1234")
    decided = review.decide(
        review_table, item, "Victor", ARN, "approved",
        {"contract_amount": FieldDecision(action="reject")}, accept_passed=True, now=lambda: NOW,
    )  # fmt: skip
    rows = {r["field_name"]: r for r in rows_for(decided)}
    assert set(rows) == set(ALL_FIELDS)
    assert rows["contract_id"]["final_value"] == "NNA09DC26C"
    assert rows["contract_amount"]["review_action"] == "reject"
    assert rows["contract_amount"]["final_value"] is None and rows["contract_amount"]["currency"] is None
    assert rows["performance_start_date"]["review_action"] == "not_extracted"
    assert rows["supplier"]["currency"] is None

    review.enqueue(review_table, _record("doc-2"))
    other = review.get(review_table, "doc-2", "20261111T110000Z-abcd1234")
    rejected = review.decide(review_table, other, "Victor", ARN, "rejected", comment="wrong document")
    assert {r["final_value"] for r in rows_for(rejected)} == {None}
    assert {r["review_action"] for r in rows_for(rejected)} == {"document_rejected"}
    with pytest.raises(ValueError, match="not decided"):
        rows_for(review.ReviewItem("d", "e", "pending_review", {}))


def test_literals_are_escaped_and_typed():
    assert literal("O'Brien", "string") == "'O''Brien'"
    assert literal(None, "int") == "CAST(NULL AS integer)"
    assert literal("2026-11-11T07:00:00-05:00", "timestamp") == "TIMESTAMP '2026-11-11 12:00:00.000000'"
    with pytest.raises(ValueError, match="timezone"):
        literal("2026-11-11T07:00:00", "timestamp")
    with pytest.raises(ValueError, match="NUL"):
        literal("a\x00b", "string")


def test_sql_shapes(review_table):
    review.enqueue(review_table, _record())
    item = review.get(review_table, "doc-1", "20261111T110000Z-abcd1234")
    decided = review.decide(
        review_table, item, "Victor", ARN, "approved", accept_passed=True, now=lambda: NOW
    )
    sql = merge_sql("procurelake_dev_silver", rows_for(decided))
    assert sql.startswith("MERGE INTO procurelake_dev_silver.silver_contract_intelligence t")
    assert "ON t.document_id = s.document_id AND t.extraction_id = s.extraction_id AND t.field_name" in sql
    assert sql.count("(CAST(") + sql.count("('") == len(ALL_FIELDS)
    ddl = create_table_sql("procurelake_dev_silver", "s3://lake/silver/contract_intelligence/")
    assert "'table_type' = 'ICEBERG'" in ddl
    assert all(f"  {c} " in ddl for c, _ in COLUMNS)
    with pytest.raises(ValueError, match="unsafe identifier"):
        merge_sql("x; DROP TABLE y", rows_for(decided))


def _truth(**overrides) -> GroundTruth:
    fields = {name: LabelField(value=good_extraction()[name]["value"]) for name in ALL_FIELDS}
    fields.update(overrides)
    return GroundTruth(
        labels_version=1, labelled_by="test", labelled_on=date(2026, 11, 11), checked_by=None,
        checked_on=None, method="test",
        documents={"doc-1": DocumentLabels(document_type="modification", fields=fields)},
    )  # fmt: skip


def test_outcomes():
    label = LabelField(value="Swinerton Builders, Inc.", accept=["SWINERTON BUILDERS"])
    assert outcome("supplier", "SWINERTON BUILDERS INC", label) == "correct"
    assert outcome("supplier", None, label) == "missed"
    assert outcome("supplier", "Turner", label) == "wrong"
    assert outcome("supplier", None, LabelField(value=None)) == "correct_empty"
    assert outcome("supplier", "Turner", LabelField(value=None)) == "fabricated"
    assert outcome("contract_id", "DESC0012704", LabelField(value="DE-SC0012704")) == "correct"
    assert outcome("contract_amount", "588", LabelField(value="588.00")) == "correct"


def test_evaluate_scores_latest_extraction(review_table):
    review.enqueue(review_table, _record(extraction_id="20261111T100000Z-old00000"))
    record = _record()
    record["extraction"] = {
        **good_extraction(),
        "performance_end_date": evidence("2011-10-07", 1, "x 10/07/2011"),
    }
    review.enqueue(review_table, record)
    review.enqueue(review_table, _record("unlabelled-doc"))
    report = evaluate(
        _truth(supplier=LabelField(value="Turner Construction")), review.list_items(review_table)
    )
    (doc,) = report.documents
    assert doc.extraction_id == "20261111T110000Z-abcd1234"
    assert doc.outcomes["supplier"] == "wrong"
    assert doc.outcomes["performance_end_date"] == "fabricated"
    summary = report.summary()
    assert summary["fabricated"] == 1
    assert summary["unlabelled_documents"] == ["unlabelled-doc"]
    assert summary["accuracy"] == 6 / len(ALL_FIELDS)  # supplier wrong, end date fabricated
    assert summary["labels"]["checked_by"] is None


def test_labels_must_cover_every_field():
    with pytest.raises(ValueError, match="missing"):
        DocumentLabels(document_type="award", fields={"supplier": LabelField(value=None)})
