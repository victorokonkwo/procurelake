"""The catalog of public contract documents (``configs/ai/public_contract_documents.yaml``)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

DOCUMENT_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
AWARD_ID = re.compile(r"^CONT_AWD_[A-Z0-9]+_[0-9]{4}_[A-Z0-9-]+_[A-Z0-9-]+$")


class CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    url: HttpUrl
    landing_page: HttpUrl | None
    usaspending_award_id: str
    form: str
    notes: str | None = None

    @field_validator("document_id")
    @classmethod
    def _document_id(cls, value: str) -> str:
        if not DOCUMENT_ID.fullmatch(value) or len(value) > 80:
            raise ValueError(f"document_id must be lower-case words joined by '-': {value!r}")
        return value

    @field_validator("usaspending_award_id")
    @classmethod
    def _award_id(cls, value: str) -> str:
        if not AWARD_ID.fullmatch(value):
            raise ValueError(f"not a USAspending contract award id: {value!r}")
        return value


class DocumentCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_version: int
    license_basis: str
    documents: list[CatalogDocument] = Field(min_length=1)

    @field_validator("documents")
    @classmethod
    def _unique(cls, docs: list[CatalogDocument]) -> list[CatalogDocument]:
        ids = [d.document_id for d in docs]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate document_id: {duplicates}")
        return docs

    def get(self, document_id: str) -> CatalogDocument:
        for doc in self.documents:
            if doc.document_id == document_id:
                return doc
        raise KeyError(f"{document_id!r} is not in the document catalog")


def load_catalog(path: Path) -> DocumentCatalog:
    return DocumentCatalog.model_validate(yaml.safe_load(path.read_text()))
