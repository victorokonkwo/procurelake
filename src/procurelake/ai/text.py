"""Page text from a contract PDF: the PDF's own text layer, or Textract OCR where it has none.

A page whose text layer has fewer than ``min_words`` words is treated as an image and sent to
Amazon Textract ``DetectDocumentText``. The synchronous API takes a single-page PDF as bytes,
so each such page is split out with pypdf and sent on its own (no S3 hand-off, so no access to
the encrypted Bronze object is delegated to Textract).

Scans that carry an existing OCR text layer keep that layer as delivered: it is the
document's own text, and its quality is part of what the evaluation measures.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Literal

from pypdf import PdfReader, PdfWriter

DEFAULT_MIN_WORDS = 15
TEXTRACT_MAX_BYTES = 10 * 1024 * 1024  # synchronous DetectDocumentText limit for raw bytes


@dataclass(frozen=True)
class PageText:
    page: int  # 1-based
    text: str
    source: Literal["text_layer", "textract"]
    word_count: int


@dataclass
class DocumentText:
    pages: list[PageText]
    textract_responses: dict[int, dict] = field(default_factory=dict)  # page -> raw response

    @property
    def ocr_pages(self) -> list[int]:
        return [p.page for p in self.pages if p.source == "textract"]

    def page(self, number: int) -> PageText | None:
        return next((p for p in self.pages if p.page == number), None)


def _words(text: str) -> int:
    return len(text.split())


def single_page_pdf(reader: PdfReader, index: int) -> bytes:
    writer = PdfWriter()
    writer.add_page(reader.pages[index])
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def textract_lines(response: dict) -> str:
    """Join Textract LINE blocks in the order Textract returns them (reading order)."""
    return "\n".join(b["Text"] for b in response.get("Blocks", []) if b.get("BlockType") == "LINE")


def extract_text(pdf: bytes, textract=None, min_words: int = DEFAULT_MIN_WORDS) -> DocumentText:
    """Text for every page. ``textract`` is a boto3 Textract client; without one, pages that
    need OCR raise, so a scan is never silently treated as an empty document."""
    reader = PdfReader(io.BytesIO(pdf))
    pages: list[PageText] = []
    responses: dict[int, dict] = {}
    for index, pdf_page in enumerate(reader.pages):
        number = index + 1
        layer = (pdf_page.extract_text() or "").strip()
        if _words(layer) >= min_words:
            pages.append(PageText(number, layer, "text_layer", _words(layer)))
            continue
        if textract is None:
            raise RuntimeError(f"page {number} has no usable text layer and no Textract client was given")
        page_pdf = single_page_pdf(reader, index)
        if len(page_pdf) > TEXTRACT_MAX_BYTES:
            raise ValueError(f"page {number} is {len(page_pdf)} bytes, over the Textract sync limit")
        response = textract.detect_document_text(Document={"Bytes": page_pdf})
        responses[number] = response
        text = textract_lines(response)
        pages.append(PageText(number, text, "textract", _words(text)))
    return DocumentText(pages=pages, textract_responses=responses)
