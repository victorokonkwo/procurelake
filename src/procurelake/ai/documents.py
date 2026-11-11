"""Land public contract documents in Bronze, with provenance and their USAspending award record.

For each catalog document, one batch lands, create-only (ADR 0002):

* ``documents/public_contracts/document_id=<id>/sha256=<hex>/source.pdf``: the PDF exactly
  as downloaded;
* ``.../usaspending_award.json``: the USAspending award record exactly as returned, the
  independent reference for the contract identifier, supplier and agency;
* a manifest in the manifest store (the artifacts bucket,
  ``pipeline_manifests/public_contracts/contract_documents/...``, as for every other source)
  recording the source URL, landing page, licence basis, retrieval time and checksums. The
  manifest is written last and is the only signal extraction reads.

A document whose bytes already landed (same SHA-256, succeeded manifest) is not landed again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from procurelake.ai.catalog import CatalogDocument, DocumentCatalog
from procurelake.ingestion.http import HttpClient
from procurelake.ingestion.landing import LandingStore, sha256_hex
from procurelake.ingestion.manifest import IngestionManifest, LandedFile, load_manifests, write_manifest

SOURCE_SYSTEM = "public_contracts"
DATASET = "contract_documents"
SCHEMA_VERSION = "1"
DOCUMENTS_ROOT = "documents/public_contracts"
USASPENDING_AWARD_URL = "https://api.usaspending.gov/api/v2/awards/{award_id}/"
PDF_MAGIC = b"%PDF-"


class NotAPdfError(ValueError):
    """The downloaded bytes are not a PDF (for example an HTML error page)."""


@dataclass(frozen=True)
class LandedDocument:
    document_id: str
    sha256: str
    pdf_key: str
    award_key: str
    manifest: IngestionManifest


def document_prefix(document_id: str, sha256: str) -> str:
    return f"{DOCUMENTS_ROOT}/document_id={document_id}/sha256={sha256}"


def _landed(manifest: IngestionManifest) -> LandedDocument:
    request = manifest.request
    return LandedDocument(
        document_id=request["document_id"],
        sha256=request["pdf_sha256"],
        pdf_key=request["pdf_key"],
        award_key=request["award_key"],
        manifest=manifest,
    )


def landed_documents(manifest_store: LandingStore) -> dict[str, LandedDocument]:
    """The latest succeeded landing of each document, by document id."""
    latest: dict[str, LandedDocument] = {}
    manifests = sorted(
        load_manifests(manifest_store, SOURCE_SYSTEM, DATASET), key=lambda m: m.ingestion_completed_at
    )
    for manifest in manifests:
        if manifest.status == "succeeded":
            landed = _landed(manifest)
            latest[landed.document_id] = landed
    return latest


def land_document(
    doc: CatalogDocument,
    catalog: DocumentCatalog,
    store: LandingStore,
    manifest_store: LandingStore,
    http: HttpClient,
    code_version: str,
    now=lambda: datetime.now(UTC),
) -> LandedDocument:
    started = now()
    pdf = http.get(str(doc.url)).body
    if not pdf.startswith(PDF_MAGIC):
        raise NotAPdfError(f"{doc.url} did not return a PDF (first bytes {pdf[:16]!r})")
    digest = sha256_hex(pdf)

    existing = landed_documents(manifest_store).get(doc.document_id)
    if existing is not None and existing.sha256 == digest:
        return existing

    award_body = http.get(USASPENDING_AWARD_URL.format(award_id=doc.usaspending_award_id)).body
    award = json.loads(award_body)
    if award.get("generated_unique_award_id") != doc.usaspending_award_id:
        raise ValueError(
            f"USAspending returned award {award.get('generated_unique_award_id')!r}, "
            f"expected {doc.usaspending_award_id!r}"
        )

    prefix = document_prefix(doc.document_id, digest)
    stored_pdf = store.put_new(f"{prefix}/source.pdf", pdf, "application/pdf")
    stored_award = store.put_new(f"{prefix}/usaspending_award.json", award_body, "application/json")
    completed = now()
    manifest = IngestionManifest(
        source_system=SOURCE_SYSTEM,
        dataset=DATASET,
        schema_version=SCHEMA_VERSION,
        batch_id=f"{doc.document_id}-{digest[:16]}",
        status="succeeded",
        ingestion_started_at=started,
        ingestion_completed_at=completed,
        request={
            "document_id": doc.document_id,
            "url": str(doc.url),
            "landing_page": str(doc.landing_page) if doc.landing_page else None,
            "form": doc.form,
            "license_basis": catalog.license_basis,
            "catalog_version": catalog.catalog_version,
            "retrieved_at": started.isoformat(),
            "usaspending_award_id": doc.usaspending_award_id,
            "usaspending_award_url": USASPENDING_AWARD_URL.format(award_id=doc.usaspending_award_id),
            "pdf_key": stored_pdf.key,
            "pdf_sha256": digest,
            "award_key": stored_award.key,
        },
        files=[
            LandedFile(key=s.key, uri=s.uri, size_bytes=s.size_bytes, sha256=s.sha256_hex, record_count=1)
            for s in (stored_pdf, stored_award)
        ],
        code_version=code_version,
    )
    write_manifest(manifest_store, manifest)
    return _landed(manifest)
