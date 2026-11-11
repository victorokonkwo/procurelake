"""Page text (text layer vs Textract) and contact-detail masking."""

from __future__ import annotations

import io

import pytest
from ai_doubles import SF30_PAGE, FakeTextract, make_pdf
from pypdf import PdfReader

from procurelake.ai.redact import redact
from procurelake.ai.text import extract_text


def test_text_layer_pages_do_not_call_textract():
    textract = FakeTextract([])
    doc = extract_text(make_pdf([SF30_PAGE]), textract)
    assert [p.source for p in doc.pages] == ["text_layer"]
    assert "NNA09DC26C" in doc.pages[0].text
    assert textract.calls == []


def test_pages_without_text_go_to_textract_one_page_at_a_time():
    textract = FakeTextract([["MODIFICATION NO. 01", "EFFECTIVE DATE 07/30/2009"]])
    doc = extract_text(make_pdf([SF30_PAGE, None]), textract)
    assert [p.source for p in doc.pages] == ["text_layer", "textract"]
    assert doc.pages[1].text == "MODIFICATION NO. 01\nEFFECTIVE DATE 07/30/2009"
    assert doc.ocr_pages == [2]
    assert set(doc.textract_responses) == {2}
    # Textract was sent a single-page PDF.
    assert len(PdfReader(io.BytesIO(textract.calls[0])).pages) == 1


def test_short_text_layer_counts_as_no_text():
    textract = FakeTextract([["full OCR text of the scanned page with enough words"]])
    doc = extract_text(make_pdf(["only three words"]), textract, min_words=15)
    assert doc.pages[0].source == "textract"


def test_scan_without_textract_client_raises():
    with pytest.raises(RuntimeError, match="no usable text layer"):
        extract_text(make_pdf([None]), textract=None)


def test_redact_masks_emails_and_phones_only():
    text = (
        "Contact jane.doe@nasa.gov or (650) 604-1234 / 650.604.1235 / +1 650 604 1236. "
        "Contract NNA09DC26C, amount $24,823,320.00, DUNS 123456789, date 07/30/2009."
    )
    result = redact(text)
    assert result.emails == 1
    assert result.phones == 3
    for kept in ("NNA09DC26C", "$24,823,320.00", "123456789", "07/30/2009"):
        assert kept in result.text
    assert "jane.doe" not in result.text
    assert "604-1234" not in result.text
