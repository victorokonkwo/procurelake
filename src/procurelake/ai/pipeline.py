"""Run one extraction for a landed document and put it in the review queue.

Steps: read the PDF and its USAspending record from Bronze -> page text (Textract for pages
without a text layer) -> mask contact details -> Bedrock extraction -> deterministic checks ->
review item (``pending_review``).

Audit trail, create-only in the artifacts bucket under
``audit/ai_extraction/document_id=<id>/extraction_id=<eid>/``:

* ``pages.json``: the masked text sent to the model, per page, with its source and masking
  counts;
* ``textract/page-<n>.json``: raw Textract responses for OCR'd pages;
* ``request.json``: model, prompt and schema versions, and SHA-256 of the exact system prompt
  and document text sent;
* ``response.json`` (raw reply, usage, latency, cost estimate) or ``error.json``.

A failed extraction writes ``error.json`` and raises; it never creates a review item.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from procurelake.ai import review
from procurelake.ai.checks import CheckReport, check_extraction
from procurelake.ai.documents import LandedDocument
from procurelake.ai.extract import ExtractionError, ExtractionResult, ModelConfig, extract, render_document
from procurelake.ai.redact import redact
from procurelake.ai.text import PageText, extract_text
from procurelake.ingestion.landing import LandingStore

AUDIT_ROOT = "audit/ai_extraction"


def new_extraction_id(now: datetime) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def audit_prefix(document_id: str, extraction_id: str) -> str:
    return f"{AUDIT_ROOT}/document_id={document_id}/extraction_id={extraction_id}"


def _json(data) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True, default=str).encode() + b"\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class ExtractionRun:
    document_id: str
    extraction_id: str
    result: ExtractionResult
    checks: CheckReport
    ocr_pages: list[int]


def run_extraction(
    landed: LandedDocument,
    raw_store: LandingStore,
    audit_store: LandingStore,
    textract,
    bedrock_client,
    review_table,
    config: ModelConfig,
    system_prompt: str,
    now=lambda: datetime.now(UTC),
) -> ExtractionRun:
    started = now()
    extraction_id = new_extraction_id(started)
    prefix = audit_prefix(landed.document_id, extraction_id)

    pdf = raw_store.get(landed.pdf_key)
    if hashlib.sha256(pdf).hexdigest() != landed.sha256:
        raise RuntimeError(f"{landed.pdf_key} does not match its manifest checksum")
    award = json.loads(raw_store.get(landed.award_key))

    text = extract_text(pdf, textract, min_words=config.text.min_words_per_page)
    for number, response in text.textract_responses.items():
        audit_store.put_new(f"{prefix}/textract/page-{number}.json", _json(response), "application/json")

    pages: list[PageText] = []
    masked = {"emails": 0, "phones": 0}
    for page in text.pages:
        r = redact(page.text)
        masked["emails"] += r.emails
        masked["phones"] += r.phones
        pages.append(PageText(page.page, r.text, page.source, page.word_count))
    audit_store.put_new(
        f"{prefix}/pages.json",
        _json({"pages": [asdict(p) for p in pages], "masked": masked}),
        "application/json",
    )

    document_text = render_document(landed.document_id, pages)
    audit_store.put_new(
        f"{prefix}/request.json",
        _json(
            {
                "document_id": landed.document_id,
                "pdf_sha256": landed.sha256,
                "model_id": config.model_id,
                "foundation_model": config.foundation_model,
                "region": config.region,
                "effort": config.effort,
                "max_tokens": config.max_tokens,
                "prompt_version": config.prompt_version,
                "schema_version": config.schema_version,
                "system_prompt_sha256": _sha(system_prompt),
                "document_text_sha256": _sha(document_text),
                "pages": len(pages),
                "ocr_pages": text.ocr_pages,
            }
        ),
        "application/json",
    )

    try:
        result = extract(bedrock_client, config, system_prompt, landed.document_id, pages)
    except ExtractionError as exc:
        audit_store.put_new(
            f"{prefix}/error.json",
            _json({"error": str(exc), "raw_text": exc.raw_text, "request_id": exc.request_id}),
            "application/json",
        )
        raise

    audit_store.put_new(
        f"{prefix}/response.json",
        _json(
            {
                "raw_text": result.raw_text,
                "response_model": result.response_model,
                "request_id": result.request_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "latency_ms": result.latency_ms,
                "cost_usd_estimate": str(result.cost_usd),
            }
        ),
        "application/json",
    )

    checks = check_extraction(result.extraction, pages, award)
    request = landed.manifest.request
    review.enqueue(
        review_table,
        {
            "pk": landed.document_id,
            "sk": extraction_id,
            "extracted_at": started.isoformat(),
            "model_id": result.model_id,
            "response_model": result.response_model,
            "request_id": result.request_id,
            "prompt_version": config.prompt_version,
            "schema_version": config.schema_version,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "latency_ms": result.latency_ms,
            "cost_usd_estimate": str(result.cost_usd),
            "audit_prefix": prefix,
            "extraction": result.extraction.model_dump(mode="json"),
            "checks": {
                name: {"status": c.status, "reasons": list(c.reasons), "notes": list(c.notes)}
                for name, c in checks.fields.items()
            },
            "provenance": {
                "source_url": request["url"],
                "landing_page": request.get("landing_page"),
                "license_basis": request["license_basis"],
                "retrieved_at": request["retrieved_at"],
                "pdf_sha256": landed.sha256,
                "pdf_key": landed.pdf_key,
                "award_key": landed.award_key,
                "usaspending_award_id": request["usaspending_award_id"],
                "ocr_pages": text.ocr_pages,
                "masked": masked,
            },
        },
    )
    return ExtractionRun(landed.document_id, extraction_id, result, checks, text.ocr_pages)
