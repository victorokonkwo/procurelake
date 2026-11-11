"""Test doubles for the AI pipeline (tests only): PDFs built in memory, fake Textract and
Bedrock clients, sample extractions. No AWS or model calls."""

from __future__ import annotations

import json
from types import SimpleNamespace

REVIEW_TABLE = "procurelake-dev-ai-review"


def make_pdf(pages: list[str | None]) -> bytes:
    """A valid PDF. Each string becomes a page with a text layer (one line per ``\\n``,
    Helvetica); ``None`` becomes a page with no text, like an image-only scan."""
    objects: list[bytes] = []
    page_ids = []
    font_id = 3
    next_id = 4
    page_objs: list[tuple[int, int, bytes]] = []
    for text in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        lines = (text or "").split("\n") if text else []
        ops = ["BT", "/F1 10 Tf", "12 TL", "40 750 Td"]
        for line in lines:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            ops.append(f"({escaped}) Tj T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1") if lines else b""
        page_objs.append((page_id, content_id, stream))
        page_ids.append(page_id)

    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for _page_id, content_id, stream in page_objs:  # ids follow the order appended
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FakeTextract:
    """Returns the given lines for each call, in order, and records the bytes it was sent."""

    def __init__(self, pages_of_lines: list[list[str]]):
        self._pages = list(pages_of_lines)
        self.calls: list[bytes] = []

    def detect_document_text(self, Document):
        self.calls.append(Document["Bytes"])
        lines = self._pages.pop(0)
        return {"Blocks": [{"BlockType": "PAGE"}] + [{"BlockType": "LINE", "Text": t} for t in lines]}


class FakeBedrock:
    """Stands in for anthropic.AnthropicBedrock: ``messages.create`` returns a canned reply."""

    def __init__(self, reply: dict | str, stop_reason: str = "end_turn", usage=(1200, 300)):
        text = reply if isinstance(reply, str) else json.dumps(reply)
        self.requests: list[dict] = []
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=stop_reason,
            model="claude-haiku-5-5",
            usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
            _request_id="req_test",
        )

        def create(**kwargs):
            self.requests.append(kwargs)
            return response

        self.messages = SimpleNamespace(create=create)


def evidence(value=None, page=None, quote=None, **extra) -> dict:
    return {"value": value, "page": page, "quote": quote, **extra}


SF30_PAGE = (
    "AMENDMENT OF SOLICITATION/MODIFICATION OF CONTRACT\n"
    "10A. MODIFICATION OF CONTRACT/ORDER NO. NNA09DC26C\n"
    "3. EFFECTIVE DATE 07/30/2009\n"
    "6. ISSUED BY NASA Ames Research Center\n"
    "CONTRACTOR Swinerton Builders, Inc.\n"
    "Total amount of contract increased by $588.00\n"
    "PSC Y111 Construction of office buildings\n"
    "Contact: jane.doe@nasa.gov or (650) 604-1234"
)


def good_extraction() -> dict:
    return {
        "document_type": "modification",
        "contract_id": evidence("NNA09DC26C", 1, "MODIFICATION OF CONTRACT/ORDER NO. NNA09DC26C"),
        "supplier": evidence("Swinerton Builders, Inc.", 1, "CONTRACTOR Swinerton Builders, Inc."),
        "issuing_agency": evidence("NASA Ames Research Center", 1, "ISSUED BY NASA Ames Research Center"),
        "effective_date": evidence("2009-07-30", 1, "3. EFFECTIVE DATE 07/30/2009"),
        "performance_start_date": evidence(),
        "performance_end_date": evidence(),
        "contract_category": evidence("Y111", 1, "PSC Y111 Construction of office buildings"),
        "contract_amount": evidence(
            "588.00", 1, "increased by $588.00", currency="USD", amount_type="modification_change"
        ),
    }
